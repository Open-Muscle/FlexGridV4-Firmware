"""Pre-ship check for a FlexGrid V4 band, run over USB with mpremote.

Reads the band's settings, log files, I2C bus, battery and firmware files,
decides whether it is in FACTORY STATE (ready to hand to someone who will give
it their own Wi-Fi), and prints either FAIL with the reasons or PASS with the
text for the band's sticker. On FAIL it still prints the setup name and
password (clearly marked as read only, not sticker text) when the band has a
well-formed one, because a band without a screen shows them nowhere else.

Usage:
    python tools/kit_check.py <runtime_com> [options]

        <runtime_com>   COM port of the band while it runs MicroPython
                        (VID:PID 303a:4001 in `mpremote connect list`).

    Options:
        --role R        set the band's role first (left, right or labeler)
        --label TEXT    set a short device label first (printed on the
                        sticker text, e.g. "Band A"; at most 16 characters)
        --clear-logs    delete log.txt and log.txt.bak before checking (the
                        log can name a Wi-Fi network from bench testing)
        --min-battery V fail when the battery is under V volts (default 3.7)
        --require-sd    fail, instead of warn, when no SD card mounts
        --require-screen
                        fail, instead of warn, when no screen answers (only
                        for bands that are meant to ship WITH the screen)
        --require-tag   fail, instead of warn, unless this checkout is exactly
                        on a git tag (the public release)
        --reset         reboot the band afterwards

Factory state means all of: no Wi-Fi name or password saved, no join history
(wifi_joined_ok false, no last_ssid), no log.txt.bak, and no line in log.txt
that names a Wi-Fi network. A log.txt that only holds boot lines from the
band's own setup network is fine.

Other checks: whether the OLED answers at 0x3C, the battery reads at least
--min-battery, the band has a setup password of the right shape, and every
firmware file on the band is byte-identical to this checkout (so the band runs
exactly the code in the repo, ideally the tagged release; see --require-tag).

The screen is a WARNING, not a failure, by default. Kit bands ship without the
small screen (its header is left unplugged) and the firmware copes: the person
reads the setup name and password from the sticker this tool prints, and the
blue light with two quick blinks then a pause is the normal setup pattern. The
warning says the sticker is the only place the password lives. Use
--require-screen for a batch that is meant to carry the screen.

The Wi-Fi password is never read back to the PC: the band only reports whether
one is saved. Exit status: 0 PASS, 1 FAIL, 2 could not talk to the band.

Needs mpremote in the same Python (pip install mpremote).
"""
import hashlib
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import flash  # noqa: E402  (repo-root module; firmware_files lists what is flashed)

PSK_CHARSET = "abcdefghijkmnpqrstuvwxyz23456789"
PSK_LENGTH = 10
OLED_ADDR = 0x3C
# Every address the firmware's IMU driver accepts: InvenSense/Bosch parts at
# 0x68/0x69 and the TOKMAS ICM-42688-P rebrand at 0x36/0x37 (lib/imu.py).
IMU_ADDRS = (0x36, 0x37, 0x68, 0x69)
ROLES = ("left", "right", "labeler")
LABEL_MAX = 16
DEFAULT_MIN_BATTERY_V = 3.7
NO_BATTERY_V = 2.5
MARKER = "KITCHECK "


# ---------------------------------------------------------------------------
# Code that runs ON the band. {root} is "/" on the device (a temp directory in
# the host tests); {pins} and {files} are filled in from the repo.
# ---------------------------------------------------------------------------

PROBE_CODE = '''
import os, json, machine, hashlib, binascii
R = {root!r}
def rd(p):
    try:
        with open(R + p) as f:
            return f.read()
    except OSError:
        return None
def ex(p):
    try:
        os.stat(R + p)
        return True
    except OSError:
        return False
out = {{}}
raw = rd('config/settings.json')
s = {{}}
out['settings_present'] = raw is not None
if raw:
    try:
        s = json.loads(raw)
    except Exception:
        out['settings_unreadable'] = True
out['device_id'] = s.get('device_id')
out['device_type'] = s.get('device_type')
out['role'] = s.get('role') or ''
out['label'] = s.get('device_label') or ''
out['psk'] = s.get('provisioning_psk') or ''
out['has_ssid'] = bool((s.get('wifi_ssid') or '').strip())
out['has_password'] = bool(s.get('wifi_password'))
out['joined_ok'] = bool(s.get('wifi_joined_ok'))
out['has_last_ssid'] = bool(s.get('last_ssid'))
out['bak'] = ex('log.txt.bak')
log = {{'present': ex('log.txt'), 'bytes': 0, 'net_lines': 0}}
if log['present']:
    try:
        log['bytes'] = os.stat(R + 'log.txt')[6]
        with open(R + 'log.txt') as f:
            for line in f:
                if ('Connecting to Wi-Fi SSID=' in line
                        or ("'wifi_ssid': '" in line and "'wifi_ssid': ''" not in line)
                        or ("'last_ssid': '" in line and "'last_ssid': ''" not in line)):
                    log['net_lines'] += 1
    except Exception:
        log['unreadable'] = True
out['log'] = log
try:
    i2c = machine.I2C(0, scl=machine.Pin({scl}), sda=machine.Pin({sda}), freq=400000)
    out['i2c'] = sorted(i2c.scan())
except Exception as e:
    out['i2c'] = None
try:
    a = machine.ADC(machine.Pin({adc}))
    a.atten(machine.ADC.ATTN_11DB)
    t = 0
    for _ in range(32):
        t += a.read_uv()
    out['vbat'] = round(t / 32 / 1000000 * 2, 3)
except Exception as e:
    out['vbat'] = None
try:
    if 'sd' in os.listdir('/'):
        out['sd'] = True
    else:
        card = machine.SDCard(slot=2, sck=machine.Pin({sck}), mosi=machine.Pin({mosi}),
                              miso=machine.Pin({miso}), cs=machine.Pin({cs}))
        os.mount(card, '/sd')
        os.umount('/sd')
        out['sd'] = True
except Exception as e:
    out['sd'] = False
H = {{}}
for p in {files!r}:
    try:
        h = hashlib.sha256()
        with open(R + p, 'rb') as f:
            while True:
                b = f.read(512)
                if not b:
                    break
                h.update(b)
        H[p] = binascii.hexlify(h.digest()).decode()
    except OSError:
        H[p] = None
out['hashes'] = H
print('KITCHECK ' + json.dumps(out))
'''

# Settings edits and log clearing. {role}/{label} are None or a string.
APPLY_CODE = '''
import os, json
R = {root!r}
ROLE = {role!r}
LABEL = {label!r}
out = {{'role_set': False, 'label_set': False, 'removed': []}}
if ROLE is not None or LABEL is not None:
    p = R + 'config/settings.json'
    try:
        with open(p) as f:
            s = json.load(f)
        if ROLE is not None:
            s['role'] = ROLE
            out['role_set'] = True
        if LABEL is not None:
            s['device_label'] = LABEL
            out['label_set'] = True
        with open(p, 'w') as f:
            json.dump(s, f)
    except OSError:
        out['no_settings'] = True
if {clear!r}:
    try:
        import logger
        fp = logger._log_fp
        if fp is not None:
            fp.close()
        logger._log_fp = None
        logger._persist_enabled = False
    except Exception:
        pass
    for p in ('log.txt', 'log.txt.bak'):
        try:
            os.remove(R + p)
            out['removed'].append(p)
        except OSError:
            pass
print('KITCHECK ' + json.dumps(out))
'''


def pins_from_pinmap():
    """Pin numbers from lib/pinmap.py, so a pinmap change is followed here."""
    wanted = {"I2C_SDA": 8, "I2C_SCL": 9, "ADC_BAT": 18, "SPI_SCK": 12,
              "SPI_MOSI": 13, "SPI_MISO": 11, "SD_CS": 14}
    try:
        with open(os.path.join(ROOT, "lib", "pinmap.py"), encoding="utf-8") as f:
            text = f.read()
        for name in wanted:
            m = re.search(r"^{}\s*=\s*(\d+)".format(name), text, re.M)
            if m:
                wanted[name] = int(m.group(1))
    except OSError:
        pass
    return wanted


def probe_code(root="/"):
    p = pins_from_pinmap()
    return PROBE_CODE.format(
        root=root, scl=p["I2C_SCL"], sda=p["I2C_SDA"], adc=p["ADC_BAT"],
        sck=p["SPI_SCK"], mosi=p["SPI_MOSI"], miso=p["SPI_MISO"], cs=p["SD_CS"],
        files=[remote for _local, remote in flash.firmware_files(True)])


def apply_code(role=None, label=None, clear=False, root="/"):
    return APPLY_CODE.format(root=root, role=role, label=label, clear=bool(clear))


# ---------------------------------------------------------------------------
# Host side
# ---------------------------------------------------------------------------

def local_hashes():
    """{remote_path: set of acceptable sha256 hex digests}. A git checkout on
    Windows may hold CRLF line endings while the band holds whatever was
    pushed, so the LF and CRLF forms of each file are both accepted."""
    out = {}
    for local, remote in flash.firmware_files(True):
        with open(os.path.join(ROOT, local), "rb") as f:
            data = f.read()
        lf = data.replace(b"\r\n", b"\n")
        crlf = lf.replace(b"\n", b"\r\n")
        out[remote] = {hashlib.sha256(x).hexdigest() for x in (data, lf, crlf)}
    return out


def git_tag():
    """(tag, describe): the tag this checkout is exactly on, or None."""
    def git(*args):
        try:
            r = subprocess.run(["git"] + list(args), cwd=ROOT, capture_output=True,
                               text=True, timeout=10)
            return r.stdout.strip() if r.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""
    tag = git("describe", "--tags", "--exact-match")
    return (tag or None), (git("describe", "--tags", "--always", "--dirty") or "unknown")


def ap_name(device_type, device_id):
    """Same rule as lib/provisioning.ssid_for: OM-<dev>-<id-tail>."""
    prefix = (device_type or "flexgrid") + "-"
    tail = device_id[len(prefix):] if device_id.startswith(prefix) else device_id
    return "OM-{}-{}".format(device_type or "flexgrid", tail)


def evaluate(report, opts, expected_hashes, tag_info):
    """Turn the band's report into a list of (level, topic, message) where
    level is ok / warn / fail. Pure, so it is tested without a band."""
    items = []

    def ok(topic, msg):
        items.append(("ok", topic, msg))

    def warn(topic, msg):
        items.append(("warn", topic, msg))

    def fail(topic, msg):
        items.append(("fail", topic, msg))

    # Identity and settings
    dev = report.get("device_id")
    if not report.get("settings_present") or report.get("settings_unreadable"):
        fail("settings", "no readable settings file: boot the band once so it makes its id and setup password")
    elif not dev:
        fail("settings", "settings file has no device id: boot the band once")
    else:
        ok("settings", "band {}".format(dev))

    # Factory state
    problems = []
    if report.get("has_ssid"):
        problems.append("a Wi-Fi name is saved")
    if report.get("has_password"):
        problems.append("a Wi-Fi password is saved")
    if report.get("joined_ok"):
        problems.append("it has joined a network before (wifi_joined_ok)")
    if report.get("has_last_ssid"):
        problems.append("it remembers a network it tried (last_ssid)")
    if report.get("bak"):
        problems.append("log.txt.bak exists")
    log = report.get("log") or {}
    if log.get("net_lines"):
        problems.append("log.txt names a Wi-Fi network ({} line(s))".format(log["net_lines"]))
    if log.get("unreadable"):
        problems.append("log.txt could not be read")
    if problems:
        fail("factory state", "not in factory state: " + "; ".join(problems)
             + " (run wipe.py, or --clear-logs for the logs)")
    else:
        extra = " (log.txt holds boot lines only)" if log.get("present") else ""
        ok("factory state", "no Wi-Fi name or password saved, no join history, no network names in the logs" + extra)

    # Setup password
    psk = report.get("psk") or ""
    if len(psk) == PSK_LENGTH and set(psk) <= set(PSK_CHARSET):
        ok("setup password", "{} characters".format(PSK_LENGTH))
    elif not psk:
        fail("setup password", "none yet: boot the band once so it makes one")
    else:
        fail("setup password", "malformed (length {}, expected {} from the allowed set)".format(
            len(psk), PSK_LENGTH))

    # I2C: OLED and IMU
    i2c = report.get("i2c")
    if i2c is None:
        fail("OLED", "the I2C bus did not answer; check the motion sensor and the I2C solder joints")
    else:
        found = ", ".join("0x{:02X}".format(a) for a in i2c) or "nothing"
        if OLED_ADDR in i2c:
            ok("OLED", "answers at 0x{:02X} (I2C scan: {})".format(OLED_ADDR, found))
        elif opts.get("require_screen"):
            fail("OLED", "not found at 0x{:02X} (I2C scan: {}) and --require-screen was given".format(
                OLED_ADDR, found))
        else:
            warn("OLED", "not fitted (nothing at 0x{:02X}; I2C scan: {}). Expected: kit bands ship without "
                 "the screen. The sticker is the only place the setup password lives, so print it and "
                 "keep it with the band. The light blinks blue twice then pauses in setup; that is normal".format(
                     OLED_ADDR, found))
        if any(a in i2c for a in IMU_ADDRS):
            ok("IMU", "answers")
        else:
            warn("IMU", "not found at 0x36, 0x37, 0x68 or 0x69; orientation data will be missing")

    # Battery
    vbat = report.get("vbat")
    minv = opts["min_battery"]
    if vbat is None:
        fail("battery", "could not read the battery voltage")
    elif vbat < NO_BATTERY_V:
        fail("battery", "no battery detected ({:.2f} V)".format(vbat))
    elif vbat < minv:
        fail("battery", "low: {:.2f} V is under {:.2f} V; charge it before shipping".format(vbat, minv))
    else:
        ok("battery", "{:.2f} V".format(vbat))

    # SD card
    if report.get("sd"):
        ok("SD card", "mounts")
    elif opts["require_sd"]:
        fail("SD card", "no card mounts and --require-sd was given")
    else:
        warn("SD card", "no card mounts; RECORD will say NO SD CARD and stay on Wi-Fi")

    # Firmware identity
    hashes = report.get("hashes") or {}
    missing = sorted(p for p, h in hashes.items() if h is None)
    differ = sorted(p for p, h in hashes.items()
                    if h is not None and p in expected_hashes and h not in expected_hashes[p])
    absent = sorted(p for p in expected_hashes if p not in hashes)
    if missing or absent:
        fail("firmware", "files missing on the band: " + ", ".join(missing + absent))
    elif differ:
        fail("firmware", "differs from this checkout: " + ", ".join(differ) + " (re-flash with flash.py --factory)")
    else:
        ok("firmware", "all {} files match this checkout".format(len(expected_hashes)))
    tag, describe = tag_info
    if tag:
        ok("release", "checkout is on tag {}".format(tag))
    elif opts["require_tag"]:
        fail("release", "checkout is not on a git tag ({}); flash from the tagged release".format(describe))
    else:
        warn("release", "checkout is not on a git tag ({}); ship only from the tagged release".format(describe))
    return items


def sticker_lines(report):
    dev = report.get("device_id") or ""
    lines = [
        "OpenMuscle FlexGrid V4",
        "Band             " + dev,
    ]
    if report.get("role"):
        lines.append("Role             " + report["role"])
    if report.get("label"):
        lines.append("Label            " + report["label"])
    lines.append("Setup Wi-Fi      " + ap_name(report.get("device_type"), dev))
    lines.append("Setup password   " + (report.get("psk") or ""))
    return lines


def render(port, items):
    mark = {"ok": "[ok]  ", "warn": "[warn]", "fail": "[FAIL]"}
    lines = ["FlexGrid V4 kit check on {}".format(port)]
    for level, topic, msg in items:
        lines.append("  {} {}: {}".format(mark[level], topic, msg))
    return lines


# ---------------------------------------------------------------------------
# Transport: mpremote exec
# ---------------------------------------------------------------------------

class BandError(Exception):
    pass


def mpremote_exec(port, code, timeout=60):
    """Run `code` on the band with mpremote and return its stdout."""
    cmd = [sys.executable, "-m", "mpremote", "connect", port, "exec", code]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        raise BandError("could not run mpremote: {}".format(e))
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        raise BandError("mpremote failed on {}: {}".format(port, " | ".join(tail) or "no output"))
    return r.stdout


def parse_report(stdout):
    for line in stdout.splitlines():
        if line.startswith(MARKER):
            try:
                return json.loads(line[len(MARKER):])
            except ValueError:
                break
    raise BandError("the band did not return a report; output was: {!r}".format(stdout[:200]))


def parse_args(argv):
    opts = {"port": None, "role": None, "label": None, "clear_logs": False,
            "min_battery": DEFAULT_MIN_BATTERY_V, "require_sd": False,
            "require_screen": False, "require_tag": False, "reset": False}
    it = iter(argv)
    for a in it:
        if a == "--role":
            opts["role"] = next(it, None)
            if opts["role"] not in ROLES:
                raise ValueError("--role must be one of: " + ", ".join(ROLES))
        elif a == "--label":
            opts["label"] = next(it, None)
            if not opts["label"] or len(opts["label"]) > LABEL_MAX \
                    or not opts["label"].isprintable():
                raise ValueError("--label must be 1 to {} printable characters".format(LABEL_MAX))
        elif a == "--min-battery":
            try:
                opts["min_battery"] = float(next(it, ""))
            except ValueError:
                raise ValueError("--min-battery needs a number of volts")
        elif a == "--clear-logs":
            opts["clear_logs"] = True
        elif a == "--require-sd":
            opts["require_sd"] = True
        elif a == "--require-screen":
            opts["require_screen"] = True
        elif a == "--require-tag":
            opts["require_tag"] = True
        elif a == "--reset":
            opts["reset"] = True
        elif a.startswith("--"):
            raise ValueError("unknown option " + a)
        elif opts["port"] is None:
            opts["port"] = a
        else:
            raise ValueError("only one COM port, got an extra argument: " + a)
    if not opts["port"]:
        raise ValueError("give the band's COM port")
    return opts


def run_check(opts, runner=mpremote_exec, expected=None, tag_info=None, out=print):
    """Do the check; returns the exit status. `runner(port, code)` returns the
    band's stdout (swapped for a fake in tests)."""
    port = opts["port"]
    expected = local_hashes() if expected is None else expected
    tag_info = git_tag() if tag_info is None else tag_info
    try:
        if opts["role"] or opts["label"] or opts["clear_logs"]:
            applied = parse_report(runner(port, apply_code(
                opts["role"], opts["label"], opts["clear_logs"])))
            if applied.get("no_settings"):
                out("note: no settings file yet, so the role/label could not be set; "
                    "boot the band once and run again")
        report = parse_report(runner(port, probe_code()))
    except BandError as e:
        out("ERROR: {}".format(e))
        return 2
    items = evaluate(report, opts, expected, tag_info)
    for line in render(port, items):
        out(line)
    failures = [i for i in items if i[0] == "fail"]
    if failures:
        out("FAIL ({} problem{})".format(len(failures), "" if len(failures) == 1 else "s"))
        psk = report.get("psk") or ""
        if len(psk) == PSK_LENGTH and set(psk) <= set(PSK_CHARSET):
            # A screenless band has no other way to show its password, so a
            # band that is still charging or has already joined a network
            # must stay readable. This is NOT sticker text.
            out("")
            out("Setup name and password (read only; this band FAILED the check, "
                "do not print a sticker from it):")
            dev = report.get("device_id") or ""
            out("  Setup Wi-Fi      " + ap_name(report.get("device_type"), dev))
            out("  Setup password   " + psk)
        status = 1
    else:
        out("PASS")
        out("")
        out("Sticker text:")
        for line in sticker_lines(report):
            out("  " + line)
        status = 0
    if opts["reset"]:
        try:
            runner(port, "import machine\nmachine.reset()\n")
        except BandError:
            pass          # the USB link drops as the band reboots
        out("Band rebooted.")
    return status


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        opts = parse_args(argv)
    except ValueError as e:
        print("kit_check: {}\n".format(e))
        print(__doc__)
        return 2
    return run_check(opts)


if __name__ == "__main__":
    sys.exit(main())
