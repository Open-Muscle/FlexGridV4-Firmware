# lib/logger.py
#
# Logs to both stdout (UART REPL) and a rotating file on flash. The
# persistent log survives reboots, which is the whole point: when the
# device unexpectedly resets mid-recording, the boot log of the NEW run
# can tell you what reset_cause the previous run terminated with.
#
# Storage budget: /log.txt is capped at LOG_MAX_BYTES (default 30 KB).
# When full, it's renamed to /log.txt.bak and a fresh file starts. So at
# any time you have up to ~60 KB of recent history split across two files.
# This is cheap on a 16 MB flash (the most you'd ever burn is ~1 erase
# every few hours of continuous logging, well inside the device lifetime).

import time

try:
    import uos as _os
except ImportError:
    import os as _os

DEBUG = True  # flip to False to silence debug output

LOG_PATH = "/log.txt"
BAK_PATH = "/log.txt.bak"
LOG_MAX_BYTES = 30 * 1024

_log_fp = None
_persist_enabled = False
_boot_ticks_ms = time.ticks_ms()


def _prefix(level):
    return "[{}]".format(level)


def _log_size():
    try:
        return _os.stat(LOG_PATH)[6]
    except OSError:
        return 0


def _rotate_if_needed():
    """If log is over the size cap, rotate it to .bak. One generation only."""
    if _log_size() <= LOG_MAX_BYTES:
        return
    try:
        try:
            _os.remove(BAK_PATH)
        except OSError:
            pass
        _os.rename(LOG_PATH, BAK_PATH)
    except OSError as e:
        # Best-effort; if rotation fails we'll just keep appending until the
        # FS itself rejects writes.
        print("[WRN] log rotate failed:", e)


def init_persistent():
    """Open the on-flash log file. Safe to call repeatedly."""
    global _log_fp, _persist_enabled
    if _log_fp is not None:
        return
    try:
        _rotate_if_needed()
        _log_fp = open(LOG_PATH, "a")
        _persist_enabled = True
        _log_fp.write("\n=== boot @ ticks_ms={} ===\n".format(_boot_ticks_ms))
        _log_fp.flush()
    except Exception as e:
        # Don't crash boot if the FS is wedged; fall back to stdout-only.
        print("[ERR] persistent log init failed:", e)
        _persist_enabled = False


def _write_persistent(line):
    """Append a line to the on-flash log. Flushes so any subsequent crash
    leaves the disk-state in sync with what we last logged."""
    if not _persist_enabled or _log_fp is None:
        return
    try:
        _log_fp.write(line + "\n")
        _log_fp.flush()
    except Exception:
        # Disk full / FS error -- swallow silently to avoid recursive logging.
        pass


def _emit(level, msg):
    line = "{} {}".format(_prefix(level), msg)
    print(line)
    _write_persistent(line)


def debug(msg):
    if DEBUG:
        _emit("DBG", msg)


def info(msg):
    _emit("INF", msg)


def warn(msg):
    _emit("WRN", msg)


def error(msg):
    _emit("ERR", msg)


def tail(n_lines=40, include_bak=False):
    """Return the last n_lines of the log as a list of strings. Useful for
    `mpremote eval 'import logger; logger.tail()'` from a host." """
    lines = []
    paths = [LOG_PATH]
    if include_bak:
        paths = [BAK_PATH, LOG_PATH]
    for p in paths:
        try:
            with open(p, "r") as f:
                # Cheap-and-cheerful: read all then slice. /log.txt is small
                # (<= 30 KB) so this fits in MicroPython's heap fine.
                lines.extend(f.read().splitlines())
        except OSError:
            pass
    return lines[-n_lines:]
