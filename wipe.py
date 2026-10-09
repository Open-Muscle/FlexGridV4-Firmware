"""Clear a used FlexGrid V4 band without re-flashing it.

Removes the three files that can carry a previous owner's data:
    /config/settings.json   Wi-Fi name + password, setup password (PSK), device id
    /log.txt                persistent log
    /log.txt.bak            rotated copy of the log

The firmware itself stays installed. On the next boot the band finds no
settings, mints a NEW device id and a NEW setup password, and shows WIFI SETUP
on its OLED, exactly like a freshly factory-flashed board.

Usage:
    python wipe.py <runtime_com> [--reset]
        <runtime_com>: the COM port of the band while it runs MicroPython
                       (VID:PID 303a:4001 in `mpremote connect list`).
        --reset:       reboot the band afterwards. Without it the band is left
                       idle at the REPL so the three files really stay gone;
                       unplug it (or press the reset button) to boot it. A
                       reboot creates a fresh, empty log.txt.

Needs pyserial. Uses the same raw-REPL path as flash.py.
"""
import sys
import time

import flash


# Runs ON the device. The logger keeps /log.txt open; close it first so the
# removal is not followed by a buffered write to a deleted file.
WIPE_CODE = (
    "import uos\n"
    "try:\n"
    " import logger\n"
    " fp = logger._log_fp\n"
    " if fp is not None:\n"
    "  fp.close()\n"
    " logger._log_fp = None\n"
    " logger._persist_enabled = False\n"
    "except Exception:\n"
    " pass\n"
    "gone = []\n"
    "for p in ('/config/settings.json', '/log.txt', '/log.txt.bak'):\n"
    " try:\n"
    "  uos.remove(p)\n"
    "  gone.append(p)\n"
    " except OSError:\n"
    "  pass\n"
    "print(','.join(gone))\n"
)


def wipe(runtime_com, reset=False):
    if flash.serial is None:
        raise SystemExit("pyserial required; pip install pyserial")
    s = flash.serial.Serial(runtime_com, 115200, timeout=2)
    # Same line handling as flash.push_files_raw: DTR up, never pulse RTS.
    s.rts = False
    s.dtr = True
    time.sleep(0.3)
    try:
        flash._enter_raw_repl(s)
        out, err = flash._exec_raw(s, WIPE_CODE)
        if err.strip():
            raise SystemExit("wipe failed on device: {!r}".format(err[:200]))
        removed = out.decode("utf-8", "replace").strip()
        print("Removed: {}".format(removed or "(nothing; already clean)"))
        if reset:
            s.write(b"import machine\r\n")
            s.write(b"machine.reset()\r\n")
            s.write(b"\x04")
            time.sleep(0.5)
            print("Band reset; it will boot to WIFI SETUP.")
        else:
            print("Band left idle. Unplug it, or run again with --reset to reboot it.")
    finally:
        s.close()


def main(argv):
    reset = "--reset" in argv
    args = [a for a in argv if not a.startswith("--")]
    unknown = [a for a in argv if a.startswith("--") and a != "--reset"]
    if unknown or len(args) != 1:
        print(__doc__)
        return 1
    wipe(args[0], reset=reset)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
