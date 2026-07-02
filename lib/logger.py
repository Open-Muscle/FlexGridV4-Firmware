# lib/logger.py
#
# Logs to stdout (UART REPL) and optionally to a rotating file on flash.
# The persistent log survives reboots, which is the whole point: when the
# device unexpectedly resets mid-recording, the boot log of the NEW run
# can tell you what reset_cause the previous run terminated with.
#
# Persistence policy (revised for V4 rate parity, board #0192):
#   DBG  -> stdout only (NEVER persisted; would dominate flash writes
#                        for the periodic rate / wdt-canary / battery
#                        status spam that caused 1-second asyncio
#                        stalls and halved the emit rate)
#   INF  -> persisted, NOT flushed (flush() at every write was the
#                        actual ~50-200ms flash sync hit; we accept
#                        the loss of the last few INF lines on hard
#                        reset in exchange for sustained 20+ Hz)
#   WRN  -> persisted AND flushed (rare; we want these durable)
#   ERR  -> persisted AND flushed (rare; we want these durable)
#
# Storage budget: /log.txt is capped at LOG_MAX_BYTES (default 16 KB,
# down from 30 KB now that we drop DBG from persistent). When full,
# rotated to /log.txt.bak (one generation). Runtime rotation: the size
# check runs every _ROTATE_CHECK_EVERY writes so the file no longer
# grows unbounded between reboots (the original bug: rotation only
# ran at init_persistent(), so a long-running device hit 183 KB+ of
# log file and every subsequent flush was slower and slower).

import time

try:
    import uos as _os
except ImportError:
    import os as _os

DEBUG = True  # flip to False to also silence DBG to stdout

LOG_PATH = "/log.txt"
BAK_PATH = "/log.txt.bak"
LOG_MAX_BYTES = 16 * 1024
_ROTATE_CHECK_EVERY = 32   # check + maybe-rotate every N writes

_log_fp = None
_persist_enabled = False
_boot_ticks_ms = time.ticks_ms()
_writes_since_check = 0


def _prefix(level):
    return "[{}]".format(level)


def _log_size():
    try:
        return _os.stat(LOG_PATH)[6]
    except OSError:
        return 0


def _rotate_if_needed():
    """If log is over the size cap, rotate it to .bak. One generation only.
    Closes the current file handle, renames, opens a fresh handle."""
    global _log_fp
    if _log_size() <= LOG_MAX_BYTES:
        return
    try:
        if _log_fp is not None:
            try:
                _log_fp.close()
            except Exception:
                pass
            _log_fp = None
        try:
            _os.remove(BAK_PATH)
        except OSError:
            pass
        _os.rename(LOG_PATH, BAK_PATH)
        # Reopen fresh.
        _log_fp = open(LOG_PATH, "a")
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


def _write_persistent(line, do_flush):
    """Append a line to the on-flash log. Flushes only on demand (WRN/ERR),
    so the high-frequency INF path does not pay per-line flash-sync cost.
    Runs a rotation check every _ROTATE_CHECK_EVERY writes."""
    global _writes_since_check
    if not _persist_enabled or _log_fp is None:
        return
    try:
        _log_fp.write(line + "\n")
        if do_flush:
            _log_fp.flush()
        _writes_since_check += 1
        if _writes_since_check >= _ROTATE_CHECK_EVERY:
            _writes_since_check = 0
            _rotate_if_needed()
    except Exception:
        # Disk full / FS error -- swallow silently to avoid recursive logging.
        pass


def _emit(level, msg, persist, flush):
    line = "{} {}".format(_prefix(level), msg)
    print(line)
    if persist:
        _write_persistent(line, flush)


def debug(msg):
    # DBG never persists. Use for high-frequency telemetry that you want
    # on the serial monitor but not on flash (rate, wdt-canary, periodic
    # battery status, per-frame trace).
    if DEBUG:
        _emit("DBG", msg, persist=False, flush=False)


def info(msg):
    # Persisted but NOT flushed per line. Reserve for infrequent events
    # (boot, network up/down, subscribe add/drop, config changes); avoid
    # for periodic telemetry.
    _emit("INF", msg, persist=True, flush=False)


def warn(msg):
    # Persisted AND flushed; durable across hard reset.
    _emit("WRN", msg, persist=True, flush=True)


def error(msg):
    # Persisted AND flushed; durable across hard reset.
    _emit("ERR", msg, persist=True, flush=True)


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
