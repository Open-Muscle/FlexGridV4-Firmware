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
import logger

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
                logger.info(
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
                logger.info("wdt: gap={}ms op={}".format(gap_ms, _current_op))
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
            logger.info("BAT {:.2f}V ({}%) up={}s rssi={} mem={} subs={} {}".format(
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


async def status_led_loop(state, status_led, network, interval_ms=33):
    """Drive the RGB LED state palette based on live device state. Runs at
    ~30 Hz so breathe / blink animations look smooth.

    State machine (highest priority wins):
       error          -> any error flag in device_status (TODO: wire this up)
       wifi_lost      -> Wi-Fi disconnected
       boot           -> Wi-Fi present but no subscribers yet (initial state)
       recording      -> SD recording active
       predicting     -> reserved for future hub-side prediction echo
       streaming      -> at least one subscriber currently receiving
       idle           -> Wi-Fi up, no subscribers (settled into the idle state)
    """
    has_been_subscribed = False
    while True:
        try:
            now_ms = time.ticks_ms()
            if not network.is_connected():
                status_led.set_state("wifi_lost")
            elif state.subscribers.count() > 0:
                status_led.set_state("streaming")
                has_been_subscribed = True
            elif has_been_subscribed:
                # We have served at least one hub; settle into idle.
                status_led.set_state("idle")
            else:
                # Booted, Wi-Fi up, never subscribed yet.
                status_led.set_state("boot")
            status_led.animate(now_ms)
        except (asyncio.CancelledError, SystemExit):
            raise
        except BaseException as e:
            logger.warn("status_led_loop failed: {} ({})".format(type(e).__name__, e))
        await asyncio.sleep_ms(interval_ms)


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
    power         = PowerManager()
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
    asyncio.create_task(subscriber_prune_loop(subscribers))
    asyncio.create_task(status_led_loop(state, status_led, network))
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
