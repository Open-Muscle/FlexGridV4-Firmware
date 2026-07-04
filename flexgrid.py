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
from network_manager  import NetworkManager
from power_manager    import PowerManager
from status_led       import StatusLed
from subscribers      import Subscribers
from discovery        import Discovery
from commands         import CommandServer, build_handlers
from imu              import IMU
from sd_logger        import SDLogger
import provisioning


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
            # to log offline while no hub is around.
            if sd.is_recording():
                _current_op = "sd_write"
                sd.write_frame(matrix)
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


async def display_loop(display, sensor_matrix, interval_ms=200):
    """Render at ~5 Hz independent of scan rate.

    Was 66 ms (~15 Hz). Bumped to 200 ms during the 60Hz profile work
    (#0179) because the SSD1306 I2C frame takes ~22 ms, so at 66 ms
    the display was eating ~33% of bus / CPU time, blocking sensor_loop
    iterations that overlapped a draw. At 200 ms display CPU drops to
    ~11% (frees ~22 ms of CPU per 200 ms window) without meaningfully
    hurting the live-readout UX. Easy to revert by passing
    interval_ms=66 at the call site.
    """
    while True:
        try:
            display.draw_sensor_matrix(sensor_matrix.matrix)
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
    ~30 Hz so breathe / blink animations look smooth.

    Resolution priority (highest wins, locked palette per board #0220):

        fault              -> in_fault set (recent WDT) -- RED fast blink
        sleep              -> in_sleep set (gyro stillness in STREAM) -- WHITE dim breath
        ap_provisioning    -> only reachable on the AP-mode boot branch
        STANDBY            -> purple slow breath
        CHARGE + full      -> solid cyan
        CHARGE + charging  -> cyan slow pulse
        STREAM + subs + USB-> orange solid + brief cyan flash every 5s
        STREAM + subs      -> solid orange
        STREAM + idle      -> role-color brief pulse every 3s
                              (BLUE left, RED right, GREEN labeler, WHITE unassigned)

    Wi-Fi-down is intentionally not surfaced -- the device degrading
    Wi-Fi only matters while a hub is trying to use it, and Tory wants
    the role-pulse to remain dominant when the device is sitting idle
    on a desk. If the operator needs Wi-Fi diagnosis they read get_info
    or watch the OLED, both of which are clearer than a blinking LED.
    """
    while True:
        try:
            now_ms = time.ticks_ms()
            named = _resolve_led_state(state, power)
            status_led.set_state(named)
            status_led.animate(now_ms)
        except (asyncio.CancelledError, SystemExit):
            raise
        except BaseException as e:
            logger.warn("status_led_loop failed: {} ({})".format(type(e).__name__, e))
        await asyncio.sleep_ms(interval_ms)


def _resolve_led_state(state, power):
    """Pure helper: map the current (power_mode, role, has_subs, vbus,
    fault, sleep) tuple to one palette name. Kept side-effect-free so
    the resolver logic is easy to unit-test off-device by feeding it a
    fake state object."""
    if getattr(state, "in_fault", False):
        return "fault"
    if getattr(state, "in_sleep", False):
        return "sleep"
    # Button-hold preview beats mode display: while BOOT is held past a
    # threshold the LED shows which action RELEASING now will trigger.
    hp = getattr(state, "hold_preview", None)
    if hp == "wipe":
        return "ap_provisioning"   # magenta blink = release wipes Wi-Fi
    if hp == "charge":
        return "charging"          # purple pulse = release toggles charge
    mode = getattr(state, "power_mode", "stream")
    if mode == "standby":
        return "standby"
    if mode == "charge":
        return "charge_full" if power.is_battery_full() else "charging"
    # STREAM. Treat a hub-paused state (subs > 0 but state.streaming
    # is False because a hub called stop_stream) as idle, so the LED
    # does not lie about whether data is flowing.
    has_active_subs = state.subscribers.count() > 0 and state.streaming
    vbus = power.is_vbus_present()
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
                          tick_ms=100,
                          auto_charge_idle_s=30,
                          fault_clear_s=60,
                          charge_hold_s=2):
    """Drive the STREAM/STANDBY/CHARGE state machine + SELECT button cycle.

    Responsibilities (board #0220):
      - Sample SELECT (active-LOW with pull-up) at tick_ms; debounce
        on falling edge; on press, advance state.cycle_power_mode().
      - Every 10 ticks (1 s) update power.update_vbus_state() so the
        LED resolver sees fresh VBUS + battery-full bits.
      - Auto-enter CHARGE: when power_mode==stream + USB present +
        idle (no subscribers) for auto_charge_idle_s seconds.
      - Auto-exit CHARGE: when USB unplugs while in CHARGE, drop
        straight back to STREAM (the user's expected default).
      - Clear in_fault after fault_clear_s of clean runtime so the
        red-blink reverts to normal once the device has demonstrated
        stability.

    CONTROL SCHEME (V4 button topology confirmed from schematic
    2026-07-02: MENU=GPIO21, SELECT=GPIO10; the "BOOT" button is wired
    to EN = hardware reset, NOT a readable GPIO, so it can only reset
    the chip and carries no firmware action):

      MENU held 2 s        -> toggle CHARGE mode
      SELECT short press    -> toggle STANDBY <-> STREAM
      (in CHARGE) either    -> leave CHARGE (reboots for clean Wi-Fi)
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

    Hardened with SystemExit/CancelledError re-raise + BaseException
    catch matching the rest of the V4 loops (board #0166)."""

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
            SettingsManager.save(settings)
        except Exception as e:
            logger.error("Cred wipe failed; NOT rebooting: {}".format(e))
            return
        machine.reset()

    DEBOUNCE_MS = 250
    tick_count = 0
    menu_held_ticks = 0
    menu_latched = False
    chord_held_ticks = 0
    chord_latched = False
    last_select_ms = 0
    charge_hold_ticks = max(1, (charge_hold_s * 1000) // tick_ms)   # 2 s
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
            else:
                if chord_held_ticks:
                    state.hold_preview = None
                chord_held_ticks = 0
                chord_latched = False

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
                else:
                    if menu_held_ticks:
                        state.hold_preview = None
                    menu_held_ticks = 0
                    menu_latched = False

                    # C) SELECT short press (only when MENU isn't held).
                    #    In CHARGE -> leave (reboot). Else toggle STANDBY.
                    if sel_down and time.ticks_diff(now_ms, last_select_ms) > DEBOUNCE_MS:
                        last_select_ms = now_ms
                        if state.power_mode == "charge":
                            _leave_charge("SELECT")   # reboots
                        elif state.power_mode == "standby":
                            state.power_mode = "stream"
                            state.streaming = True
                            logger.info("SELECT: STANDBY -> STREAM")
                        else:
                            state.power_mode = "standby"
                            state.streaming = False
                            logger.info("SELECT: STREAM -> STANDBY")

            # D) Network-requested mode (set_power_mode cmd verb). The ack
            #    already flushed before this tick, so acting here (incl.
            #    a reboot to leave charge) does not eat the reply.
            req = state.requested_power_mode
            if req:
                state.requested_power_mode = None
                if req == "charge":
                    if state.power_mode != "charge":
                        _enter_charge("cmd")
                elif req == "stream":
                    if state.power_mode == "charge":
                        _leave_charge("cmd stream")   # reboots into stream
                    else:
                        state.power_mode = "stream"
                        state.streaming = True
                elif req == "standby":
                    if state.power_mode == "charge":
                        _leave_charge("cmd standby")  # reboot lands in stream
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
# Boot sequence
# ---------------------------------------------------------------------------

async def main():
    # Open the on-flash log FIRST so everything that follows is persisted.
    logger.init_persistent()
    logger.info("FlexGrid V4 startup")

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
        # on the OLED) and status LED (solid blue for setup mode).
        display    = DisplayManager()
        status_led = StatusLed()
        status_led.set_rgb(0, 0, 255)

        # Bring the AP up FIRST so we know the PSK (provisioning.run mints it
        # if missing), then paint the OLED with the final SSID + PSK.
        psk = provisioning.start_ap(
            settings, settings["device_type"], settings["device_id"])
        SettingsManager.save(settings)
        display.text_screen([
            "OpenMuscle",
            "WIFI SETUP",
            provisioning.ssid_for(settings["device_type"], settings["device_id"]),
            "PSK " + psk,
        ])

        info_extras = {
            "caps":   ["sensor", "status", "cmd", "imu"],
            "matrix": [15, 4],
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
    display       = DisplayManager()
    sensor_matrix = SensorMatrix()
    network       = NetworkManager(settings, subscribers)
    menu          = MenuManager(display, network, power)
    status_led    = StatusLed()

    # IMU and SD are V4-only. Both are best-effort: if the hardware is not
    # present, the firmware degrades gracefully.
    imu = IMU()
    imu.init()
    sd = SDLogger(settings)
    if sd.enabled:
        sd.mount()

    # DeviceState carries the live mutable state command handlers may mutate.
    state = _DeviceState(settings, subscribers)
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

    display.text_screen([
        "OpenMuscle",
        "FlexGrid V4",
        settings["device_id"],
        "BAT {:.2f}V".format(power.battery_voltage()),
        "Booting...",
    ])

    try:
        await network.connect()
    except Exception as e:
        logger.error("Network: {}".format(e))

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
    asyncio.create_task(sensor_loop(state, sensor_matrix, network, sd))
    asyncio.create_task(display_loop(display, sensor_matrix))
    asyncio.create_task(menu_loop(menu))
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
    asyncio.create_task(status_led_loop(state, status_led, network, power))
    # Power-mode state machine + SELECT-button cycle (board #0220).
    # Drives STREAM/STANDBY/CHARGE transitions, auto-CHARGE on USB+idle,
    # and refreshes VBUS-present + battery-full bits the LED resolver
    # reads. Cheap (100 ms tick, ~1 % CPU).
    asyncio.create_task(power_mode_loop(state, power, select_pin, settings,
                                        menu_pin=menu_pin,
                                        network=network))
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
            info_extras={"caps": state.caps, "matrix": list(state.matrix_dims)},
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
