"""One-shot flash script for a fresh V4 board.

Steps performed for the given COM port (the ROM bootloader DFU port):
    1. esptool erase_flash + write_flash of MicroPython 1.28.0
    2. Wait for the board to re-enumerate as USB-Serial-JTAG (a different COM)
    3. Push all V4 firmware files + lib/ssd1306.py + config/settings.json
    4. machine.reset() the device

Usage:
    python flash.py <bootloader_com> [<runtime_com>]
        <bootloader_com>: the COM port the board shows up on while in DFU mode
                          (VID:PID 303a:1001 in `mpremote connect list`)
        <runtime_com>:    optional. The COM port the board re-enumerates as
                          after MicroPython boots (VID:PID 303a:4001).
                          If omitted, the script will scan and pick the new one.

Pre-requisites:
    - Board in DFU mode (BOOT held + RESET tapped + BOOT held 1s more + released)
    - Real Wi-Fi credentials present at config/settings.json
      (copy from config/settings.example.json and edit)
    - MicroPython firmware downloaded to firmware/ESP32_GENERIC_S3-*.bin
      (or set MICROPYTHON_BIN env var to a custom path)
"""
import os
import subprocess
import sys
import time
import glob


HERE = os.path.dirname(os.path.abspath(__file__))


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
            ["python", "-m", "mpremote", "connect", "list"],
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


def run(cmd, check=True):
    print("$ " + " ".join(str(c) for c in cmd))
    r = subprocess.run(cmd)
    if check and r.returncode != 0:
        raise SystemExit("Command failed: {}".format(cmd))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    bootloader_com = sys.argv[1]
    runtime_com = sys.argv[2] if len(sys.argv) > 2 else None

    bin_path = find_micropython_bin()
    settings_path = os.path.join(HERE, "config", "settings.json")
    if not os.path.exists(settings_path):
        raise SystemExit(
            "config/settings.json missing. Copy config/settings.example.json and "
            "fill in real wifi_ssid and wifi_password before running."
        )

    print("=" * 60)
    print("STEP 1: Erase + flash MicroPython on {}".format(bootloader_com))
    print("=" * 60)
    run(["python", "-m", "esptool", "--chip", "esp32s3", "--port", bootloader_com,
         "--before", "usb-reset", "erase-flash"])
    run(["python", "-m", "esptool", "--chip", "esp32s3", "--port", bootloader_com,
         "--before", "usb-reset", "write-flash", "0", bin_path])

    print()
    print("=" * 60)
    print("STEP 2: Wait for re-enumeration as USB-Serial-JTAG")
    print("=" * 60)
    if runtime_com is None:
        ports_before = set()  # the bootloader port disappears after reset
        print("Scanning for new port (up to 20s)...")
        target = None
        for _ in range(40):
            time.sleep(0.5)
            ports_now = list_serial_ports()
            new = [p for p in ports_now if p != bootloader_com and p != "COM3"]  # skip TourBox
            if new:
                target = new[0]
                print("  found:", target)
                break
        if not target:
            raise SystemExit(
                "Did not detect re-enumerated port. Pass it explicitly as a 2nd arg."
            )
        runtime_com = target
    else:
        time.sleep(5)
        print("Using provided runtime port:", runtime_com)

    print()
    print("=" * 60)
    print("STEP 3: Push firmware files to {}".format(runtime_com))
    print("=" * 60)
    files_to_push = [
        ("boot.py",                       "boot.py"),
        ("main.py",                       "main.py"),
        ("flexgrid.py",                   "flexgrid.py"),
        ("lib/pinmap.py",                 "lib/pinmap.py"),
        ("lib/settings_manager.py",       "lib/settings_manager.py"),
        ("lib/logger.py",                 "lib/logger.py"),
        ("lib/power_manager.py",          "lib/power_manager.py"),
        ("lib/sensor_matrix.py",          "lib/sensor_matrix.py"),
        ("lib/display_manager.py",        "lib/display_manager.py"),
        ("lib/menu_manager.py",           "lib/menu_manager.py"),
        ("lib/ssd1306.py",                "lib/ssd1306.py"),
        ("lib/network_manager.py",        "lib/network_manager.py"),
        ("lib/status_led.py",             "lib/status_led.py"),
        ("lib/subscribers.py",            "lib/subscribers.py"),
        ("lib/commands.py",               "lib/commands.py"),
        ("lib/discovery.py",              "lib/discovery.py"),
        ("lib/imu.py",                    "lib/imu.py"),
        ("lib/sd_logger.py",              "lib/sd_logger.py"),
        ("config/settings.json",          "config/settings.json"),
    ]
    # mkdir lib and config first (one shot)
    run(["python", "-m", "mpremote", "connect", runtime_com,
         "fs", "mkdir", ":lib"], check=False)
    run(["python", "-m", "mpremote", "connect", runtime_com,
         "fs", "mkdir", ":config"], check=False)
    # Push in two batches so chained calls don't get too long for the shell
    for local, remote in files_to_push:
        run(["python", "-m", "mpremote", "connect", runtime_com,
             "fs", "cp", os.path.join(HERE, local), ":" + remote])

    print()
    print("=" * 60)
    print("STEP 4: machine.reset()")
    print("=" * 60)
    run(["python", "-m", "mpremote", "connect", runtime_com,
         "exec", "import machine; machine.reset()"], check=False)

    print()
    print("Done. Give the board ~20 s to join Wi-Fi, then listen for the")
    print("UDP broadcast beacon on port 3141 to confirm:")
    print('  python -c "import socket,json; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.bind((\'\',3141)); print(json.loads(s.recvfrom(4096)[0]))"')
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
