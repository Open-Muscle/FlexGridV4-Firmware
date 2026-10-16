# flexgrid.py
# OpenMuscle FlexGrid V4 main application.
#
# Differences from V3:
#   * No hardcoded destination IP. Sensor frames go only to hubs that have
#     subscribed via the command channel (lib/commands.py).
#   * Discovery: mDNS hostname plus a UDP broadcast beacon (lib/discovery.py).
#   * Three new peripherals: RGB status LED (lib/status_led.py), ICM-42688-P
#     IMU (lib/imu.py), microSD session logger (lib/sd_logger.py).
#   * Status snapshot now carries IMU values when present.
#   * Sensor frames carry a rolling seq number (added by network_manager).
#
# Async task layout:
#   sensor_loop           - scan + fan-out to subscribers   (~50 Hz)
#   display_loop          - OLED render                     (~15 Hz)
#   menu_loop             - button poll                      (20 Hz)
#   status_loop           - refresh device_status + IMU      (1 / 5s)
#   announce_loop         - mDNS + UDP beacon                (1 Hz)
#   subscriber_prune_loop - drop subscribers past heartbeat  (1 Hz)
#   status_led_loop       - RGB LED animation                (30 Hz)
#   gc_loop               - periodic gc.collect()            (0.5 Hz)
#
# Setup mode (no Wi-Fi credentials) runs a smaller set: setup_led_loop (the
# blue setup blink) and setup_screen_loop (network name + password on the
# OLED) next to the provisioning HTTP server.
#
# The command server (lib/commands.py) listens on cmd_port and accepts hubs
# via asyncio.start_server in its own background tasks.

import asyncio
import gc
import machine
import time
import uos
from machine import Pin
import logger
import pinmap

from settings_manager import SettingsManager
from sensor_matrix    import SensorMatrix
from display_manager  import DisplayManager
from menu_manager     import MenuManager
from network_manager  import NetworkManager, record_join_failure, record_join_success
from power_manager    import PowerManager
from status_led       import StatusLed
from subscribers      import Subscribers
from discovery        import Discovery
from commands         import CommandServer, build_handlers
import usb_console
from imu              import IMU
from sd_logger        import SDLogger
import provisioning
import screens


# Reset-cause names, built from the named machine.* constants instead of
# integer literals so we stay correct across MicroPython builds whose
# integer values differ. Audit-pass fix (board #0189 / #0191): the old
# dict had `6: "BROWNOUT"` which is wrong on ESP32-S3 (brownout surfaces
# as PWRON_RESET=1 here, since the brownout detector reboots through the
# same path; if you see PWRON_RESET mid-recording, suspect a Wi-Fi-TX-burst
# brownout). Using attrgetter-style lookup also future-proofs against
# value shifts between MicroPython versions.
def _build_reset_causes():
    names = ("PWRON_RESET", "HARD_RESET", "WDT_RESET",
             "DEEPSLEEP_RESET", "SOFT_RESET", "BROWNOUT_RESET")
    pretty = {
        "PWRON_RESET":     "POWER_ON",   # cold boot OR brownout on ESP32-S3
        "HARD_RESET":      "HARD",       # external RST line / chip enable
        "WDT_RESET":       "WDT",        # watchdog timeout, task got stuck
        "DEEPSLEEP_RESET": "DEEPSLEEP",
        "SOFT_RESET":      "SOFT",       # Ctrl-D / machine.soft_reset()
        "BROWNOUT_RESET":  "BROWNOUT",   # not on ESP32-S3, present on others
    }
    d = {}
    for n in names:
        try:
            d[getattr(machine, n)] = pretty[n]
        except AttributeError:
            pass  # not all MicroPython builds expose every constant
    return d


_RESET_CAUSES = _build_reset_causes()


# Watchdog timeout in ms. sensor_loop feeds it every iteration; if anything
# stalls the loop for longer than this, the WDT reboots the device. The
# subsequent boot logs reset_cause=WDT so we know what happened.
WDT_TIMEOUT_MS = 30_000


# Shared device-status dict, refreshed by status_loop and attached to
# outgoing sensor packets ~1 Hz. Same pattern as V3 (sensor_loop reads
# without locking; status_loop is the sole writer).
device_status = {
    "vbat":             None,
    "pct":              None,
    "uptime_s":         0,
    "free_mem":         0,
    "rssi":             None,
    "imu":              None,   # populated when ICM-42688-P is present
    "subscribers":      0,      # current subscriber count
    "ip":               "",     # LAN address once joined ("" until then)
    "role":             "",     # left / right / labeler / "" (unassigned)
    "reset_cause":      None,
    "reset_cause_name": None,
}


# Module-level handles so the inner task functions can reach them after
# main() returns control to the event loop.
_wdt = None

# WDT instrumentation. _last_feed_ms is the most recent ticks_ms() at which
# the watchdog was fed (i.e. sensor_loop was healthy). _current_op is what
# sensor_loop is doing right now ("scan_matrix" / "send_sensor" / "sd_write"
# / "sleep"). The wdt_canary_loop logs both periodically; on a WDT-fire-reset,
# the persistent log's LAST canary line is the leading edge of the stall AND
# names the step we were on when it began. This is how we root-cause WDT
# resets without paying for per-iteration flash writes.
_last_feed_ms = 0
_current_op   = "boot"


def _feed_wdt():
    global _last_feed_ms
    # Catch BaseException defensively: if ticks_ms or _wdt.feed ever raises a
    # BaseException (MemoryError, asyncio internals, etc.) we still want the
    # canary to report a stale gap rather than the call to propagate up
    # through whatever loop invoked us. SystemExit + CancelledError do
    # propagate (no catch covers them) since this is a sync function.
    try:
        _last_feed_ms = time.ticks_ms()
    except (SystemExit, KeyboardInterrupt):
        pass  # already at module-level, can't propagate cleanly
    except BaseException:
        pass
    if _wdt is not None:
        try:
            _wdt.feed()
        except BaseException:
            pass


# ---------------------------------------------------------------------------
# Status UX constants and pure helpers (testable off-device)
# ---------------------------------------------------------------------------

JOINED_LED_MS = 2000          # solid green after the Wi-Fi link comes up
STATUS_HOLD_MS = 15000        # how long the status page stays up
LOW_BATTERY_NOTICE_EVERY_MS = 300000   # re-show the LOW BATTERY notice
LOW_BATTERY_V_DEFAULT = 3.55
LOW_BATTERY_CLEAR_V = 0.10    # volts above the threshold before the flag clears
LOW_BATTERY_MIN_V = 2.5       # below this the ADC reading is not a battery


def display_height_from(settings):
    """OLED height in pixels from settings: 64 for a 128x64 panel, else the
    shipped 32."""
    try:
        h = int(settings.get("display_height", 32))
    except (TypeError, ValueError):
        return 32
    return 64 if h == 64 else 32


def update_low_battery(was_low, vbat, vbus, threshold=LOW_BATTERY_V_DEFAULT):
    """Low-battery flag with hysteresis. Never low while USB is feeding the
    band, and an implausible reading (nothing connected to the ADC) does not
    count as an empty battery."""
    if vbus or vbat is None or vbat < LOW_BATTERY_MIN_V:
        return False
    if was_low:
        return vbat < threshold + LOW_BATTERY_CLEAR_V
    return vbat < threshold


def note_link(state, connected, ip, now_ms):
    """Record a Wi-Fi link change on the device state. The moment the link
    comes up starts the 2 s green LED confirmation; the first time in this
    boot it also raises the status page so the IP address can be read.
    Called every LED tick with the live link flag."""
    was_up = state.link_up
    state.link_up = bool(connected)
    if not connected:
        state.ip = ""
        return
    if not was_up:
        state.ip = ip or ""
        state.joined_until = time.ticks_add(now_ms, JOINED_LED_MS)
        if not state.link_announced:
            state.link_announced = True
            state.show_status(0)


# ---------------------------------------------------------------------------
# DeviceState: passed to commands.build_handlers() so command verbs can
# mutate live firmware state (scan rate, streaming on/off, reboot flag)
# without commands.py importing the rest of the world.
# ---------------------------------------------------------------------------

class _DeviceState:
    def __init__(self, settings, subscribers):
        self.device_id     = settings["device_id"]
        self.device_type   = settings["device_type"]
        self.fw_version    = settings.get("fw_version", "v4.0.0")
        self.subscribers   = subscribers
        self.matrix_dims   = (15, 4)
        self.caps          = ["sensor", "status", "cmd", "imu"]
        # Mutable runtime knobs
        self.scan_interval_ms = settings.get("scan_interval_ms", 20)
        self.streaming        = True
        self.reboot_requested = False
        # IMU cache populated by imu_loop and consumed by sensor_loop +
        # status_loop. None when the IMU is absent or has not produced a
        # successful read yet. Shape: {"accel": [ax,ay,az], "gyro":
        # [gx,gy,gz]} per PROTOCOL.md 7.1 data.imu. Single writer
        # (imu_loop), multiple lockless readers (MicroPython is single-
        # threaded so dict-replace is atomic from the consumer's view).
        self.imu_cache        = None
        # IMU chip + per-LSB scale advertised in announce + get_info so
        # the hub fusion normalizes data.imu raw counts across mixed-fleet
        # sources (overseer #0217; closes vrpc #0200/#0210 gyro-scale ask).
        # None when no IMU is present. Set by main() after imu.init() lands.
        self.imu_scale        = None

        # Power-mode state machine (board #0220). Three user-visible modes
        # cycled by the SELECT button:
        #   "stream"  -- default; Wi-Fi on, sensor scan on, sub-driven UDP
        #   "standby" -- Wi-Fi on, scan on, UDP send paused; heartbeat ack
        #                still keeps the hub's subscription alive
        #   "charge"  -- USB plugged, low power: send paused, sensor/imu
        #                paused; Wi-Fi may be disabled in a follow-up
        # Defaults to "stream" at boot; the auto-enter rule promotes to
        # "charge" after vbus_present + 30s idle (see power_mode_loop).
        self.power_mode       = "stream"
        # Role tag: "left" / "right" / "labeler" / "" (unassigned). Drives
        # the idle-LED hue and lets the device know which side it is on.
        # Pushed from the phone via the new set_role cmd verb; persisted.
        self.role             = settings.get("role", "") or ""
        # Recent-fault flag: set true at boot when reset_cause indicates
        # WDT (or panic), cleared by power_mode_loop after 60 s. Drives
        # the fast-red blink so the user sees the device just recovered.
        self.in_fault         = False
        # Idle-seconds counter incremented by power_mode_loop when in
        # STREAM with no subscribers; gates auto-enter to CHARGE.
        self.idle_seconds     = 0
        # LED preview while a button is held past an action threshold:
        # None | "charge" | "wipe". Set by power_mode_loop, read by
        # _resolve_led_state so the user sees which action release will
        # trigger (purple = charge toggle, magenta = wifi wipe).
        self.hold_preview     = None
        # Network-requested mode ("stream"/"standby"/"charge") set by the
        # set_power_mode cmd verb; applied by power_mode_loop within a
        # tick so the ack flushes before the radio drops.
        self.requested_power_mode = None
        # Transient OLED message: (lines, until_ticks_ms) or None. While it
        # is live display_loop shows it instead of the heatmap, so a refused
        # action (e.g. RECORD with no SD card) tells the user why nothing
        # happened. Set via show_notice().
        self.notice = None
        # Wi-Fi bring-up and status UX (see note_link / _resolve_led_state).
        #   joining        True from boot until the first join attempt ends
        #   link_up        live Wi-Fi link flag, kept by note_link
        #   ip             LAN address while link_up, else ""
        #   joined_until   ticks_ms until which the LED shows solid green
        #   join_failed    first join failed; band is about to reboot to setup
        #   low_battery    battery under settings low_battery_v, USB absent
        #   view_page      status page on the OLED (0, 1) or None = heatmap
        self.ssid             = settings.get("wifi_ssid") or ""
        self.joining          = True
        self.link_up          = False
        self.link_announced   = False
        self.ip               = ""
        self.joined_until     = None
        self.join_failed      = False
        self.low_battery      = False
        self.low_notice_at    = None
        self.view_page        = None
        self.view_until       = 0
        try:
            self.low_battery_v = float(settings.get("low_battery_v", LOW_BATTERY_V_DEFAULT))
        except (TypeError, ValueError):
            self.low_battery_v = LOW_BATTERY_V_DEFAULT
        self.display_rows     = screens.DEFAULT_ROWS
        self.status_page_count = 2

    def show_notice(self, lines, hold_ms=3000):
        self.notice = (list(lines), time.ticks_add(time.ticks_ms(), hold_ms))

    def show_status(self, page=0, hold_ms=None):
        """Put a status page (0 = network, 1 = device) on the OLED for
        hold_ms (default STATUS_HOLD_MS), then fall back to the heatmap."""
        if hold_ms is None:
            hold_ms = STATUS_HOLD_MS
        self.view_page = page
        self.view_until = time.ticks_add(time.ticks_ms(), hold_ms)

    def tap_status(self):
        """MENU tap: heatmap -> status page 0 -> page 1 -> heatmap."""
        page = self.status_page_now(time.ticks_ms())
        if page is None:
            self.show_status(0)
        elif page + 1 < self.status_page_count:
            self.show_status(page + 1)
        else:
            self.view_page = None

    def status_page_now(self, now_ms):
        """The status page that should be on screen, or None."""
        if self.view_page is None:
            return None
        if time.ticks_diff(self.view_until, now_ms) <= 0:
            self.view_page = None
            return None
        return self.view_page

    def update_battery(self, vbat, pct, vbus, now_ms, threshold=LOW_BATTERY_V_DEFAULT):
        """Refresh the low-battery flag; raise the OLED notice when it first
        trips and again every LOW_BATTERY_NOTICE_EVERY_MS while it stays."""
        low = update_low_battery(self.low_battery, vbat, vbus, threshold)
        if low and (self.low_notice_at is None
                    or time.ticks_diff(now_ms, self.low_notice_at) >= 0):
            self.show_notice(screens.low_battery_lines(vbat, pct, self.display_rows), 4000)
            self.low_notice_at = time.ticks_add(now_ms, LOW_BATTERY_NOTICE_EVERY_MS)
        if not low:
            self.low_notice_at = None
        self.low_battery = low

    def set_scan_interval(self, ms):
        self.scan_interval_ms = int(ms)
        logger.info("scan_interval_ms set to {}".format(self.scan_interval_ms))

    def start_stream(self):
        self.streaming = True
        logger.info("Streaming started")

    def stop_stream(self):
        self.streaming = False
        logger.info("Streaming stopped")

    def request_reboot(self):
        self.reboot_requested = True
        logger.info("Reboot requested")

    def set_role(self, role):
        """Hub-pushed role assignment. Stored in settings (caller
        persists). Valid values: 'left' / 'right' / 'labeler' / ''
        (clear). Unknown roles are clamped to '' so a buggy hub cannot
        wedge the device into an undefined LED state."""
        if role not in ("left", "right", "labeler", ""):
            role = ""
        self.role = role
        logger.info("Role set to '{}'".format(role))

    def cycle_power_mode(self, vbus_present):
        """SELECT-button action. Cycle through the 3-mode wheel:
            STREAM -> STANDBY -> (CHARGE if USB) -> STREAM
        With no USB the CHARGE leg is skipped so the wheel becomes a
        simple 2-state toggle. Matches Tory's locked UX (board #0220)."""
        if self.power_mode == "stream":
            self.power_mode = "standby"
        elif self.power_mode == "standby":
            self.power_mode = "charge" if vbus_present else "stream"
        else:  # "charge" or anything unexpected
            self.power_mode = "stream"
        # Streaming flag reflects whether UDP send should fire:
        # only true in STREAM. STANDBY + CHARGE pause it.
        self.streaming = (self.power_mode == "stream")
        self.idle_seconds = 0
        logger.info("power_mode -> {} (vbus={})".format(self.power_mode, vbus_present))


# ---------------------------------------------------------------------------
# Async tasks
# ---------------------------------------------------------------------------

async def sensor_loop(state, sensor_matrix, network, sd):
    """Scan the matrix and broadcast to every subscriber. No display work
    here; that lives in display_loop.

    Attaches `device_status` as `meta` to ~1 packet per second only. The
    other packets at 50 Hz are lean: if we put meta on every packet the
    larger payloads exhaust lwip's pbuf pool and we start dropping sends
    with [Errno 12] ENOMEM. 1 Hz is plenty for a battery readout in the
    hub UI; hubs merge meta keys non-destructively so stale fields persist
    between updates.

    BaseException catch: phone (#0156) caught d7af0b doing live WDT resets.
    The most likely class of cause is the same one that bit the cmd server
    pre-supervisor: a BaseException (MemoryError, KeyboardInterrupt from
    mpremote, or asyncio internals raising one of these) propagates past
    the old `except Exception` and silently kills this task. With this loop
    dead, _feed_wdt() never runs and the device hard-resets ~30 s later.
    Catching BaseException prevents that silent-death mode; CancelledError
    still re-raises so deliberate shutdown works. _current_op + the
    wdt_canary_loop together identify which step was running when a stall
    began.
    """
    global _current_op
    n = 0
    err_count = 0
    # Rate-profile accumulators (per-step microseconds + iter count).
    # Logged every PROFILE_WINDOW iterations as one info line so the
    # overhead is one int add per step + a periodic log. Diagnostic for
    # the 60 Hz target work (board #0179).
    _p_scan_us = 0
    _p_emit_us = 0
    _p_iter_us = 0
    _p_count = 0
    PROFILE_WINDOW = 100
    last_iter_t = time.ticks_us()
    while True:
        # Pet the watchdog FIRST so it is the first thing that stops
        # happening if this loop wedges.
        _feed_wdt()
        interval_ms = state.scan_interval_ms
        meta_every = max(1, 1000 // max(1, interval_ms))
        try:
            _current_op = "scan_matrix"
            t_scan_start = time.ticks_us()
            matrix = sensor_matrix.scan_matrix()
            t_scan_end = time.ticks_us()
            n += 1
            t_emit_end = t_scan_end  # default if not streaming
            if state.streaming:
                _current_op = "send_sensor"
                meta = device_status if (n % meta_every == 0) else None
                # data.imu rides every frame at the current sensor rate when
                # the cache is populated (imu_loop is the sole writer). Hubs
                # use this for smooth orientation viz; the slower meta.imu
                # path is back-compat for hubs that have not migrated.
                await network.send_sensor(matrix, meta=meta, imu=state.imu_cache)
                t_emit_end = time.ticks_us()
            # SD recording is independent of streaming; the user may want
            # to log offline while no hub is around (record mode = Wi-Fi off
            # + logging, for unsupervised ML capture). IMU rides each row.
            if sd.is_recording():
                _current_op = "sd_write"
                sd.write_frame(matrix, imu=state.imu_cache)
            _current_op = "sleep"
            err_count = 0
            # Profile accumulators
            now_us = time.ticks_us()
            _p_scan_us += time.ticks_diff(t_scan_end, t_scan_start)
            _p_emit_us += time.ticks_diff(t_emit_end, t_scan_end)
            _p_iter_us += time.ticks_diff(now_us, last_iter_t)
            last_iter_t = now_us
            _p_count += 1
            if _p_count >= PROFILE_WINDOW:
                avg_scan = _p_scan_us // _p_count
                avg_emit = _p_emit_us // _p_count
                avg_iter = _p_iter_us // _p_count
                # Effective rate from real iter time: 1e6 / avg_iter_us
                rate_hz = (1000000 // avg_iter) if avg_iter > 0 else 0
                # Demoted to debug (board #0192): persisting this every
                # PROFILE_WINDOW frames dominated flash writes + caused
                # 1s asyncio stalls. Still on stdout for live diagnosis.
                logger.debug(
                    "rate: scan={}us emit={}us iter={}us interval={}ms streaming={} rate={}Hz".format(
                        avg_scan, avg_emit, avg_iter, interval_ms,
                        state.streaming, rate_hz))
                _p_scan_us = 0; _p_emit_us = 0; _p_iter_us = 0; _p_count = 0
        except (asyncio.CancelledError, SystemExit):
            raise
        except BaseException as e:
            if err_count == 0 or err_count % 100 == 0:
                logger.error("sensor_loop iter #{} op={} failed: {} ({})".format(
                    err_count, _current_op, type(e).__name__, e))
            err_count += 1
        await asyncio.sleep_ms(interval_ms)


async def imu_loop(state, imu, interval_ms=33):
    """Read the IMU at a fixed cadence and publish to state.imu_cache for
    sensor_loop (high-rate data.imu) and status_loop (back-compat meta.imu).
    Sole I2C reader for the IMU so there is no bus contention between this
    and sensor_loop's matrix scan.

    Default 33 ms = ~30 Hz IMU read cadence. PROTOCOL.md 7.1 data.imu rides
    each sensor frame at whatever rate sensor_loop fires, so the effective
    hub-side IMU cadence is min(imu_loop rate, sensor_loop rate). Overseer
    target is 60 Hz on the sensor frame; this loop is configured at half
    that (30 Hz) which is smooth enough for orientation while halving the
    I2C traffic. Bump to 16 ms (60 Hz) if a smoother readout is needed.

    Catches BaseException so an I2C glitch cannot silently kill the task."""
    while True:
        try:
            if imu.present and imu.read():
                state.imu_cache = {
                    "accel": [imu.last["ax"], imu.last["ay"], imu.last["az"]],
                    "gyro":  [imu.last["gx"], imu.last["gy"], imu.last["gz"]],
                }
        except (asyncio.CancelledError, SystemExit):
            raise
        except BaseException as e:
            try:
                logger.warn("imu_loop iter failed: {} ({})".format(
                    type(e).__name__, e))
            except Exception:
                pass
        await asyncio.sleep_ms(interval_ms)


async def wdt_canary_loop(interval_s=5):
    """Logs the gap-since-last-WDT-feed and what sensor_loop step is current,
    every interval_s seconds. Diagnostic for the V4 WDT-reset mystery (board
    #0156). On a WDT-fire-reset the persistent log's last canary line tells
    us when the stall started AND which sensor_loop step was running when it
    did. Cheap (one log line per 5 s). Logs at WARN level when gap is more
    than 20 s (approaching the 30 s WDT_TIMEOUT_MS) so it stands out in
    post-mortem review."""
    while True:
        try:
            now = time.ticks_ms()
            gap_ms = time.ticks_diff(now, _last_feed_ms)
            if gap_ms > 20000:
                logger.warn("wdt: gap={}ms op={} (APPROACHING TIMEOUT)".format(
                    gap_ms, _current_op))
            else:
                # Demoted to debug (board #0192): canary fires every 5s
                # and was flushing flash on every emit.
                logger.debug("wdt: gap={}ms op={}".format(gap_ms, _current_op))
        except (asyncio.CancelledError, SystemExit):
            raise
        except BaseException as e:
            try:
                logger.warn("wdt_canary iter failed: {} ({})".format(
                    type(e).__name__, e))
            except Exception:
                pass
        await asyncio.sleep(interval_s)


def _status_info(state):
    """Collect what the status pages show (see screens.status_pages)."""
    mode = getattr(state, "power_mode", "stream")
    if mode in ("charge", "record"):
        wifi = "off"          # the radio is switched off in these modes
    elif state.link_up:
        wifi = "ok"
    elif state.joining:
        wifi = "joining"
    else:
        wifi = "lost"
    return {
        "wifi":      wifi,
        "ip":        state.ip,
        "device_id": state.device_id,
        "role":      state.role,
        "vbat":      device_status.get("vbat"),
        "pct":       device_status.get("pct"),
        "low":       state.low_battery,
        "mode":      mode,
        "subs":      state.subscribers.count(),
        "ssid":      state.ssid,
    }


def _screen_override(state):
    """Pure helper: the text lines that should replace the live heatmap right
    now, or None to draw the heatmap. Priority: a transient notice, then the
    status page (MENU tap, or the first 15 s after joining Wi-Fi), then any
    non-streaming power mode (PAUSED / CHARGING / RECORDING) banner so the
    user can see why the band is not streaming and how to resume."""
    if state is None:
        return None
    rows = getattr(state, "display_rows", screens.DEFAULT_ROWS)
    notice = getattr(state, "notice", None)
    if notice is not None:
        if time.ticks_diff(notice[1], time.ticks_ms()) > 0:
            return notice[0]
        state.notice = None
    page = state.status_page_now(time.ticks_ms()) if hasattr(state, "status_page_now") else None
    if page is not None:
        pages = screens.status_pages(_status_info(state), rows)
        return pages[min(page, len(pages) - 1)]
    return screens.mode_lines(getattr(state, "power_mode", "stream"), rows)


async def display_loop(display, sensor_matrix, interval_ms=200, state=None):
    """Render at ~5 Hz independent of scan rate.

    With `state` given, notices and the PAUSED / CHARGE / RECORD banners
    (see _screen_override) replace the heatmap; text is drawn only when it
    changes, so a banner costs one I2C frame instead of five per second.

    Was 66 ms (~15 Hz). Bumped to 200 ms during the 60Hz profile work
    (#0179) because the SSD1306 I2C frame takes ~22 ms, so at 66 ms
    the display was eating ~33% of bus / CPU time, blocking sensor_loop
    iterations that overlapped a draw. At 200 ms display CPU drops to
    ~11% (frees ~22 ms of CPU per 200 ms window) without meaningfully
    hurting the live-readout UX. Easy to revert by passing
    interval_ms=66 at the call site.
    """
    last_text = None
    while True:
        try:
            lines = _screen_override(state)
            if lines is None:
                last_text = None
                display.draw_sensor_matrix(sensor_matrix.matrix)
            elif lines != last_text:
                display.text_screen(lines)
                last_text = lines
        except (asyncio.CancelledError, SystemExit):
            raise
        except BaseException as e:
            logger.warn("display_loop draw failed: {} ({})".format(type(e).__name__, e))
        await asyncio.sleep_ms(interval_ms)


async def menu_loop(menu):
    """Poll buttons frequently for responsive UI."""
    while True:
        try:
            menu.check_buttons()
        except (asyncio.CancelledError, SystemExit):
            raise
        except BaseException as e:
            logger.warn("menu_loop check failed: {} ({})".format(type(e).__name__, e))
        await asyncio.sleep_ms(50)


async def status_loop(state, power, network, imu, interval_s=5):
    """Refresh `device_status` and emit a REPL heartbeat. The dict is
    consumed by sensor_loop (attached as packet meta) and by status_led_loop
    (drives the connection-state palette).

    Also keeps the LAN address, the role and the low-battery flag current:
    `ip` and `role` ride in the status frame (and GET /info) so a hub can
    show which band is which without a second request.

    Does NOT call imu.read() any more (board #0163 IMU-in-data work):
    imu_loop is the sole I2C reader and writes state.imu_cache. status_loop
    just snapshots imu.status_summary() to keep the meta.imu path populated
    for back-compat hubs that have not migrated to data.imu yet."""
    while True:
        try:
            v = power.battery_voltage()
            p = power.battery_percent()
            uptime_s = time.ticks_ms() // 1000
            free_mem = gc.mem_free()
            rssi = network.rssi()
            imu_snap = imu.status_summary() if imu.present else None

            device_status["vbat"]        = round(v, 3)
            device_status["pct"]         = p
            device_status["uptime_s"]    = uptime_s
            device_status["free_mem"]    = free_mem
            device_status["rssi"]        = rssi
            device_status["imu"]         = imu_snap
            device_status["subscribers"] = state.subscribers.count()
            device_status["role"]        = state.role
            if network.is_connected():
                state.ip = network.local_ip() or state.ip
            device_status["ip"]          = state.ip
            state.update_battery(v, p, power.is_vbus_present(), time.ticks_ms(),
                                 state.low_battery_v)

            wifi = "WiFi:ok" if network.is_connected() else "WiFi:--"
            subs = state.subscribers.count()
            # Demoted to debug (board #0192): periodic status line; the
            # status frame on the wire carries the same data for hubs.
            logger.debug("BAT {:.2f}V ({}%) up={}s rssi={} mem={} subs={} {}".format(
                v, p, uptime_s, rssi, free_mem, subs, wifi))
        except (asyncio.CancelledError, SystemExit):
            raise
        except BaseException as e:
            logger.warn("status_loop iter failed: {} ({})".format(type(e).__name__, e))
        await asyncio.sleep(interval_s)


async def subscriber_prune_loop(subscribers, interval_s=1):
    """Drop subscribers whose last heartbeat aged past the timeout."""
    while True:
        try:
            dropped = subscribers.prune_stale()
            if dropped:
                logger.info("Pruned {} stale subscriber(s); remaining={}".format(
                    dropped, subscribers.count()))
        except (asyncio.CancelledError, SystemExit):
            raise
        except BaseException as e:
            logger.warn("subscriber_prune_loop failed: {} ({})".format(type(e).__name__, e))
        await asyncio.sleep(interval_s)


async def status_led_loop(state, status_led, network, power, interval_ms=33):
    """Drive the RGB LED state palette based on live device state. Runs at
    ~30 Hz so breathe / blink animations look smooth. Started before the
    Wi-Fi join so "joining" is visible, and it also watches the Wi-Fi link
    (note_link) to time the green "joined" confirmation.

    Resolution priority (highest wins; the same list is the LED guide in the
    README and is pinned by tests/test_status_ux.py):

        fault              -> in_fault set (recent WDT) -- RED fast blink
        hold previews      -> MENU+SELECT held: MAGENTA blink (release wipes
                              Wi-Fi); MENU held: PURPLE pulse; SELECT held: GREEN breath
        join_failed        -> AMBER fast blink, just before rebooting to setup
        joined             -> GREEN solid for 2 s after the Wi-Fi link comes up
        joining            -> BLUE solid while the first join runs
        RECORD             -> GREEN 1 Hz breath
        STANDBY            -> CYAN slow breath
        CHARGE + full      -> solid PURPLE
        CHARGE + charging  -> PURPLE slow pulse
        low battery        -> AMBER 1 Hz blink (USB not plugged in)
        Wi-Fi lost         -> AMBER slow pulse (the band keeps retrying)
        STREAM + subs + USB-> ORANGE solid + brief cyan flash every 5s
        STREAM + subs      -> solid ORANGE
        STREAM + idle      -> role-color brief pulse every 3s
                              (BLUE left, RED right, GREEN labeler, WHITE unassigned)

    Setup mode (blue 1 Hz blink) is not in this list: it runs on the AP
    branch of main() with setup_led_loop instead.
    """
    while True:
        try:
            now_ms = time.ticks_ms()
            try:
                connected = network.is_connected()
                note_link(state, connected,
                          network.local_ip() if connected and not state.link_up else None,
                          now_ms)
            except Exception as e:
                logger.warn("note_link failed: {}".format(e))
            named = _resolve_led_state(state, power, now_ms)
            status_led.set_state(named)
            status_led.animate(now_ms)
        except (asyncio.CancelledError, SystemExit):
            raise
        except BaseException as e:
            logger.warn("status_led_loop failed: {} ({})".format(type(e).__name__, e))
        await asyncio.sleep_ms(interval_ms)


def _resolve_led_state(state, power, now_ms=None):
    """Pure helper: map the current device state to one palette name (the
    priority list is in the status_led_loop docstring). Kept
    side-effect-free so the resolver logic is easy to unit-test off-device by
    feeding it a fake state object."""
    if getattr(state, "in_fault", False):
        return "fault"
    # Button-hold preview beats mode display: while BOOT is held past a
    # threshold the LED shows which action RELEASING now will trigger.
    hp = getattr(state, "hold_preview", None)
    if hp == "wipe":
        return "ap_provisioning"   # magenta blink = release wipes Wi-Fi
    if hp == "charge":
        return "charging"          # purple pulse = release toggles charge
    if hp == "record":
        return "record"            # green breath = release toggles record
    if getattr(state, "join_failed", False):
        return "join_failed"
    if now_ms is None:
        now_ms = time.ticks_ms()
    joined_until = getattr(state, "joined_until", None)
    if joined_until is not None and time.ticks_diff(joined_until, now_ms) > 0:
        return "joined"
    link_up = getattr(state, "link_up", True)
    if getattr(state, "joining", False) and not link_up:
        return "joining"
    mode = getattr(state, "power_mode", "stream")
    if mode == "record":
        return "record"
    if mode == "standby":
        return "standby"
    if mode == "charge":
        return "charge_full" if power.is_battery_full() else "charging"
    vbus = power.is_vbus_present()
    if getattr(state, "low_battery", False) and not vbus:
        return "low_battery"
    if not link_up:
        return "wifi_lost"
    # STREAM. Treat a hub-paused state (subs > 0 but state.streaming
    # is False because a hub called stop_stream) as idle, so the LED
    # does not lie about whether data is flowing.
    has_active_subs = state.subscribers.count() > 0 and state.streaming
    if has_active_subs:
        return "streaming_on_usb" if vbus else "streaming"
    # Idle: role-coded
    role = getattr(state, "role", "") or ""
    return {
        "left":    "idle_left",
        "right":   "idle_right",
        "labeler": "idle_labeler",
    }.get(role, "idle_unassigned")


async def power_mode_loop(state, power, select_pin, settings,
                          menu_pin=None,
                          network=None,
                          sd=None,
                          tick_ms=100,
                          auto_charge_idle_s=None,
                          fault_clear_s=60,
                          charge_hold_s=2):
    """Drive the STREAM/STANDBY/CHARGE state machine + SELECT button cycle.

    Responsibilities (board #0220):
      - Sample SELECT (active-LOW with pull-up) at tick_ms; debounce
        on falling edge; on press, advance state.cycle_power_mode().
      - Every 10 ticks (1 s) update power.update_vbus_state() so the
        LED resolver sees fresh VBUS + battery-full bits.
      - Auto-enter CHARGE: when power_mode==stream + USB present +
        idle (no subscribers) for auto_charge_idle_s seconds (settings
        "auto_charge_idle_s", default 300; 0 = never). CHARGE turns Wi-Fi
        off, so the delay is long enough that a band plugged in for power
        while the hub is still being set up does not vanish from the app.
      - Auto-exit CHARGE: when USB unplugs while in CHARGE, drop
        straight back to STREAM (the user's expected default).
      - Clear in_fault after fault_clear_s of clean runtime so the
        red-blink reverts to normal once the device has demonstrated
        stability.

    CONTROL SCHEME (V4 button topology confirmed from schematic
    2026-07-02: MENU=GPIO21, SELECT=GPIO10; the "BOOT" button is wired
    to EN = hardware reset, NOT a readable GPIO, so it can only reset
    the chip and carries no firmware action):

      MENU tap              -> status page on the OLED (Wi-Fi, IP address,
                               device id, role; then battery, mode, hubs);
                               each tap shows the next page, the last tap
                               returns to the heatmap; pages time out after 15 s
      MENU held 2 s        -> toggle CHARGE mode
      SELECT tap            -> toggle STANDBY <-> STREAM
      SELECT held 2 s       -> toggle RECORD (Wi-Fi off + SD logging;
                               unsupervised ML capture). Refused, with
                               Wi-Fi left on and an OLED note, when the SD
                               card does not mount or settings
                               "record_enabled" is false.
      (in CHARGE/RECORD) a  -> SELECT tap or hold leaves the mode
                               (reboots for clean Wi-Fi; RECORD closes
                               the SD session first)
      MENU + SELECT held 3s -> wipe Wi-Fi creds -> AP reprovision

    CHARGE turns the Wi-Fi RADIO OFF and drops the CPU to 80 MHz so
    nearly all of the STC4054's programmed charge current reaches the
    battery instead of feeding the radio (the reason the green CHRG LED
    never went out on a weak port). LED: purple pulse charging, solid
    purple at full.

    LEAVING CHARGE REBOOTS. sta.active(False) -> active(True) does not
    reliably rejoin the AP on ESP32-S3 (this was the "held the button
    and it never came back onto Wi-Fi" symptom). A reboot always lands
    in STREAM with Wi-Fi up, which for a charging device is fine.

    The OLED shows a banner in every non-streaming mode (see
    screens.mode_lines), drawn by display_loop from state.power_mode.

    Hardened with SystemExit/CancelledError re-raise + BaseException
    catch matching the rest of the V4 loops (board #0166)."""
    if auto_charge_idle_s is None:
        auto_charge_idle_s = settings.get("auto_charge_idle_s", 300)

    def _enter_charge(reason):
        state.power_mode = "charge"
        state.streaming = False
        state.idle_seconds = 0
        try:
            machine.freq(80_000_000)
        except Exception as e:
            logger.warn("freq(80MHz) failed: {}".format(e))
        if network is not None:
            try:
                network.sta.active(False)
            except Exception as e:
                logger.warn("Wi-Fi off failed: {}".format(e))
        logger.info("CHARGE entered ({}): Wi-Fi off, CPU 80MHz".format(reason))

    def _leave_charge(reason):
        # Reboot for a guaranteed-clean Wi-Fi bring-up (see docstring).
        # logger.warn flushes before the reset so the reason is durable.
        logger.warn("CHARGE exit ({}): rebooting for clean Wi-Fi".format(reason))
        machine.reset()

    def _wipe_and_reprovision(reason):
        logger.warn("{}: wiping Wi-Fi creds -> AP provisioning".format(reason))
        try:
            settings["wifi_ssid"] = ""
            settings["wifi_password"] = ""
            settings["wifi_joined_ok"] = False
            settings["last_wifi_error"] = ""
            settings["last_ssid"] = ""
            SettingsManager.save(settings)
        except Exception as e:
            logger.error("Cred wipe failed; NOT rebooting: {}".format(e))
            return
        machine.reset()

    def _enter_record(reason):
        # Unsupervised local capture: Wi-Fi OFF, sensor scan continues at
        # full speed (240 MHz -- SD writes + scan need it), SD session open.
        # sensor_loop writes each frame (matrix + labels + IMU) while
        # sd.is_recording(). Green breathing LED.
        #
        # Entering RECORD cuts Wi-Fi, so it is refused (nothing changes, Wi-Fi
        # stays up and streaming) unless the SD session is already open: with
        # no card the old order dropped Wi-Fi first and then captured nothing,
        # which looked like the band had died.
        if not settings.get("record_enabled", True):
            logger.info("RECORD requested ({}) but record_enabled is false; ignoring".format(reason))
            state.show_notice(["RECORD OFF", "disabled in", "settings"])
            return
        if sd is None:
            logger.warn("record requested but no SD logger; ignoring")
            state.show_notice(["NO SD CARD", "Record needs one", "Wi-Fi stays on"])
            return
        was_enabled = sd.enabled
        sd.force_enable()
        opened = False
        try:
            opened = bool(sd.mount() and sd.start_session())
        except Exception as e:
            logger.warn("RECORD start failed: {}".format(e))
        if not opened:
            sd.enabled = was_enabled
            logger.warn("RECORD refused ({}): SD mount/session failed (card in?); Wi-Fi left on".format(reason))
            state.show_notice(["NO SD CARD", "Record needs one", "Wi-Fi stays on"])
            return
        state.power_mode = "record"
        state.streaming = False
        state.idle_seconds = 0
        if network is not None:
            try:
                network.sta.active(False)
            except Exception as e:
                logger.warn("Wi-Fi off failed: {}".format(e))
        logger.info("RECORD entered ({}): Wi-Fi off, SD session open".format(reason))

    def _leave_record(reason):
        # Close the SD file cleanly BEFORE the reboot so the last buffered
        # frames are flushed, then reboot for a clean Wi-Fi bring-up.
        if sd is not None:
            try:
                sd.end_session()
            except Exception as e:
                logger.warn("RECORD end_session failed: {}".format(e))
        logger.warn("RECORD exit ({}): SD closed, rebooting for clean Wi-Fi".format(reason))
        machine.reset()

    DEBOUNCE_MS = 250
    tick_count = 0
    menu_held_ticks = 0
    menu_latched = False
    chord_held_ticks = 0
    chord_latched = False
    sel_held_ticks = 0
    sel_latched = False
    chord_wait = False
    last_select_ms = 0
    last_menu_ms = 0
    charge_hold_ticks = max(1, (charge_hold_s * 1000) // tick_ms)   # 2 s
    record_hold_ticks = charge_hold_ticks                          # SELECT hold 2 s
    chord_hold_ticks = max(1, (3 * 1000) // tick_ms)                # 3 s
    prev_vbus = power.is_vbus_present()
    fault_started_ms = time.ticks_ms() if state.in_fault else None
    while True:
        try:
            now_ms = time.ticks_ms()
            try:
                menu_down = menu_pin.value() == 0
            except Exception:
                menu_down = False
            try:
                sel_down = select_pin.value() == 0
            except Exception:
                sel_down = False

            # A) MENU + SELECT chord held 3 s -> reprovision wipe. Checked
            #    first so a deliberate two-finger hold never triggers the
            #    single-button actions. LED previews magenta while held.
            if menu_down and sel_down:
                chord_held_ticks += 1
                state.hold_preview = "wipe"
                if chord_held_ticks >= chord_hold_ticks and not chord_latched:
                    chord_latched = True
                    _wipe_and_reprovision("MENU+SELECT held {}s".format(
                        (chord_held_ticks * tick_ms) // 1000))
                # Skip single-button handling while the chord is forming.
                menu_held_ticks = 0
                menu_latched = False
                sel_held_ticks = 0
                sel_latched = False
            else:
                if chord_held_ticks:
                    state.hold_preview = None
                    # The chord was let go before 3 s. Ignore whichever
                    # finger is still down until both are up, so letting go
                    # of one button first does not count as a tap or a hold
                    # of the other.
                    chord_wait = True
                chord_held_ticks = 0
                chord_latched = False
                if chord_wait:
                    if not menu_down and not sel_down:
                        chord_wait = False
                    else:
                        menu_down = False
                        sel_down = False

                # B) MENU held 2 s -> toggle CHARGE (one-shot per hold).
                if menu_down:
                    menu_held_ticks += 1
                    if menu_held_ticks >= charge_hold_ticks:
                        state.hold_preview = "charge"
                        if not menu_latched:
                            menu_latched = True
                            if state.power_mode == "charge":
                                _leave_charge("MENU hold")   # reboots
                            else:
                                _enter_charge("MENU hold")
                    # MENU has priority; do not accumulate a SELECT hold too.
                    sel_held_ticks = 0
                    sel_latched = False
                else:
                    if menu_held_ticks:
                        state.hold_preview = None
                    # Short MENU tap on release -> status page (the 2 s hold
                    # above is the CHARGE toggle and does not also tap).
                    if (menu_held_ticks and not menu_latched
                            and menu_held_ticks < charge_hold_ticks
                            and time.ticks_diff(now_ms, last_menu_ms) > DEBOUNCE_MS):
                        last_menu_ms = now_ms
                        state.tap_status()
                    menu_held_ticks = 0
                    menu_latched = False

                    # C) SELECT: TAP -> toggle STANDBY (or leave charge/record);
                    #    HOLD 2 s -> toggle RECORD (unsupervised SD logging).
                    #    Tap is decided on release (short press, record not
                    #    latched); the hold fires at the 2 s mark.
                    if sel_down:
                        sel_held_ticks += 1
                        if sel_held_ticks >= record_hold_ticks:
                            if settings.get("record_enabled", True):
                                state.hold_preview = "record"
                            if not sel_latched:
                                sel_latched = True
                                if state.power_mode == "record":
                                    _leave_record("SELECT hold")  # reboots
                                else:
                                    _enter_record("SELECT hold")
                    else:
                        if sel_held_ticks and state.hold_preview == "record":
                            state.hold_preview = None
                        # Short tap on release -> the light STANDBY toggle.
                        if (sel_held_ticks and not sel_latched
                                and sel_held_ticks < record_hold_ticks
                                and time.ticks_diff(now_ms, last_select_ms) > DEBOUNCE_MS):
                            last_select_ms = now_ms
                            if state.power_mode == "charge":
                                _leave_charge("SELECT tap")     # reboots
                            elif state.power_mode == "record":
                                _leave_record("SELECT tap")     # reboots
                            elif state.power_mode == "standby":
                                state.power_mode = "stream"
                                state.streaming = True
                                logger.info("SELECT: STANDBY -> STREAM")
                            else:
                                state.power_mode = "standby"
                                state.streaming = False
                                logger.info("SELECT: STREAM -> STANDBY")
                        sel_held_ticks = 0
                        sel_latched = False

            # D) Network-requested mode (set_power_mode cmd verb). The ack
            #    already flushed before this tick, so acting here (incl.
            #    a reboot to leave charge) does not eat the reply.
            req = state.requested_power_mode
            if req:
                state.requested_power_mode = None
                cur = state.power_mode
                if req == "charge":
                    if cur == "record":
                        _leave_record("cmd charge")   # reboots; user re-enters charge
                    elif cur != "charge":
                        _enter_charge("cmd")
                elif req == "record":
                    if cur == "charge":
                        _leave_charge("cmd record")   # reboots; user re-requests record
                    elif cur != "record":
                        _enter_record("cmd")
                elif req == "stream":
                    if cur == "charge":
                        _leave_charge("cmd stream")   # reboots into stream
                    elif cur == "record":
                        _leave_record("cmd stream")   # reboots into stream
                    else:
                        state.power_mode = "stream"
                        state.streaming = True
                elif req == "standby":
                    if cur == "charge":
                        _leave_charge("cmd standby")  # reboot lands in stream
                    elif cur == "record":
                        _leave_record("cmd standby")  # reboot lands in stream
                    else:
                        state.power_mode = "standby"
                        state.streaming = False

            # 2) VBUS + battery-full update at 1 Hz.
            tick_count += 1
            if tick_count >= max(1, 1000 // tick_ms):
                tick_count = 0
                try:
                    power.update_vbus_state()
                except Exception as e:
                    logger.warn("update_vbus_state failed: {}".format(e))

                # 3) Track idle seconds for auto-CHARGE entry.
                if state.power_mode == "stream" and state.subscribers.count() == 0:
                    state.idle_seconds += 1
                else:
                    state.idle_seconds = 0

                # 4) Auto-enter / auto-exit CHARGE. Exit is EDGE-based
                #    (vbus present -> absent) rather than level-based so a
                #    manual MENU-hold entry without USB (e.g. a power bank
                #    the VBAT heuristic cannot see) is not immediately
                #    fought by the auto-exit.
                vbus_now = power.is_vbus_present()
                if (state.power_mode == "stream"
                        and vbus_now
                        and auto_charge_idle_s > 0
                        and state.idle_seconds >= auto_charge_idle_s):
                    _enter_charge("USB + idle {}s".format(auto_charge_idle_s))
                elif (state.power_mode == "charge"
                        and prev_vbus and not vbus_now):
                    _leave_charge("USB unplugged")   # reboots into stream
                prev_vbus = vbus_now

                # 5) Auto-clear in_fault after fault_clear_s of clean runtime.
                if state.in_fault and fault_started_ms is not None:
                    if time.ticks_diff(now_ms, fault_started_ms) > fault_clear_s * 1000:
                        state.in_fault = False
                        logger.info("in_fault cleared after {}s clean runtime".format(fault_clear_s))
        except (asyncio.CancelledError, SystemExit):
            raise
        except BaseException as e:
            logger.warn("power_mode_loop iter failed: {} ({})".format(
                type(e).__name__, e))
        await asyncio.sleep_ms(tick_ms)


async def gc_loop(interval_s=2):
    """Manual GC pacing. ESP32 MicroPython tends to let the heap fragment
    under steady allocation pressure; periodic explicit collect keeps it flat."""
    while True:
        try:
            gc.collect()
        except (asyncio.CancelledError, SystemExit):
            raise
        except BaseException as e:
            try:
                logger.warn("gc_loop failed: {} ({})".format(type(e).__name__, e))
            except Exception:
                pass
        await asyncio.sleep(interval_s)


async def reboot_watcher(state):
    """Poll the reboot flag set by a `reboot` command. We sleep briefly so
    the ack can flush, then soft_reset."""
    while True:
        try:
            if state.reboot_requested:
                logger.info("Soft-resetting in 500 ms...")
                await asyncio.sleep_ms(500)
                machine.soft_reset()
        except (asyncio.CancelledError, SystemExit):
            raise
        except BaseException as e:
            logger.warn("reboot_watcher failed: {} ({})".format(type(e).__name__, e))
        await asyncio.sleep_ms(200)


# ---------------------------------------------------------------------------
# First-join fallback (PROVISIONING.md section 2)
# ---------------------------------------------------------------------------

async def _fall_back_to_setup(settings, display, status_led, reason, hold_s=2, state=None):
    """The very first Wi-Fi join after provisioning failed (wrong password,
    5 GHz-only network, out of range). Forget the credentials, remember why
    (last_wifi_error) and which network (last_ssid), then reboot: with no
    wifi_ssid the next boot lands in the AP branch of main(), which shows
    the same SSID and PSK plus the reason on the OLED. Returns False without
    resetting if the settings could not be saved (so a flash fault cannot
    cause a reset loop); on success machine.reset() does not return.

    With `state`, the amber "join failed" blink goes through the LED
    animator that is already running; without it the LED is set directly."""
    keys = ("wifi_ssid", "wifi_password", "wifi_joined_ok", "last_wifi_error", "last_ssid")
    backup = {k: settings.get(k) for k in keys}
    tried = settings.get("wifi_ssid") or ""
    record_join_failure(settings, reason)
    if not SettingsManager.save(settings):
        for k in keys:
            settings[k] = backup[k]
        logger.error("Wi-Fi fallback: could not save settings; staying in normal mode")
        return False
    logger.warn("First Wi-Fi join failed ({}); creds cleared, rebooting into setup mode".format(reason))
    try:
        display.text_screen(screens.join_failed_lines(
            reason, tried, getattr(display, "rows", screens.DEFAULT_ROWS)))
        if state is not None:
            state.join_failed = True     # status_led_loop shows the amber blink
        else:
            status_led.set_rgb(200, 80, 0)   # amber while the reason is on screen
    except Exception as e:
        logger.warn("Wi-Fi fallback screen failed: {}".format(e))
    await asyncio.sleep(hold_s)
    machine.reset()
    return True


async def _handle_join_result(joined, network, settings, display, status_led, state=None):
    """Decide what a finished network.connect() means. Returns True when the
    band is resetting into setup mode (the caller should stop booting)."""
    if joined:
        # Proven network: from now on a failed join (router off, out of
        # range) only waits and retries, it never discards the credentials.
        if record_join_success(settings):
            SettingsManager.save(settings)
        return False
    if network.last_error and not settings.get("wifi_joined_ok"):
        # Never joined this network. Go back to setup mode so the user can
        # fix the password / pick a 2.4 GHz network, instead of looping forever.
        return await _fall_back_to_setup(settings, display, status_led,
                                         network.last_error, state=state)
    return False


# ---------------------------------------------------------------------------
# USB console: the PC app gives the band its Wi-Fi over the USB cable
# (lib/usb_console.py, docs/usb-console.md). Started in BOTH branches of
# main() so a band answers on the cable whether or not it has Wi-Fi yet.
# ---------------------------------------------------------------------------

def start_usb_console(settings, set_role, get_ip=None):
    """Spawn the USB console task. Best-effort: a failure here is logged and
    the band carries on without the cable channel (the setup network and the
    192.168.4.1 page still work). Keyboard-interrupt handling is untouched."""
    try:
        console = usb_console.UsbConsole(
            settings,
            save=lambda: SettingsManager.save(settings),
            restart=machine.reset,
            set_role=set_role,
            get_ip=get_ip)
        asyncio.create_task(usb_console.serve(console))
        return console
    except Exception as e:
        logger.warn("USB console not started: {} ({})".format(type(e).__name__, e))
        return None


# ---------------------------------------------------------------------------
# Setup mode (no Wi-Fi credentials): LED + OLED while the HTTP server waits
# ---------------------------------------------------------------------------

async def setup_led_loop(status_led, name, interval_ms=33):
    """Animate one named palette state for as long as the band stays in setup
    mode (blue 1 Hz blink, or the blue double blink when there is no screen,
    the normal case on a kit band)."""
    status_led.set_state(name)
    while True:
        try:
            status_led.animate(time.ticks_ms())
        except (asyncio.CancelledError, SystemExit):
            raise
        except BaseException as e:
            logger.warn("setup_led_loop failed: {} ({})".format(type(e).__name__, e))
        await asyncio.sleep_ms(interval_ms)


async def setup_screen_loop(display, ap_ssid, psk, last_error="", last_ssid="",
                            frame_s=None):
    """Redraw the setup screen when its content changes. On a 4-line panel
    the top row rotates (WIFI SETUP / see guide / failure reason) while the
    network name and password stay put; on a taller panel nothing rotates and
    this just draws once."""
    if frame_s is None:
        frame_s = screens.SETUP_FRAME_S
    tick = 0
    last = None
    while True:
        try:
            lines = screens.setup_screen_lines(
                ap_ssid, psk, last_error, last_ssid,
                getattr(display, "rows", screens.DEFAULT_ROWS), tick)
            if lines != last:
                display.text_screen(lines)
                last = lines
        except (asyncio.CancelledError, SystemExit):
            raise
        except BaseException as e:
            logger.warn("setup_screen_loop failed: {} ({})".format(type(e).__name__, e))
        tick += 1
        await asyncio.sleep(frame_s)


# ---------------------------------------------------------------------------
# Boot sequence
# ---------------------------------------------------------------------------

async def main():
    # Open the on-flash log FIRST so everything that follows is persisted.
    logger.init_persistent()
    logger.info("FlexGrid V4 startup")

    # Light the status LED the moment the firmware runs, before the radio
    # starts: a band with no screen has no other sign of life, and a supply
    # that cannot feed the Wi-Fi radio (seen on battery in setup mode) would
    # otherwise reset the chip before the first blink. Dim blue, solid, until
    # a state loop takes over.
    status_led = None
    try:
        status_led = StatusLed()
        status_led.set_rgb(0, 0, 60)
    except Exception as e:
        logger.warn("Status LED init failed: {}".format(e))

    # Reset cause tells us how the LAST run ended.
    rc = None
    try:
        rc = machine.reset_cause()
        logger.info("Reset cause: {} ({})".format(rc, _RESET_CAUSES.get(rc, "?")))
    except Exception as e:
        logger.warn("Could not read reset_cause: {}".format(e))
    device_status["reset_cause"] = rc
    device_status["reset_cause_name"] = _RESET_CAUSES.get(rc, str(rc))

    try:
        uos.stat('config')
    except OSError:
        logger.warn("No config folder; creating")
        uos.mkdir('config')

    settings = SettingsManager.load()

    # boot_seq: monotonic per-device restart counter, persisted so that a
    # phone seeing the announce can detect a fresh boot (counter jumped)
    # and flush cached subscription state (PROTOCOL.md section 5.6).
    # Bumped once here, before any subsystem that reads the announce
    # envelope is constructed, and persisted via SettingsManager so the
    # value survives reboot. The save is the only flash write this path
    # makes; cheap.
    settings["boot_seq"] = int(settings.get("boot_seq", 0)) + 1
    try:
        SettingsManager.save(settings)
    except Exception as e:
        logger.warn("boot_seq persist failed (non-fatal): {}".format(e))
    logger.info("boot_seq: {}".format(settings["boot_seq"]))

    redacted = {k: ("<redacted>" if k in ("wifi_password", "provisioning_psk") else v)
                for k, v in settings.items()}
    logger.info("Settings: {}".format(redacted))
    logger.info("Device id: {}".format(settings["device_id"]))

    # ----------------------------------------------------------------------
    # State machine fork (PROVISIONING.md section 2).
    # An empty wifi_ssid means we have never been provisioned (or were
    # reset back to factory). Boot to AP mode and run the provisioning
    # HTTP server until the user POSTs /provision, then soft-reset into
    # the provisioned branch below.
    # ----------------------------------------------------------------------
    if not (settings.get("wifi_ssid") or "").strip():
        logger.info("Unprovisioned (wifi_ssid empty); entering AP mode")
        # Minimal subsystems for the AP path: display (to print SSID + PSK
        # on the OLED) and status LED (blue blink for setup mode; a double
        # blink instead when there is no screen, which is the normal case on
        # a kit band: the password is then read from the sticker and the LED
        # is the only sign of life).
        display    = DisplayManager(height=display_height_from(settings))
        if status_led is None:
            status_led = StatusLed()
        # Setup mode runs only the access point and a small web page, and on
        # battery the supply has little headroom for the radio: slow the CPU
        # here and let start_ap turn the transmit power down.
        try:
            machine.freq(80_000_000)
        except Exception as e:
            logger.warn("freq(80MHz) in setup mode failed: {}".format(e))
        if not display.present:
            logger.info("Setup mode with no screen (normal on a kit band): the sticker has the setup name and password; LED shows the no-screen pattern")
        led_name = "setup" if display.present else "setup_no_oled"
        asyncio.create_task(setup_led_loop(status_led, led_name))

        # Wi-Fi over the USB cable (the app's route; the setup network below
        # stays as the fallback). No device state exists in setup mode, so the
        # role verb writes straight to the settings.
        async def _setup_set_role(role):
            if role not in usb_console.ROLES:
                role = ""
            settings["role"] = role
            SettingsManager.save(settings)
            return role
        start_usb_console(settings, _setup_set_role)

        # Bring the AP up FIRST so we know the PSK (provisioning.run mints it
        # if missing), then paint the OLED with the final SSID + PSK.
        psk = provisioning.start_ap(
            settings, settings["device_type"], settings["device_id"])
        SettingsManager.save(settings)
        ap_ssid = provisioning.ssid_for(settings["device_type"], settings["device_id"])
        last_error = settings.get("last_wifi_error") or ""
        last_ssid = settings.get("last_ssid") or ""
        asyncio.create_task(setup_screen_loop(display, ap_ssid, psk, last_error, last_ssid))

        info_extras = {
            "caps":   ["sensor", "status", "cmd", "imu"],
            "matrix": [15, 4],
            "oled":   display.present,
            "usb_console": 1,
        }
        # serve() blocks until /provision lands, then soft_resets. Since
        # start_ap was already called, we drive serve() directly rather than
        # the bundled run() entry point.
        state = provisioning._ProvisioningState(settings, SettingsManager, info_extras)
        await provisioning.serve(state)
        # serve() ends in machine.soft_reset(); if it somehow returns, the
        # next loop iteration should fall through to STA.
        return

    # Build the subsystems in roughly the order they are needed.
    subscribers   = Subscribers(
                        max_subscribers=settings.get("max_subscribers", 4),
                        heartbeat_timeout_s=settings.get("heartbeat_timeout_s", 5))
    # vbus_stat_pin: None on as-built V4 (GPIO39 unwired, floats);
    # set to 39 in settings.json on V4.2+ hardware only.
    power         = PowerManager(vbus_pin=settings.get("vbus_stat_pin"))
    display       = DisplayManager(height=display_height_from(settings))
    sensor_matrix = SensorMatrix()
    network       = NetworkManager(settings, subscribers)
    menu          = MenuManager(display, network, power)
    if status_led is None:
        status_led = StatusLed()

    # IMU and SD are V4-only. Both are best-effort: if the hardware is not
    # present, the firmware degrades gracefully.
    imu = IMU()
    imu.init()
    sd = SDLogger(settings)
    if sd.enabled:
        sd.mount()

    # DeviceState carries the live mutable state command handlers may mutate.
    state = _DeviceState(settings, subscribers)
    state.display_rows = display.rows
    state.status_page_count = 2 if display.rows < 8 else 1
    # Cache the IMU scale once on _DeviceState so announce + get_info pick
    # it up without each having to ask the imu instance. Stays None on
    # boards where the IMU isn't present, which keeps the field optional.
    state.imu_scale = imu.scale_dict() if imu.present else None
    # If the previous run ended in a watchdog timeout, flag in_fault so
    # the RGB LED resolver shows fast-red-blink for the first 60 s.
    # power_mode_loop will auto-clear after fault_clear_s of clean runtime.
    if rc is not None and _RESET_CAUSES.get(rc, "") == "WDT":
        state.in_fault = True
        logger.warn("in_fault set (previous run ended in WDT)")
    # Per-key persist callback bound to the SettingsManager. The set_role
    # cmd verb uses this to write the new role through to settings.json.
    def _save_settings(key, value):
        settings[key] = value
        SettingsManager.save(settings)
    state.save_settings = _save_settings
    # SELECT-button input. Active-LOW with internal pull-up. On boards
    # where the button is NOT yet soldered, pin_value() returns 1 always
    # and power_mode_loop's polling no-ops gracefully (auto-CHARGE still
    # works). Constructed once + handed to the loop.
    try:
        select_pin = Pin(pinmap.BTN_SELECT, Pin.IN, Pin.PULL_UP)
    except Exception as e:
        logger.warn("SELECT button init failed (continuing without it): {}".format(e))
        # Fallback dummy: a stub whose value() always returns 1 keeps the
        # loop alive without special-casing the missing-pin path.
        class _NoPin:
            def value(self):
                return 1
        select_pin = _NoPin()
    # MENU button (GPIO21). Held 2 s -> toggle CHARGE mode (Wi-Fi off,
    # purple LED; Tory 2026-07-02). NOTE: the physical "BOOT" button on
    # V4 is wired to EN (hardware reset), NOT GPIO0, so it carries no
    # firmware action -- MENU + SELECT are the only readable controls.
    try:
        menu_pin = Pin(pinmap.BTN_MENU, Pin.IN, Pin.PULL_UP)
    except Exception as e:
        logger.warn("MENU button init failed (charge toggle disabled): {}".format(e))
        class _NoPin3:
            def value(self):
                return 1
        menu_pin = _NoPin3()

    display.text_screen(screens.boot_lines(settings["device_id"], settings.get("wifi_ssid") or "",
                                           display.rows))

    # The LED animator starts BEFORE the join so "joining" (blue solid) is
    # visible while network.connect() waits, then the green "joined" flash
    # and the idle pulse follow without a gap.
    asyncio.create_task(status_led_loop(state, status_led, network, power))

    joined = False
    try:
        joined = await network.connect()
    except Exception as e:
        logger.error("Network: {}".format(e))
    state.joining = False
    if await _handle_join_result(joined, network, settings, display, status_led, state):
        return

    # Discovery: announce ourselves to the network. Best-effort mDNS plus a
    # broadcast beacon that runs whenever no hub is subscribed.
    discovery = Discovery(
        settings,
        subscribers,
        network.sta,
        services={
            "sensor": settings.get("udp_sensor_port", 3141),
            "cmd":    settings.get("cmd_port", 8001),
        },
        caps=state.caps,
        matrix_dims=state.matrix_dims,
        imu_scale=state.imu_scale,
    )

    # Command channel: hubs subscribe + send commands here. The handlers
    # close over our device_state so they can mutate the live runtime.
    # serve_forever() is a supervised loop: if the listener dies (e.g. an
    # mpremote KeyboardInterrupt lands on the asyncio accept task), it
    # logs, sleeps briefly, and rebinds. PROTOCOL.md section 10 requires
    # this for v1.0 conformance.
    cmd_server = CommandServer(
        port=settings.get("cmd_port", 8001),
        handlers=build_handlers(state),
    )

    logger.info("Spawning async tasks")
    asyncio.create_task(cmd_server.serve_forever())
    # Wi-Fi over the USB cable. The role verb reuses the TCP set_role handler
    # so both channels share one behaviour (clamp, apply, persist).
    _tcp_set_role = build_handlers(state)["set_role"]
    async def _usb_set_role(role):
        return (await _tcp_set_role({"role": role}, ("usb", 0)))["role"]
    start_usb_console(settings, _usb_set_role, get_ip=lambda: state.ip)
    asyncio.create_task(sensor_loop(state, sensor_matrix, network, sd))
    asyncio.create_task(display_loop(display, sensor_matrix, state=state))
    # NOTE: menu_loop(menu) is intentionally NOT spawned. MenuManager's
    # check_buttons() busy-waits (`while pin.value()==0: pass`, no await)
    # on any button hold, which froze the entire asyncio event loop and
    # blocked power_mode_loop from accumulating the 2 s MENU hold -> so
    # CHARGE never triggered even though GPIO21 read fine (board 2026-07-02
    # diagnosis: MENU pin verified toggling at the REPL, but the hold was
    # eaten by this loop). power_mode_loop now owns MENU + SELECT; the old
    # OLED menu system is retired to avoid the two fighting for the pins.
    asyncio.create_task(status_loop(state, power, network, imu,
                                    settings.get("status_interval_s", 5)))
    # imu_loop is the sole I2C reader of the IMU; sensor_loop + status_loop
    # consume from state.imu_cache. Default 30 Hz read cadence; data.imu
    # then rides each sensor frame at the sensor rate (PROTOCOL.md 7.1).
    asyncio.create_task(imu_loop(state, imu,
                                 settings.get("imu_interval_ms", 33)))
    asyncio.create_task(discovery.announce_loop())
    # Active discovery listener: phones broadcast {verb:"discover"} on the
    # announce port and we reply with an immediate announce. Catches the
    # "phone just joined a new network and does not want to wait up to a
    # full beacon interval" case. See PROTOCOL.md section 5.5.
    asyncio.create_task(discovery.discover_listener_loop())
    asyncio.create_task(subscriber_prune_loop(subscribers))
    # Power-mode state machine + SELECT-button cycle (board #0220).
    # Drives STREAM/STANDBY/CHARGE transitions, auto-CHARGE on USB+idle,
    # and refreshes VBUS-present + battery-full bits the LED resolver
    # reads. Cheap (100 ms tick, ~1 % CPU).
    asyncio.create_task(power_mode_loop(state, power, select_pin, settings,
                                        menu_pin=menu_pin,
                                        network=network,
                                        sd=sd))
    asyncio.create_task(gc_loop())
    asyncio.create_task(reboot_watcher(state))
    # Diagnostic for the V4 WDT-reset mystery (#0156). Logs gap-since-feed +
    # the current sensor_loop step every 5 s. On a WDT reset, the last
    # canary log line before reboot is the leading edge of the stall.
    asyncio.create_task(wdt_canary_loop())

    # STA-mode provisioning admin: serves GET /info and POST /reprovision
    # on TCP 80 against the device's LAN IP, per PROVISIONING.md section
    # 4.3 ("Phones reach this endpoint over the STA-mode normal LAN; it
    # does not require AP mode"). Bound to the STA interface IP so we do
    # not double-expose the reprovision endpoint when the AP interface is
    # also up briefly during STA bring-up.
    sta_ip = network.local_ip()
    if sta_ip:
        sta_state = provisioning._ProvisioningState(
            settings, SettingsManager,
            info_extras={"caps": state.caps, "matrix": list(state.matrix_dims),
                         "ip": sta_ip, "oled": display.present, "usb_console": 1},
            state="provisioned")
        asyncio.create_task(provisioning.serve(sta_state, bind_ip=sta_ip))
    else:
        logger.warn("No STA IP yet; skipping STA-mode reprovision listener (will not retry this boot)")

    # Watchdog: armed AFTER tasks are spawned so the slow boot + Wi-Fi
    # phase does not trip it. Once armed, sensor_loop must call _feed_wdt()
    # within WDT_TIMEOUT_MS or the chip hard-resets and reset_cause=WDT
    # appears in the next boot's log.
    global _wdt
    try:
        _wdt = machine.WDT(timeout=WDT_TIMEOUT_MS)
        logger.info("Watchdog armed: {}ms".format(WDT_TIMEOUT_MS))
    except Exception as e:
        logger.warn("WDT init failed (may not be supported on this build): {}".format(e))

    while True:
        await asyncio.sleep(1)
