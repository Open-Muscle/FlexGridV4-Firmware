# lib/sd_logger.py
# microSD session logger for offline sensor capture.
#
# V4 hardware adds an SPI-mode microSD socket (Molex 5033981892, LCSC C428492)
# wired to the SPI bus pins per the V4 README. This module mounts the card,
# spawns a session file with a header that matches the PC's training CSV
# format, and appends each scanned frame as a single line.
#
# Session file format: matches the PC's training CSV defined in
# OpenMuscle-Connect/docs/WIRE-FORMAT.md section 5:
#   timestamp,R0C0,R0C1,...,R3C14,label_0,label_1,label_2,label_3
#
# Labels: SD logging is sensor-only by default. If a label source is paired
# (LASK5 or manual), labels can be appended to each row by the recording
# loop in flexgrid.py; this module just writes whatever 4-element label list
# the caller provides.
#
# Failure modes (all non-fatal, all logged):
#   - SD not inserted: mount() returns False, recording is silently disabled.
#   - SD becomes full mid-session: write fails, session is auto-closed, a
#     warning is logged. The next attempt to start_session() will re-mount.
#   - Card pulled mid-session: same path as full; auto-close + warning.

import os
import time
import logger


# Where on the SD card sessions go. Created automatically on first session.
_SD_MOUNT = "/sd"
_SESSIONS_DIR = "/sd/sessions"


class SDLogger:
    def __init__(self, settings):
        self.enabled = settings.get("sd_logging", False)
        self.matrix_dims = (settings.get("matrix_cols", 15),
                            settings.get("matrix_rows", 4))
        self.mounted = False
        self.current_file = None
        self.current_path = None
        self.frames_written = 0
        self.session_id = None
        # Pre-build the CSV header once so we do not rebuild it for every session.
        cols, rows = self.matrix_dims
        cell_headers = ["R{}C{}".format(r, c) for r in range(rows) for c in range(cols)]
        self.csv_header = "timestamp," + ",".join(cell_headers) + ",label_0,label_1,label_2,label_3\n"

    def mount(self):
        """Mount the SD card under /sd. Best-effort; returns True on success.
        Safe to call again later if the card was inserted after boot."""
        if self.mounted:
            return True
        try:
            # MicroPython exposes machine.SDCard on ESP32 builds. We use SPI
            # mode per the V4 hardware (single-bit SPI through the standard
            # SPI bus + a CS line).
            from machine import SDCard, Pin
            import pinmap
            sd = SDCard(slot=2,
                        sck=Pin(pinmap.SPI_SCK),
                        mosi=Pin(pinmap.SPI_MOSI),
                        miso=Pin(pinmap.SPI_MISO),
                        cs=Pin(pinmap.SD_CS))
            os.mount(sd, _SD_MOUNT)
            self.mounted = True
            # Ensure the sessions directory exists.
            try:
                os.stat(_SESSIONS_DIR)
            except OSError:
                os.mkdir(_SESSIONS_DIR)
            logger.info("SD card mounted at {}".format(_SD_MOUNT))
            return True
        except Exception as e:
            logger.warn("SD mount failed (no card or wrong pinmap?): {}".format(e))
            self.mounted = False
            return False

    def start_session(self, session_id=None):
        """Open a new session file under /sd/sessions/<id>.csv and write
        the CSV header. Returns the session_id on success, None on failure
        (e.g. SD not mounted, enabled=False)."""
        if not self.enabled:
            return None
        if not self.mounted and not self.mount():
            return None
        sid = session_id or "session-{}".format(time.ticks_ms())
        path = "{}/{}.csv".format(_SESSIONS_DIR, sid)
        try:
            self.current_file = open(path, "w")
            self.current_file.write(self.csv_header)
            self.current_path = path
            self.session_id = sid
            self.frames_written = 0
            logger.info("SD session started: {}".format(path))
            return sid
        except Exception as e:
            logger.warn("SD start_session failed: {}".format(e))
            self.current_file = None
            self.current_path = None
            self.session_id = None
            return None

    def end_session(self):
        """Close the current session file cleanly. Idempotent."""
        if self.current_file is None:
            return
        try:
            self.current_file.flush()
            self.current_file.close()
            logger.info("SD session ended: {} ({} frames)".format(
                self.current_path, self.frames_written))
        except Exception as e:
            logger.warn("SD end_session close failed: {}".format(e))
        finally:
            self.current_file = None
            self.current_path = None
            self.session_id = None
            self.frames_written = 0

    def write_frame(self, matrix, labels=None):
        """Append one row to the session file: timestamp + flattened matrix
        + labels. If labels is None, writes 0,0,0,0 placeholders so the CSV
        always has a consistent column count (the PC trainer expects 4 label
        columns).

        matrix is column-major: matrix[col][row]. The flatten order is
        row-major per WIRE-FORMAT.md section 2.2:
            R0C0..R0C14, R1C0..R1C14, R2C0..R2C14, R3C0..R3C14
        """
        if self.current_file is None:
            return
        cols, rows = self.matrix_dims
        ts = time.ticks_ms()
        # Row-major flatten of column-major source.
        flat = [matrix[c][r] for r in range(rows) for c in range(cols)]
        if labels is None:
            labels = [0, 0, 0, 0]
        line = "{},{},{}\n".format(
            ts,
            ",".join(str(v) for v in flat),
            ",".join(str(v) for v in labels),
        )
        try:
            self.current_file.write(line)
            self.frames_written += 1
            # Flush every ~1 second of frames so a power loss does not throw
            # away the entire session. At 50 Hz that is roughly every 50
            # frames; we use a coarse modulus to keep the I/O cost bounded.
            if (self.frames_written % 50) == 0:
                self.current_file.flush()
        except Exception as e:
            logger.warn("SD write_frame failed (closing session): {}".format(e))
            # Disable to prevent a write-storm on a full / pulled card.
            self.end_session()

    def is_recording(self):
        return self.current_file is not None
