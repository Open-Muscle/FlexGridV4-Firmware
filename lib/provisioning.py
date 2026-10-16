# lib/provisioning.py
# Device-side AP/SoftAP provisioning per PROVISIONING.md v1.0.
#
# Boot path:
#   unprovisioned (no wifi_ssid)  ->  provisioning_run(settings, display, ...)
#                                     starts WPA2-PSK AP "OM-<dev>-<id-tail>",
#                                     binds HTTP server on 192.168.4.1:80,
#                                     blocks until POST /provision lands and
#                                     persists creds, then soft-resets.
#   provisioned                   ->  normal flexgrid.py startup (STA mode).
#
# Endpoints (PROVISIONING.md section 4):
#   GET  /info           -> device id, dev type, fw, caps, state, matrix
#   POST /provision      -> {ssid, password, hub_host?, hub_port?} ->
#                          persist + ack + soft-reset. SSID/password lengths
#                          are checked in UTF-8 bytes (32 / 63) and stored
#                          exactly as sent (no stripping).
#   POST /reprovision    -> clear ssid + rotate PSK + soft-reset (callable
#                          from the STA-mode normal LAN once provisioned;
#                          available from AP mode too as an idempotent ack)
#   GET  /               -> minimal HTML captive-portal page (optional v1.0;
#                          we ship it because the cost is one f-string and
#                          it doubles the no-app fallback path)
#
# The HTTP reader honours Content-Length: it reads until the header
# terminator, then until the declared body is complete (cap MAX_BODY_BYTES,
# READ_TIMEOUT_S overall), because a client may send headers and body in
# separate TCP segments. Anything oversized gets 413, a short body gets 400
# "incomplete_body".
#
# Security model: WPA2-PSK with a per-device RANDOM PSK that is delivered
# OUT OF BAND via the OLED. Never derived from any public field, never
# transmitted in the clear over the radio. See PROVISIONING.md section 3.

import os
import socket
import ujson
import time
import network
import asyncio
import machine
import logger


# Charset excludes the ambiguous pairs 0/o and 1/l (so OLED reading + phone
# typing stays clean). 32 chars, 5 bits/char.
PSK_CHARSET = "abcdefghijkmnpqrstuvwxyz23456789"
PSK_LENGTH  = 10

# AP parameters per PROVISIONING.md section 3.
AP_IP            = "192.168.4.1"
AP_NETMASK       = "255.255.255.0"
AP_GATEWAY       = "192.168.4.1"
AP_DNS           = "192.168.4.1"
AP_CHANNEL       = 1
AP_MAX_CLIENTS   = 4
AP_TXPOWER_DBM   = 10    # a phone next to the band needs no more, and the
                         # battery path cannot feed the radio at full power
AP_AUTH_WPA2_PSK = 3   # network.AUTH_WPA2_PSK == 3 on ESP32 MicroPython

HTTP_PORT       = 80
PROVISION_REBOOT_DELAY_MS = 1500

# Request limits. The body of a valid /provision is well under 300 bytes
# (32-byte SSID + 63-byte password + JSON); 2 KB leaves room for escapes.
MAX_HEAD_BYTES  = 4096
MAX_BODY_BYTES  = 2048
READ_CHUNK      = 512
READ_TIMEOUT_S  = 3

# WPA2 limits, in bytes (a UTF-8 SSID with accents is shorter in characters
# than in bytes, and it is bytes the radio cares about).
SSID_MAX_BYTES  = 32
PSK_MIN_BYTES   = 8
PSK_MAX_BYTES   = 63


def mint_psk():
    """Generate a 10-char random PSK from os.urandom. ~50 bits of entropy.
    Excludes ambiguous pairs (0/o, 1/l) so OLED -> phone typing is clean."""
    raw = os.urandom(PSK_LENGTH)
    return "".join(PSK_CHARSET[b & 0x1F] for b in raw)


def ssid_for(device_type, device_id):
    """SSID = OM-<dev>-<id-tail>. id-tail is device_id with the '<dev>-'
    type prefix stripped (e.g. flexgrid-d7af0b -> d7af0b)."""
    prefix = device_type + "-"
    tail = device_id[len(prefix):] if device_id.startswith(prefix) else device_id
    return "OM-{}-{}".format(device_type, tail)


# ---------------------------------------------------------------------------
# HTTP/1.1 request + response helpers. MicroPython has no built-in HTTP
# server, so we parse one request per connection (no keep-alive) directly.
# Bodies are JSON unless the route returns HTML.
# ---------------------------------------------------------------------------

class _HttpError(Exception):
    """Raised while reading a request; carries the response to send."""
    def __init__(self, code, text, message):
        self.code = code
        self.text = text
        self.message = message


def _head_end(raw):
    """Returns (index of the header terminator, its length), or (-1, 0) when
    the headers are not complete yet. Tolerates LF or CRLF."""
    i = raw.find(b"\r\n\r\n")
    if i >= 0:
        return i, 4
    i = raw.find(b"\n\n")
    if i >= 0:
        return i, 2
    return -1, 0


def _content_length(head):
    """Content-Length from a header block (bytes), or None if absent."""
    for ln in head.decode("utf-8", "replace").split("\n")[1:]:
        k, sep, v = ln.partition(":")
        if sep and k.strip().lower() == "content-length":
            v = v.strip()
            if not v.isdigit():
                raise _HttpError(400, "Bad Request", "bad_content_length")
            return int(v)
    return None


async def _read_request(reader):
    """Read one full HTTP request: headers up to the blank line, then the
    body up to Content-Length. A single reader.read() is not enough because
    clients (browsers, http.client, curl on a slow link) may send the headers
    and the body in separate TCP segments. Returns the raw bytes (b"" if the
    peer connected and closed without sending anything). Raises _HttpError
    for oversized or truncated requests; the caller applies the timeout."""
    raw = b""
    while True:
        end, sep = _head_end(raw)
        if end >= 0:
            break
        if len(raw) > MAX_HEAD_BYTES:
            raise _HttpError(413, "Payload Too Large", "request_too_large")
        chunk = await reader.read(READ_CHUNK)
        if not chunk:
            if raw:
                raise _HttpError(400, "Bad Request", "incomplete_body")
            return raw
        raw += chunk
    want = _content_length(raw[:end])
    if want is None:
        return raw
    if want > MAX_BODY_BYTES:
        raise _HttpError(413, "Payload Too Large", "request_too_large")
    need = end + sep + want
    while len(raw) < need:
        chunk = await reader.read(READ_CHUNK)
        if not chunk:
            raise _HttpError(400, "Bad Request", "incomplete_body")
        raw += chunk
    return raw


def _parse_request(raw):
    """Returns (method, path, headers_dict, body_bytes). Tolerates LF or
    CRLF; rejects malformed requests by raising ValueError. The body is cut
    to Content-Length when the header is present."""
    head_end, sep_len = _head_end(raw)
    if head_end < 0:
        raise ValueError("incomplete request head")
    head = raw[:head_end].decode("utf-8", "replace")
    body = raw[head_end + sep_len:]
    lines = head.split("\n")
    request_line = lines[0].strip()
    parts = request_line.split(" ")
    if len(parts) < 2:
        raise ValueError("bad request line")
    method = parts[0].upper()
    path = parts[1]
    headers = {}
    for ln in lines[1:]:
        ln = ln.strip()
        if not ln or ":" not in ln:
            continue
        k, v = ln.split(":", 1)
        headers[k.strip().lower()] = v.strip()
    cl = headers.get("content-length", "")
    if cl.isdigit():
        body = body[:int(cl)]
    return method, path, headers, body


def _respond(writer, status_code, status_text, body, content_type="application/json"):
    body_bytes = body.encode("utf-8") if isinstance(body, str) else body
    writer.write("HTTP/1.1 {} {}\r\n".format(status_code, status_text).encode("utf-8"))
    writer.write("Content-Type: {}\r\n".format(content_type).encode("utf-8"))
    writer.write("Content-Length: {}\r\n".format(len(body_bytes)).encode("utf-8"))
    writer.write(b"Cache-Control: no-store\r\n")
    writer.write(b"Connection: close\r\n")
    writer.write(b"\r\n")
    writer.write(body_bytes)


def _json_body(headers, body):
    if not body:
        return {}
    try:
        return ujson.loads(body.decode("utf-8"))
    except Exception as e:
        raise ValueError("invalid_json: {}".format(e))


# ---------------------------------------------------------------------------
# Captive-portal HTML (minimal; PROVISIONING.md section 4.4). One inline
# script, plain CSS, no external assets so it works on every phone browser.
# ---------------------------------------------------------------------------

_CAPTIVE_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OpenMuscle Setup</title>
<style>body{font-family:system-ui,sans-serif;max-width:24rem;margin:2rem auto;padding:0 1rem;color:#222}
h1{font-size:1.3rem;margin:0 0 .25rem}p{color:#555;margin:.25rem 0 1rem}
label{display:block;margin:.75rem 0 .25rem;font-weight:600}
input{width:100%;box-sizing:border-box;padding:.6rem;font-size:1rem;border:1px solid #ccc;border-radius:.4rem}
button{margin-top:1rem;width:100%;padding:.75rem;font-size:1rem;background:#1a73e8;color:#fff;border:0;border-radius:.4rem}
#msg{margin-top:1rem;font-size:.95rem}</style>
</head><body><h1>OpenMuscle: connect to Wi-Fi</h1>
<p id="dev">Loading device info...</p>
<form id="f"><label>Wi-Fi name (SSID)</label><input name="ssid" required autocomplete="off">
<label>Wi-Fi password</label><input name="password" type="password">
<button type="submit">Connect</button></form><div id="msg"></div>
<script>
fetch("/info").then(r=>r.json()).then(d=>{document.getElementById("dev").textContent="Device "+d.id+" ("+d.dev+", "+d.fw+")"});
document.getElementById("f").addEventListener("submit",e=>{e.preventDefault();
const f=new FormData(e.target),m=document.getElementById("msg");m.textContent="Connecting...";
fetch("/provision",{method:"POST",headers:{"Content-Type":"application/json"},
body:JSON.stringify({ssid:f.get("ssid"),password:f.get("password")})})
.then(r=>r.json()).then(d=>{m.textContent=d.status=="ok"?"Saved. Device is rebooting; reconnect to your home Wi-Fi.":("Error: "+(d.data&&d.data.message||"unknown"))})
.catch(e=>m.textContent="Error: "+e)});
</script></body></html>"""


# ---------------------------------------------------------------------------
# State carried by the HTTP handlers. settings_manager is passed in so the
# /provision handler can persist + soft-reset.
# ---------------------------------------------------------------------------

class _ProvisioningState:
    def __init__(self, settings, settings_manager, info_extras, state="unprovisioned"):
        self.settings = settings
        self.settings_manager = settings_manager
        self.info_extras = info_extras or {}
        self.reboot_pending = False
        self.state = state   # "unprovisioned" (AP mode) or "provisioned" (STA mode)

    def info_payload(self):
        d = {
            "v":     "1.0",
            "type":  "info",
            "id":    self.settings.get("device_id"),
            "dev":   self.settings.get("device_type"),
            "fw":    self.settings.get("fw_version"),
            "caps":  self.info_extras.get("caps", []),
            "state": self.state,
            # Why the last attempt to join a home network failed
            # (wrong_password | no_ap_found | timeout), or "". Lets a phone
            # or the PC app say "that password did not work" after the band
            # drops back into setup mode. last_ssid is the network it tried.
            "last_wifi_error": self.settings.get("last_wifi_error") or "",
            "last_ssid":       self.settings.get("last_ssid") or "",
            # left / right / labeler, or "" when no role has been assigned.
            "role":            self.settings.get("role") or "",
        }
        # Optional device-type-specific fields (e.g. matrix dims for FlexGrid)
        for k, v in self.info_extras.items():
            if k == "caps":
                continue
            d[k] = v
        return d


def _error_json(message, ack_type=None):
    d = {"status": "error", "data": {"message": message}}
    if ack_type:
        d["v"] = "1.0"
        d["type"] = ack_type
    return ujson.dumps(d)


def _wifi_bytes_ok(value, lo, hi):
    return isinstance(value, str) and lo <= len(value.encode("utf-8")) <= hi


async def _handle(reader, writer, state):
    try:
        try:
            raw = await asyncio.wait_for(_read_request(reader), READ_TIMEOUT_S)
        except _HttpError as e:
            _respond(writer, e.code, e.text, _error_json(e.message))
            await writer.drain()
            return
        except (asyncio.CancelledError, SystemExit):
            raise
        except Exception:
            # wait_for timed out (or the socket died) before the declared
            # body arrived.
            _respond(writer, 400, "Bad Request", _error_json("incomplete_body"))
            await writer.drain()
            return
        if not raw:
            return
        try:
            method, path, headers, body = _parse_request(raw)
        except ValueError as e:
            _respond(writer, 400, "Bad Request", _error_json(str(e)))
            await writer.drain()
            return

        if method == "GET" and path == "/info":
            _respond(writer, 200, "OK", ujson.dumps(state.info_payload()))

        elif method == "POST" and path == "/provision":
            try:
                req = _json_body(headers, body)
                if not isinstance(req, dict):
                    raise ValueError("invalid_json: expected an object")
            except ValueError as e:
                _respond(writer, 400, "Bad Request", _error_json(str(e), "provision_ack"))
                await writer.drain()
                return
            # SSID and password are stored exactly as sent: an SSID may
            # legitimately begin or end with a space, and a stripped copy
            # would never join. Lengths are UTF-8 bytes, as WPA2 counts them.
            ssid = req.get("ssid")
            password = req.get("password")
            if password is None:
                password = ""
            hub_host = req.get("hub_host")
            hub_port = req.get("hub_port")
            if not _wifi_bytes_ok(ssid, 1, SSID_MAX_BYTES) or not ssid.strip():
                _respond(writer, 400, "Bad Request",
                         _error_json("ssid_length_invalid (1..32 bytes)", "provision_ack"))
                await writer.drain()
                return
            if password and not _wifi_bytes_ok(password, PSK_MIN_BYTES, PSK_MAX_BYTES):
                _respond(writer, 400, "Bad Request",
                         _error_json("password_length_invalid (8..63 bytes for WPA2, or empty)",
                                     "provision_ack"))
                await writer.drain()
                return
            # Persist creds. Do NOT verify the join here: the AP and STA share
            # one radio, so the join happens after the reboot. If the user
            # typed the password wrong the first join fails and flexgrid.py
            # sends the band back to setup mode (wifi_joined_ok stays False
            # until a join succeeds; last_wifi_error then says why).
            state.settings["wifi_ssid"] = ssid
            state.settings["wifi_password"] = password
            state.settings["wifi_joined_ok"] = False
            state.settings["last_wifi_error"] = ""
            state.settings["last_ssid"] = ""
            if hub_host:
                state.settings["hub_host"] = hub_host
            if hub_port:
                try:
                    state.settings["hub_port"] = int(hub_port)
                except (TypeError, ValueError):
                    pass
            state.settings_manager.save(state.settings)
            logger.info("Provisioned: ssid='{}' (saved); rebooting in {}ms".format(
                ssid, PROVISION_REBOOT_DELAY_MS))
            _respond(writer, 200, "OK",
                     ujson.dumps({"v": "1.0", "type": "provision_ack",
                                  "status": "ok",
                                  "id":     state.settings.get("device_id"),
                                  "next":   "switching_to_sta",
                                  "reboot_in_ms": PROVISION_REBOOT_DELAY_MS}))
            state.reboot_pending = True

        elif method == "POST" and path == "/reprovision":
            # Clear creds + rotate PSK so a returned-to-factory device does
            # not present the same PSK to the next owner.
            state.settings["wifi_ssid"] = ""
            state.settings["wifi_password"] = ""
            state.settings["wifi_joined_ok"] = False
            state.settings["last_wifi_error"] = ""
            state.settings["last_ssid"] = ""
            state.settings["provisioning_psk"] = mint_psk()
            state.settings_manager.save(state.settings)
            logger.info("Reprovision requested; PSK rotated; rebooting in {}ms".format(
                PROVISION_REBOOT_DELAY_MS))
            _respond(writer, 200, "OK",
                     ujson.dumps({"v": "1.0", "type": "reprovision_ack",
                                  "status": "ok",
                                  "reboot_in_ms": PROVISION_REBOOT_DELAY_MS}))
            state.reboot_pending = True

        elif method == "GET" and path == "/":
            _respond(writer, 200, "OK", _CAPTIVE_HTML, content_type="text/html; charset=utf-8")

        else:
            _respond(writer, 404, "Not Found",
                     ujson.dumps({"status": "error",
                                  "data": {"message": "no_route", "method": method, "path": path}}))

        await writer.drain()
    except (asyncio.CancelledError, SystemExit):
        # Let deliberate cancellation / soft_reset propagate; close cleanup
        # still runs via the finally block.
        raise
    except BaseException as e:
        logger.warn("Provisioning HTTP handler failed: {} ({})".format(
            type(e).__name__, e))
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


def start_ap(settings, device_type, device_id):
    """Bring up the WPA2-PSK access point. Returns the configured PSK so the
    caller can show it on the OLED. Mints + persists a new PSK if the
    settings dict does not already have one."""
    psk = settings.get("provisioning_psk")
    if not psk or len(psk) != PSK_LENGTH:
        psk = mint_psk()
        settings["provisioning_psk"] = psk
        logger.info("Minted new provisioning PSK; persisting to settings.json")

    ssid = ssid_for(device_type, device_id)

    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    try:
        ap.config(txpower=AP_TXPOWER_DBM)
    except Exception as e:
        logger.warn("AP txpower not set: {}".format(e))
    # ifconfig before config so the address space is set when clients DHCP.
    try:
        ap.ifconfig((AP_IP, AP_NETMASK, AP_GATEWAY, AP_DNS))
    except Exception as e:
        logger.warn("AP ifconfig failed (continuing): {}".format(e))
    try:
        ap.config(essid=ssid, password=psk, authmode=AP_AUTH_WPA2_PSK,
                  channel=AP_CHANNEL, max_clients=AP_MAX_CLIENTS, hidden=False)
    except Exception as e:
        # Older MicroPython builds may not accept all kwargs; retry with the
        # minimal set that every ESP32 port supports.
        logger.warn("AP config full kwargs failed ({}); retrying minimal".format(e))
        ap.config(essid=ssid, password=psk, authmode=AP_AUTH_WPA2_PSK)

    logger.info("AP up: ssid='{}' psk='{}' ip={}".format(ssid, psk, AP_IP))
    return psk


async def serve(state, bind_ip=AP_IP):
    """Run the HTTP server. In AP mode (bind_ip=AP_IP, the default) this
    blocks until state.reboot_pending and then soft_resets, which is the
    boot-time provisioning flow. In STA mode (bind_ip=device's LAN IP)
    this runs forever as a background task, serving /info and /reprovision
    against the home LAN per PROVISIONING.md section 4.3. Reprovision
    still soft_resets when triggered (the user is intentionally rebooting
    the device back to AP mode).

    Bind to a specific IP rather than 0.0.0.0 so we do not accidentally
    expose the reprovision endpoint on both interfaces simultaneously."""
    server = await asyncio.start_server(
        lambda r, w: _handle(r, w, state), bind_ip, HTTP_PORT)
    logger.info("Provisioning HTTP server listening on {}:{} (state={})".format(
        bind_ip, HTTP_PORT, state.state))

    try:
        while not state.reboot_pending:
            await asyncio.sleep(0.2)
    finally:
        try:
            server.close()
        except Exception:
            pass

    # Give the OS time to flush the HTTP response before the radio drops.
    await asyncio.sleep_ms(PROVISION_REBOOT_DELAY_MS)
    logger.info("Provisioning complete; soft_reset()")
    machine.soft_reset()


async def run(settings, settings_manager, device_type, device_id, info_extras=None,
              status_led_blue_blink=None):
    """Entry point. Boot to AP mode, serve provisioning endpoints, soft-reset
    when the user finishes. Returns only on errors (callers should treat
    a return as a hard fault and fall back to STA-retry behavior).

    settings:           live settings dict (mutated; persisted on save).
    settings_manager:   has .save(settings) (persists to config/settings.json).
    device_type:        e.g. "flexgrid", "lask5".
    device_id:          e.g. "flexgrid-d7af0b".
    info_extras:        dict merged into GET /info (e.g. {"caps": [...], "matrix": [15,4]}).
    status_led_blue_blink: optional callable; awaited in the background if given.
    """
    psk = start_ap(settings, device_type, device_id)
    # Persist immediately so a reset before /provision still keeps the PSK
    # stable (the user might be mid-typing it from the OLED).
    settings_manager.save(settings)

    state = _ProvisioningState(settings, settings_manager, info_extras)
    tasks = [asyncio.create_task(serve(state))]
    if status_led_blue_blink is not None:
        tasks.append(asyncio.create_task(status_led_blue_blink()))

    for t in tasks:
        await t
