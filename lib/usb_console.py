# lib/usb_console.py
# Command channel on the USB serial port, used by the PC app to give a band its
# Wi-Fi over the cable (docs/usb-console.md has the exact JSON).
#
# Why this exists: the band's own setup network (192.168.4.1) needs the radio
# in access-point mode and, on battery, the supply cannot start it. The USB
# cable is always there during setup, so the app sends the Wi-Fi name and
# password down the same cable the band is already plugged into.
#
# Wire format: one JSON object per line in, exactly one JSON line out.
#   in : {"v":"1.0","type":"cmd","id":"<short string>","data":{"verb":"..."}}
#   out: {"v":"1.0","type":"ack","id":"<same>","ok":true,"data":{...}}
#        {"v":"1.0","type":"ack","id":"<same>","ok":false,"error":"<code>","message":"<words>"}
# Verbs: get_info, set_wifi {ssid, password}, forget_wifi, set_role {role}.
# Error codes: bad_json, unknown_verb, bad_ssid, bad_password, save_failed.
#
# Lines that are blank, do not start with '{', or carry control bytes (raw-REPL
# traffic from mpremote / flash.py) are ignored without a reply. Log lines the
# firmware prints on the same port are fine: the host only looks at lines that
# start with '{'.
#
# The parser and dispatcher take plain callables (save, restart, set_role,
# get_ip, write, sleep_ms) so the desktop tests drive them with strings and a
# fake settings dict; only serve() touches sys.stdin.

import asyncio
import sys
import time
import ujson
import logger


VERSION = "1.0"

SSID_MAX_BYTES = 32
PSK_MIN_BYTES = 8
PSK_MAX_BYTES = 63
ROLES = ("left", "right", "labeler", "")

# Time between the ack leaving and machine.reset(), so the reply gets out
# of the USB buffer before the port drops.
RESTART_DELAY_MS = 1000

# One command line is ~200 bytes at the most; anything far past that is noise.
MAX_LINE_BYTES = 768
# A half-received line older than this is junk (a stray byte, a dropped cable);
# the next byte starts a fresh line instead of gluing onto it.
STALE_LINE_MS = 2000
# Pause when the stream returns nothing, so a closed port cannot spin the loop.
IDLE_MS = 50
ERROR_PAUSE_MS = 200

# CancelledError is a BaseException subclass in modern asyncio; older
# MicroPython builds may not expose it. Same fallback as lib/commands.py.
try:
    _CancelledError = asyncio.CancelledError
except AttributeError:
    _CancelledError = type("_NoCancelledError", (BaseException,), {})


class _CmdError(Exception):
    """Raised by a verb to turn into an ok:false ack."""
    def __init__(self, code, message):
        Exception.__init__(self, code)
        self.code = code
        self.message = message


def _ack_ok(msg_id, data):
    return {"v": VERSION, "type": "ack", "id": msg_id, "ok": True, "data": data}


def _ack_err(msg_id, code, message):
    return {"v": VERSION, "type": "ack", "id": msg_id, "ok": False,
            "error": code, "message": message}


def _bytes_len(value):
    return len(value.encode("utf-8"))


def clean_line(raw):
    """Return the command text for one received line, or None when the line
    must be ignored silently: blank, not starting with '{', or holding a
    control byte (raw-REPL traffic such as Ctrl-A / Ctrl-D). A line that is
    not valid UTF-8 is returned as an unparseable placeholder so it gets a
    bad_json reply rather than being dropped."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    raw = bytes(raw).strip()
    if not raw or raw[0] != 0x7B:      # '{'
        return None
    for b in raw:
        if (b < 0x20 and b != 0x09) or b == 0x7F:
            return None
    try:
        return raw.decode("utf-8")
    except Exception:
        return "{"                      # fails to parse -> bad_json


class LineBuffer:
    """Collects bytes into lines. feed() takes one byte (an int or a length-1
    bytes) and returns a finished line (bytes, newline removed) or None. A line
    that outgrows MAX_LINE_BYTES is dropped up to its newline, and a half line
    older than STALE_LINE_MS is thrown away when the next byte arrives."""

    def __init__(self, now_ms=None):
        self._now_ms = now_ms or time.ticks_ms
        self._buf = bytearray()
        self._overflow = False
        self._last = None

    def feed(self, b):
        if not isinstance(b, int):
            b = b[0]
        now = self._now_ms()
        if (self._last is not None and self._buf
                and time.ticks_diff(now, self._last) > STALE_LINE_MS):
            self._buf = bytearray()
            self._overflow = False
        self._last = now
        if b == 0x0A:                   # '\n'
            line = bytes(self._buf)
            over = self._overflow
            self._buf = bytearray()
            self._overflow = False
            return None if over else line
        if self._overflow:
            return None
        if len(self._buf) >= MAX_LINE_BYTES:
            self._overflow = True
            self._buf = bytearray()
            return None
        self._buf.append(b)
        return None


class UsbConsole:
    """Dispatcher for the USB command channel.

    settings   the live settings dict (the same object SettingsManager saved)
    save       callable() -> truthy when the settings were written to flash
               (SettingsManager.save bound to the dict)
    restart    callable() that resets the chip (machine.reset)
    set_role   async callable(role) -> stored role (same semantics as the TCP
               set_role verb in lib/commands.py)
    get_ip     callable() -> current station address or ''
    write      callable(str) that emits one reply line (print)
    sleep_ms   async callable(ms) used for the pause before the restart
    """

    def __init__(self, settings, save, restart, set_role=None, get_ip=None,
                 write=None, sleep_ms=None, restart_delay_ms=RESTART_DELAY_MS):
        self.settings = settings
        self._save = save
        self._restart = restart
        self._set_role = set_role or self._default_set_role
        self._get_ip = get_ip or (lambda: "")
        self._write = write or print
        self._sleep_ms = sleep_ms or asyncio.sleep_ms
        self.restart_delay_ms = restart_delay_ms
        self.verbs = {
            "get_info":    self._v_get_info,
            "set_wifi":    self._v_set_wifi,
            "forget_wifi": self._v_forget_wifi,
            "set_role":    self._v_set_role,
        }

    # -- helpers -----------------------------------------------------------

    async def _default_set_role(self, role):
        if role not in ROLES:
            role = ""
        self.settings["role"] = role
        self._save()
        return role

    def _persist(self, changes):
        """Apply `changes` to the settings and save. On a failed save the old
        values are put back so memory matches flash. Returns True on success."""
        old = {}
        for k in changes:
            old[k] = self.settings.get(k)
        self.settings.update(changes)
        ok = False
        try:
            ok = bool(self._save())
        except Exception as e:
            logger.warn("usb console: save raised {}".format(type(e).__name__))
        if not ok:
            self.settings.update(old)
        return ok

    def _wifi_changes(self, ssid, password):
        # device_id, role and provisioning_psk are deliberately not in here.
        return {
            "wifi_ssid":       ssid,
            "wifi_password":   password,
            "wifi_joined_ok":  False,
            "last_ssid":       "",
            "last_wifi_error": "",
        }

    # -- verbs: each returns (data_dict, restart_needed) --------------------

    async def _v_get_info(self, data):
        s = self.settings
        ssid = s.get("wifi_ssid") or ""
        return {
            "id":          s.get("device_id") or "",
            "role":        s.get("role") or "",
            "fw":          s.get("fw_version") or "",
            "state":       "provisioned" if ssid.strip() else "unprovisioned",
            "wifi_ssid":   ssid,
            "ip":          self._get_ip() or "",
            "usb_console": 1,
        }, False

    async def _v_set_wifi(self, data):
        ssid = data.get("ssid")
        password = data.get("password")
        if password is None:
            password = ""
        if (not isinstance(ssid, str) or not ssid.strip()
                or not 1 <= _bytes_len(ssid) <= SSID_MAX_BYTES):
            raise _CmdError("bad_ssid", "The Wi-Fi name must be 1 to 32 characters.")
        if (not isinstance(password, str)
                or (password and not PSK_MIN_BYTES <= _bytes_len(password) <= PSK_MAX_BYTES)):
            raise _CmdError("bad_password",
                            "The Wi-Fi password must be 8 to 63 characters, or empty for an open network.")
        if not self._persist(self._wifi_changes(ssid, password)):
            raise _CmdError("save_failed", "The band could not save the Wi-Fi details.")
        # Never log the name or the password: kit_check treats a log that names
        # a network as not in factory state.
        logger.info("Wi-Fi saved over USB; restarting")
        return {"restart": True}, True

    async def _v_forget_wifi(self, data):
        if not self._persist(self._wifi_changes("", "")):
            raise _CmdError("save_failed", "The band could not save the change.")
        logger.info("Wi-Fi forgotten over USB; restarting")
        return {"restart": True}, True

    async def _v_set_role(self, data):
        role = data.get("role", "")
        if not isinstance(role, str):
            role = ""
        stored = await self._set_role(role)
        return {"role": stored}, False

    # -- line handling -----------------------------------------------------

    async def handle(self, raw):
        """Process one received line. Returns (reply_dict_or_None, restart).
        None means the line was ignored silently."""
        text = clean_line(raw)
        if text is None:
            return None, False
        try:
            pkt = ujson.loads(text)
        except Exception:
            return _ack_err("", "bad_json", "That line is not valid JSON."), False
        if not isinstance(pkt, dict):
            return _ack_err("", "bad_json", "A command must be a JSON object."), False

        msg_id = pkt.get("id", "")
        if isinstance(msg_id, bool) or not isinstance(msg_id, (str, int)):
            msg_id = ""
        # Anything that says it is not a command (for example our own ack
        # echoed back by a terminal) is dropped, never answered.
        ptype = pkt.get("type", "cmd")
        if ptype != "cmd":
            return None, False
        data = pkt.get("data", {})
        if data is None:
            data = {}
        if not isinstance(data, dict):
            return _ack_err(msg_id, "bad_json", "The data field must be a JSON object."), False

        verb = data.get("verb")
        fn = self.verbs.get(verb) if isinstance(verb, str) else None
        if fn is None:
            return _ack_err(msg_id, "unknown_verb",
                            "Unknown verb: {}".format(verb)), False
        try:
            out, restart = await fn(data)
        except _CmdError as e:
            return _ack_err(msg_id, e.code, e.message), False
        except (_CancelledError, SystemExit, KeyboardInterrupt):
            raise
        except Exception as e:
            logger.warn("usb console: verb {} failed: {}".format(verb, type(e).__name__))
            return _ack_err(msg_id, "save_failed",
                            "The band could not complete that: {}".format(type(e).__name__)), False
        return _ack_ok(msg_id, out), restart

    async def process(self, raw):
        """Handle one line end to end: write the reply, and only then (after a
        pause so the reply leaves the USB buffer) restart if the verb asked."""
        reply, restart = await self.handle(raw)
        if reply is None:
            return None
        self._write(ujson.dumps(reply))
        try:
            sys.stdout.flush()
        except Exception:
            pass
        if restart:
            await self._sleep_ms(self.restart_delay_ms)
            self._restart()
        return reply


def _stdin_byte_reader():
    """Production byte source: asyncio.StreamReader on sys.stdin, one byte at a
    time. read(1) only runs after the poll says a byte is ready, so it never
    blocks the event loop (readline() would sit inside C waiting for a newline
    if a stray byte arrived without one)."""
    reader = asyncio.StreamReader(sys.stdin)

    async def read_byte():
        return await reader.read(1)
    return read_byte


async def serve(console, read_byte=None):
    """Background task: read lines from the USB serial port and answer them.
    Every read is guarded so a bad byte or a transient error cannot end the
    task. KeyboardInterrupt is re-raised on purpose: Ctrl-C from mpremote /
    flash.py must still reach the REPL exactly as before this module existed."""
    if read_byte is None:
        try:
            read_byte = _stdin_byte_reader()
        except Exception as e:
            logger.warn("USB console unavailable: {} ({})".format(type(e).__name__, e))
            return
    lines = LineBuffer()
    logger.info("USB console ready")
    while True:
        try:
            b = await read_byte()
            if not b:
                await asyncio.sleep_ms(IDLE_MS)
                continue
            line = lines.feed(b)
            if line is not None:
                await console.process(line)
        except (_CancelledError, SystemExit, KeyboardInterrupt):
            raise
        except Exception as e:
            logger.warn("USB console read/handle failed: {} ({})".format(
                type(e).__name__, e))
            await asyncio.sleep_ms(ERROR_PAUSE_MS)
