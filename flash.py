"""One-shot flash script for a fresh V4 board.

Steps performed for the given COM port (the ROM bootloader DFU port):
    1. esptool erase_flash + write_flash of MicroPython 1.28.0
    2. Wait for the board to re-enumerate as USB-Serial-JTAG (a different COM)
    3. Push all V4 firmware files + lib/ssd1306.py (and config/settings.json
       unless --factory is given)
    4. machine.reset() the device

Usage:
    python flash.py [--factory] <bootloader_com> [<runtime_com>]
        --factory:        push NO config/settings.json. The erase in step 1
                          already wiped the board, so it boots with no Wi-Fi
                          credentials, mints a fresh device id and setup
                          password, and shows WIFI SETUP on the OLED. Use this
                          for any board that will be handed to someone else;
                          the user then gives it their own Wi-Fi from a phone
                          or the PC app. No config/settings.json is needed.
        <bootloader_com>: the COM port the board shows up on while in DFU mode
                          (VID:PID 303a:1001 in `mpremote connect list`)
        <runtime_com>:    optional. The COM port the board re-enumerates as
                          after MicroPython boots (VID:PID 303a:4001).
                          If omitted, the script will scan and pick the new one.

Pre-requisites:
    - Board plugged in. On the as-built V4 boards the ESP32-S3 native USB shows
      up as VID:PID 303a:1001 while blank/in-ROM; esptool's default_reset auto-
      enters the download mode over that interface, so NO manual BOOT+RESET DFU
      dance is needed. After MicroPython boots it re-enumerates as 303a:4001.
    - Without --factory: config/settings.json present (copy
      config/settings.example.json; the example has blank Wi-Fi credentials,
      so the board still boots to WIFI SETUP unless you fill them in)
    - MicroPython firmware downloaded to firmware/ESP32_GENERIC_S3-*.bin
      (or set MICROPYTHON_BIN env var to a custom path)
    - esptool + mpremote installed in the same interpreter that runs this
      script (tested with esptool 4.8.1 and mpremote 1.25.0).

Re-flashing always erases the whole chip, so the board gets a NEW device id
(flexgrid-<6hex>) and a NEW setup password (PSK). A band that was already in
use appears as a different device afterwards. To clear a used band WITHOUT
re-flashing (settings.json, log.txt, log.txt.bak), run wipe.py.
"""
import os
import subprocess
import sys
import time
import glob

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None


# ESP32-S3 native-USB identifiers. In the ROM download mode the chip enumerates
# as 303a:1001 (USB-Serial-JTAG); once MicroPython boots it re-enumerates as
# 303a:4001 (TinyUSB CDC). esptool can only reach the ROM, so a board already
# running MicroPython must be bounced into the bootloader first.
ESP_VID = 0x303A
PID_BOOTLOADER = 0x1001
PID_RUNTIME = 0x4001


HERE = os.path.dirname(os.path.abspath(__file__))

# Use the same interpreter that launched this script for the esptool/mpremote
# subprocesses. Bare "python" is not always on PATH on Windows (the py launcher
# is), and mixing interpreters risks a different site-packages without esptool.
PY = sys.executable


def find_micropython_bin():
    env = os.environ.get("MICROPYTHON_BIN")
    if env and os.path.exists(env):
        return env
    matches = glob.glob(os.path.join(HERE, "firmware", "ESP32_GENERIC_S3*.bin"))
    if matches:
        return sorted(matches)[-1]
    raise SystemExit(
        "No MicroPython firmware found. Set MICROPYTHON_BIN env var, or place a "
        "downloaded .bin under firmware/ . Get the latest at "
        "https://micropython.org/download/ESP32_GENERIC_S3/"
    )


def list_serial_ports():
    """Return a set of currently-enumerated COM device strings."""
    try:
        out = subprocess.check_output(
            [PY, "-m", "mpremote", "connect", "list"],
            text=True, stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as e:
        out = e.output or ""
    ports = set()
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        first = line.split()[0]
        if first.startswith("COM") or first.startswith("/dev/"):
            ports.add(first)
    return ports


def run(cmd, check=True, quiet_stderr=False):
    print("$ " + " ".join(str(c) for c in cmd))
    # quiet_stderr swallows a subprocess' stderr. Used for the final
    # machine.reset(): on native-USB S3 boards the reset re-enumerates the USB
    # device mid-command, so mpremote's follow() reliably throws a
    # ClearCommError traceback even though the reset succeeded. That noise looks
    # like a failure; hide it since the reset is best-effort anyway (check=False).
    r = subprocess.run(cmd, stderr=(subprocess.DEVNULL if quiet_stderr else None))
    if check and r.returncode != 0:
        raise SystemExit("Command failed: {}".format(cmd))


def _port_pid(com):
    """Return the USB PID (int) for a COM port, or None if unknown."""
    if list_ports is None:
        return None
    for p in list_ports.comports():
        if p.device.upper() == com.upper():
            return p.pid
    return None


def _bootloader_ports():
    """Set of COM devices currently enumerated as the S3 ROM bootloader."""
    if list_ports is None:
        return set()
    return {p.device for p in list_ports.comports()
            if p.vid == ESP_VID and p.pid == PID_BOOTLOADER}


def _touch_1200(com):
    """1200-baud touch: tell a running MicroPython board to reboot into its ROM
    download mode. These V4 boards have no BOOT/GPIO0 button (the silkscreen
    BOOT is wired to EN), so this line-coding signal is the only buttonless way
    into the bootloader once MicroPython is installed."""
    s = serial.Serial(com, 1200)
    time.sleep(0.1)
    s.dtr = False
    s.rts = False
    time.sleep(0.1)
    s.dtr = True
    s.rts = True
    time.sleep(0.1)
    s.close()


def ensure_bootloader(com):
    """Return a COM port sitting in the ROM bootloader, bouncing the board there
    via a 1200-baud touch if it is currently running MicroPython. The ROM window
    is transient, so we poll fast and hand the port straight to esptool with no
    intervening delay."""
    pid = _port_pid(com)
    if pid == PID_BOOTLOADER:
        return com
    if serial is None:
        raise SystemExit(
            "Port {} is not in the ROM bootloader (pid={}). Install pyserial so "
            "this script can 1200-baud-touch it into download mode, or do it "
            "manually.".format(com, hex(pid) if pid else pid))
    print("Port {} is running MicroPython (pid={}); 1200-baud touching it into "
          "the bootloader...".format(com, hex(pid) if pid else pid))
    before = _bootloader_ports()
    try:
        _touch_1200(com)
    except Exception as e:
        raise SystemExit("1200-baud touch failed on {}: {}".format(com, e))
    # Poll fast; the ROM download mode can revert if left idle.
    for _ in range(40):
        time.sleep(0.25)
        new = _bootloader_ports() - before
        if new:
            target = sorted(new)[0]
            print("  bootloader appeared on {}".format(target))
            return target
    raise SystemExit(
        "No ROM-bootloader port appeared after the touch. Re-run with the board "
        "freshly plugged in, or pass the 303a:1001 COM explicitly.")


# ---------------------------------------------------------------------------
# Raw-REPL file push over pyserial.
#
# mpremote's per-file `fs cp` was the flakiest part of this flow: on the S3
# native-USB CDC it intermittently fails "could not enter raw repl", and on a
# board whose CDC has been re-enumerated many times it can wedge entirely. This
# path speaks the MicroPython raw-REPL protocol directly over one serial
# handle: Ctrl-C to interrupt, Ctrl-A to enter raw REPL (no soft reset, so a
# half-installed app is never launched), chunked writes, then machine.reset().
# One handle for the whole push means one raw-REPL handshake instead of 20.
# ---------------------------------------------------------------------------
def _drain(s, ms=250):
    end = time.time() + ms / 1000.0
    buf = bytearray()
    while time.time() < end:
        b = s.read(s.in_waiting or 1)
        if b:
            buf.extend(b)
        else:
            time.sleep(0.01)
    return bytes(buf)


def _enter_raw_repl(s):
    for _ in range(4):
        s.write(b"\r\x03")
        time.sleep(0.08)
    _drain(s, 250)
    for _ in range(5):
        s.write(b"\r\x01")
        time.sleep(0.4)
        if b"raw REPL" in _drain(s, 500):
            return
        s.write(b"\x03\x03")
        time.sleep(0.3)
        _drain(s, 200)
    raise SystemExit("Could not enter raw REPL over {} after retries".format(s.port))


def _exec_raw(s, code):
    s.write(code.encode("utf-8"))
    s.write(b"\x04")
    buf = bytearray()
    end = time.time() + 8.0
    while time.time() < end:
        b = s.read(s.in_waiting or 1)
        if b:
            buf.extend(b)
            if buf.count(b"\x04") >= 2 and buf.endswith(b"\x04>"):
                break
        else:
            time.sleep(0.01)
    raw = bytes(buf)
    if raw[:2] != b"OK":
        raise SystemExit("raw REPL did not OK: {!r}".format(raw[:80]))
    body = raw[2:]
    if body.endswith(b"\x04>"):
        body = body[:-2]
    parts = body.split(b"\x04")
    return (parts[0] if parts else b""), (parts[1] if len(parts) >= 2 else b"")


def _ensure_remote_dir(s, remote):
    parts = remote.lstrip("/").split("/")
    if len(parts) <= 1:
        return
    d = "/" + "/".join(parts[:-1])
    _, err = _exec_raw(s, (
        "import uos\n"
        "try:\n uos.stat('{0}')\n"
        "except OSError:\n uos.mkdir('{0}')\n"
        "print('dirok')\n").format(d))
    if err.strip():
        raise SystemExit("mkdir {} failed: {!r}".format(d, err[:120]))


def _push_file_raw(s, local_abs, remote):
    with open(local_abs, "rb") as f:
        data = f.read()
    print("  {} -> {} ({} bytes)".format(os.path.basename(local_abs), remote, len(data)))
    _ensure_remote_dir(s, remote)
    _, err = _exec_raw(s, "f=open('{}','wb')\n".format(remote))
    if err.strip():
        raise SystemExit("open {} failed: {!r}".format(remote, err[:120]))
    chunk = 256
    for i in range(0, len(data), chunk):
        _, err = _exec_raw(s, "f.write({!r})\n".format(data[i:i + chunk]))
        if err.strip():
            raise SystemExit("write {}@{} failed: {!r}".format(remote, i, err[:120]))
    out, err = _exec_raw(s, "f.close()\nimport uos\nprint(uos.stat('{}')[6])\n".format(remote))
    if err.strip():
        raise SystemExit("close/stat {} failed: {!r}".format(remote, err[:120]))
    if int(out.strip()) != len(data):
        raise SystemExit("size mismatch on {}: {} != {}".format(remote, out.strip(), len(data)))


def push_files_raw(runtime_com, files_to_push):
    """Open one serial handle and push every file over raw REPL, then reset."""
    if serial is None:
        raise SystemExit("pyserial required for the raw-REPL push; pip install pyserial")
    s = serial.Serial(runtime_com, 115200, timeout=2)
    # Native-USB CDC only accepts host input with DTR asserted; never pulse RTS
    # (it maps to EN and would reset + re-enumerate mid-push).
    s.rts = False
    s.dtr = True
    time.sleep(0.3)
    try:
        _enter_raw_repl(s)
        for local, remote in files_to_push:
            _push_file_raw(s, os.path.join(HERE, local), "/" + remote.lstrip("/"))
        print("  all files confirmed on device; resetting")
        s.write(b"import machine\r\n")
        s.write(b"machine.reset()\r\n")
        s.write(b"\x04")
        time.sleep(0.5)
    finally:
        s.close()


def parse_args(argv):
    """Returns (factory, positional_args). Unknown --flags are an error so a
    typo such as --factroy cannot silently push a settings file."""
    factory = False
    positional = []
    for a in argv:
        if a == "--factory":
            factory = True
        elif a.startswith("--"):
            raise SystemExit("Unknown option {}. See the top of flash.py.".format(a))
        else:
            positional.append(a)
    return factory, positional


def firmware_files(factory):
    """(local, remote) pairs to push. main.py goes last (see STEP 3 note);
    config/settings.json is left out entirely in factory mode."""
    files = [
        ("flexgrid.py",                   "flexgrid.py"),
        ("lib/pinmap.py",                 "lib/pinmap.py"),
        ("lib/settings_manager.py",       "lib/settings_manager.py"),
        ("lib/logger.py",                 "lib/logger.py"),
        ("lib/power_manager.py",          "lib/power_manager.py"),
        ("lib/sensor_matrix.py",          "lib/sensor_matrix.py"),
        ("lib/display_manager.py",        "lib/display_manager.py"),
        ("lib/screens.py",                "lib/screens.py"),
        ("lib/menu_manager.py",           "lib/menu_manager.py"),
        ("lib/ssd1306.py",                "lib/ssd1306.py"),
        ("lib/network_manager.py",        "lib/network_manager.py"),
        ("lib/status_led.py",             "lib/status_led.py"),
        ("lib/subscribers.py",            "lib/subscribers.py"),
        ("lib/commands.py",               "lib/commands.py"),
        ("lib/discovery.py",              "lib/discovery.py"),
        ("lib/imu.py",                    "lib/imu.py"),
        ("lib/sd_logger.py",              "lib/sd_logger.py"),
        ("lib/provisioning.py",           "lib/provisioning.py"),
    ]
    if not factory:
        files.append(("config/settings.json", "config/settings.json"))
    files.append(("boot.py", "boot.py"))
    files.append(("main.py", "main.py"))
    return files


def main():
    factory, args = parse_args(sys.argv[1:])
    if not args:
        print(__doc__)
        return 1
    bootloader_com = ensure_bootloader(args[0])
    runtime_com = args[1] if len(args) > 1 else None

    bin_path = find_micropython_bin()
    settings_path = os.path.join(HERE, "config", "settings.json")
    if factory:
        print("FACTORY mode: config/settings.json will NOT be pushed; the board "
              "will boot to WIFI SETUP.")
    elif not os.path.exists(settings_path):
        raise SystemExit(
            "config/settings.json missing. Either run with --factory (board boots "
            "to WIFI SETUP, no file needed), or copy config/settings.example.json "
            "to config/settings.json first."
        )

    # Snapshot every serial port present BEFORE we flash, so STEP 2 can pick out
    # the one truly-new runtime port by set difference. Without this, any other
    # already-connected board (e.g. a second unit on another COM) would match the
    # "new port" heuristic and we could push firmware to the WRONG board.
    ports_at_start = list_serial_ports()

    print("=" * 60)
    print("STEP 1: Erase + flash MicroPython on {}".format(bootloader_com))
    print("=" * 60)
    # esptool 4.x uses underscore subcommands (erase_flash / write_flash) and
    # underscore reset modes. Use usb_reset: it is the correct reset for the
    # ESP32-S3 native USB-Serial-JTAG and connects reliably across units, where
    # default_reset (classic DTR/RTS, emulated over native USB) intermittently
    # fails with a Windows "ClearCommError" on some boards. Keep the board in
    # the bootloader after erase (no_reset) so the next write_flash does not have
    # to cold-cycle, then hard_reset into MicroPython.
    run([PY, "-m", "esptool", "--chip", "esp32s3", "--port", bootloader_com,
         "--before", "usb_reset", "--after", "no_reset", "erase_flash"])
    run([PY, "-m", "esptool", "--chip", "esp32s3", "--port", bootloader_com,
         "--before", "usb_reset", "--after", "hard_reset", "write_flash",
         "0x0", bin_path])

    print()
    print("=" * 60)
    print("STEP 2: Wait for re-enumeration as USB-Serial-JTAG")
    print("=" * 60)
    if runtime_com is None:
        # The board re-enumerates from the 1001 bootloader to a 4001 runtime port
        # on a new COM number. The genuinely-new port is whatever is present now
        # that was NOT present before the flash (ports_at_start). This ignores any
        # other board that was already connected on its own COM.
        print("Scanning for new port (up to 20s)...")
        target = None
        for _ in range(40):
            time.sleep(0.5)
            new = sorted(list_serial_ports() - ports_at_start)
            if new:
                target = new[0]
                print("  found:", target)
                break
        if not target:
            raise SystemExit(
                "Did not detect a new re-enumerated port. If the board kept the "
                "same COM number, pass the runtime port explicitly as a 2nd arg."
            )
        runtime_com = target
    else:
        time.sleep(5)
        print("Using provided runtime port:", runtime_com)

    print()
    print("=" * 60)
    print("STEP 3: Push firmware files to {}".format(runtime_com))
    print("=" * 60)
    # IMPORTANT ordering: main.py is pushed LAST (boot.py just before it).
    # Each mpremote fs cp does a soft reset on connect. If main.py already
    # exists while later files are still missing, that soft reset re-runs a
    # half-installed app whose failing imports flood the console and make the
    # NEXT "enter raw repl" fail intermittently ("could not enter raw repl").
    # By writing every module first and main.py (the auto-run trigger) last,
    # no soft reset ever launches an incomplete app, so every push lands on a
    # clean REPL. See the board-3 recovery in BRINGUP notes.
    files_to_push = firmware_files(factory)
    # Push everything over a single raw-REPL serial session (see push_files_raw).
    # This replaced the old per-file `mpremote fs cp` loop, which flaked with
    # "could not enter raw repl" on the S3 CDC and could wedge a board entirely.
    push_files_raw(runtime_com, files_to_push)

    print()
    print("=" * 60)
    print("STEP 4: reset (done inside the raw-REPL push)")
    print("=" * 60)

    print()
    if factory:
        print("Done. The board has no Wi-Fi credentials: within a few seconds the OLED")
        print("shows WIFI SETUP with its own network name and password. New device id")
        print("and setup password were generated by this flash.")
    else:
        print("Done. Give the board ~15 s to join Wi-Fi and listen for the")
        print("UDP broadcast announce on port 3140 to confirm:")
        print('  {} -c "import socket,json; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind((\'\',3140)); print(json.loads(s.recvfrom(4096)[0]))"'.format(os.path.basename(PY)))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
