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


# Reset-cause names. ESP32-S3 MicroPython exposes the abstracted constants;
# brownout typically surfaces as PWRON_RESET (1) on this build, since the
# brownout detector reboots through the same path. If you see PWRON_RESET
# at an inconvenient time mid-recording, suspect a Wi-Fi-TX-burst brownout.
_RESET_CAUSES = {
    1: "POWER_ON",     # cold boot OR brownout on ESP32-S3
    2: "HARD",         # external RST line / chip enable
    3: "WDT",          # watchdog timeout, task got stuck
    4: "DEEPSLEEP",
    5: "SOFT",         # Ctrl-D / machine.soft_reset()
    6: "BROWNOUT",     # some builds use this; treat as suspect
}


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


def _feed_wdt():
    if _wdt is not None:
        try:
            _wdt.feed()
        except Exception:
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
    """
    n = 0
    err_count = 0
    while True:
        # Pet the watchdog FIRST so it is the first thing that stops
        # happening if this loop wedges.
        _feed_wdt()
        interval_ms = state.scan_interval_ms
        meta_every = max(1, 1000 // max(1, interval_ms))
        try:
            matrix = sensor_matrix.scan_matrix()
            n += 1
            if state.streaming:
                meta = device_status if (n % meta_every == 0) else None
                await network.send_sensor(matrix, meta=meta)
            # SD recording is independent of streaming; the user may want
            # to log offline while no hub is around.
            if sd.is_recording():
                sd.write_frame(matrix)
            err_count = 0
        except Exception as e:
            if err_count == 0 or err_count % 100 == 0:
                logger.error("sensor_loop iter #{} failed: {}".format(err_count, e))
            err_count += 1
        await asyncio.sleep_ms(interval_ms)


async def display_loop(display, sensor_matrix, interval_ms=66):
    """Render at ~15 Hz independent of scan rate. I2C at 400 kHz takes ~22 ms
    per full frame; 66 ms gives the bus plenty of slack and keeps CPU free."""
    while True:
        try:
            display.draw_sensor_matrix(sensor_matrix.matrix)
        except Exception as e:
            logger.warn("display_loop draw failed: {}".format(e))
        await asyncio.sleep_ms(interval_ms)


async def menu_loop(menu):
    """Poll buttons frequently for responsive UI."""
    while True:
        try:
            menu.check_buttons()
        except Exception as e:
            logger.warn("menu_loop check failed: {}".format(e))
        await asyncio.sleep_ms(50)


async def status_loop(state, power, network, imu, interval_s=5):
    """Refresh `device_status` and emit a REPL heartbeat. The dict is
    consumed by sensor_loop (attached as packet meta) and by status_led_loop
    (drives the connection-state palette)."""
    while True:
        try:
            v = power.battery_voltage()
            p = power.battery_percent()
            uptime_s = time.ticks_ms() // 1000
            free_mem = gc.mem_free()
            rssi = network.rssi()
            imu_snap = imu.read() and imu.status_summary()

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
        except Exception as e:
            logger.warn("status_loop iter failed: {}".format(e))
        await asyncio.sleep(interval_s)


async def subscriber_prune_loop(subscribers, interval_s=1):
    """Drop subscribers whose last heartbeat aged past the timeout."""
    while True:
        try:
            dropped = subscribers.prune_stale()
            if dropped:
                logger.info("Pruned {} stale subscriber(s); remaining={}".format(
                    dropped, subscribers.count()))
        except Exception as e:
            logger.warn("subscriber_prune_loop failed: {}".format(e))
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
        except Exception as e:
            logger.warn("status_led_loop failed: {}".format(e))
        await asyncio.sleep_ms(interval_ms)


async def gc_loop(interval_s=2):
    """Manual GC pacing. ESP32 MicroPython tends to let the heap fragment
    under steady allocation pressure; periodic explicit collect keeps it flat."""
    while True:
        gc.collect()
        await asyncio.sleep(interval_s)


async def reboot_watcher(state):
    """Poll the reboot flag set by a `reboot` command. We sleep briefly so
    the ack can flush, then soft_reset."""
    while True:
        if state.reboot_requested:
            logger.info("Soft-resetting in 500 ms...")
            await asyncio.sleep_ms(500)
            machine.soft_reset()
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
    asyncio.create_task(discovery.announce_loop())
    asyncio.create_task(subscriber_prune_loop(subscribers))
    asyncio.create_task(status_led_loop(state, status_led, network))
    asyncio.create_task(gc_loop())
    asyncio.create_task(reboot_watcher(state))

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
