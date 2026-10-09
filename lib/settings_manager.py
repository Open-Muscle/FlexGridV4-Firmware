# lib/settings_manager.py
#
# V4 changes vs V3:
#   - Default device_id removed; minted at first boot as flexgrid-<6hex> from a
#     UUID stored in NVS/flash. Replaces V3's static "flexgrid-v3-01" default.
#   - udp_target_ip removed; the new discovery + subscribe model replaces it.
#     Legacy "udp_target_ip" is read back for migration logs only and is no
#     longer used to send frames.
#   - New defaults: sd_logging, imu_enabled, haptic_enabled, status_palette.

import json
import uos
import os


# ---------------------------------------------------------------------------
# Device id helper. On first boot a random 6-hex tag is generated and pinned
# in the saved settings; on subsequent boots the same id is read back so the
# hub side sees a stable identity across power cycles.
# ---------------------------------------------------------------------------
def _mint_device_id():
    try:
        # MicroPython os.urandom is available on ESP32 ports.
        b = os.urandom(3)
    except Exception:
        # Fallback: time-based, less unique but never blocks boot.
        import time
        t = time.ticks_ms() & 0xFFFFFF
        b = bytes(((t >> 16) & 0xFF, (t >> 8) & 0xFF, t & 0xFF))
    hex6 = "{:02x}{:02x}{:02x}".format(b[0], b[1], b[2])
    return "flexgrid-" + hex6


class SettingsManager:
    DEFAULTS = {
        # Identity. device_id is minted at first boot if missing.
        "device_id":         None,             # filled by load() if absent
        "device_type":       "flexgrid",
        "fw_version":        "v4.0.0",

        # Wi-Fi. Empty by default so an unprovisioned device boots to AP mode
        # per PROVISIONING.md state machine (unprovisioned -> AP -> POST
        # /provision -> persisted). Never commit real SSID or password here;
        # config/settings.json (gitignored) is the operator-supplied path.
        "wifi_ssid":         "",
        "wifi_password":     "",

        # First-join fallback (PROVISIONING.md section 2). wifi_joined_ok is
        # False until the band has joined its network once; while False, a
        # failed join clears the credentials and reboots into setup (AP) mode
        # instead of retrying forever. Once True the creds are never dropped
        # automatically, so a router that is off for hours is just waited out.
        # /provision and the MENU+SELECT wipe set it back to False.
        # last_wifi_error (wrong_password | no_ap_found | timeout) and
        # last_ssid (the network that failed) are shown on the OLED and in
        # GET /info while in setup mode. sta_join_timeout_s bounds the wait.
        "wifi_joined_ok":    False,
        "last_wifi_error":   "",
        "last_ssid":         "",
        "sta_join_timeout_s": 20,

        # Provisioning PSK. Per-device random 10-char string minted by
        # lib/provisioning.mint_psk() on first AP-mode entry and persisted
        # here. Out-of-band delivery via OLED line 4 per PROVISIONING.md
        # section 3; never derived from the device id. Default is empty so
        # the first AP-mode entry mints + persists a fresh PSK.
        "provisioning_psk":  "",

        # Discovery + transport (PROTOCOL.md v1.0 port split)
        # No udp_target_ip in V4. Sources unicast to subscribed hubs only.
        "udp_announce_port": 3140,             # UDP broadcast port for discovery announces
        "udp_sensor_port":   3141,             # UDP unicast port for sensor/label data frames
        "cmd_port":          8001,             # command channel (TCP) port
        "mdns_service":      "_openmuscle._udp",
        "announce_interval_s": 1,              # how often to broadcast the fallback beacon while unsubscribed
        "max_subscribers":   4,                # spec section 5.1
        "heartbeat_timeout_s": 5,              # drop subscribers older than this
        # Monotonic restart counter; incremented once per boot in
        # flexgrid.py main() so the announce envelope carries it
        # (PROTOCOL.md section 5.6) and phones can detect a fresh boot
        # vs same session, flushing cached subscription state.
        "boot_seq":          0,

        # Role tag (board #0220). Pushed from the phone via the
        # set_role cmd verb. Drives the idle-LED hue (BLUE left,
        # RED right, GREEN labeler, WHITE unassigned). Persistent so
        # the device remembers which side it's worn on across reboots.
        "role":              "",
        # Free-text name for the sticker (set by tools/kit_check.py --label).
        # Informational only; the firmware never reads it.
        "device_label":      "",

        # Hardware VBUS-detect GPIO. None on as-built V4 (the R19/R20
        # divider exists only in the V4.2 schematic; GPIO39 floats on
        # these boards). Set to 39 on V4.2+ hardware. None = firmware
        # infers USB presence from the VBAT threshold.
        "vbus_stat_pin":     None,

        # Sensor scan
        "scan_interval_ms":  20,               # ~50 Hz default; can be tuned via cmd
        "status_interval_s": 5,                # how often power/wifi/heap status refreshes

        # New V4 subsystems
        "imu_enabled":       True,             # ICM-42688-P read in main loop
        "sd_logging":        False,            # local microSD session recording (off by default)
        # RECORD mode (hold SELECT 2 s: Wi-Fi off, log to SD). It only starts
        # when the SD card mounts; set False to disable the hold completely
        # (e.g. units handed to people who never use the SD card).
        "record_enabled":    True,
        # Seconds on USB with no hub subscribed before the band switches
        # itself to CHARGE (Wi-Fi off). 0 disables the automatic switch; hold
        # MENU for 2 s to enter CHARGE by hand.
        "auto_charge_idle_s": 300,
        "haptic_enabled":    False,            # IRLML2060 driver untested on V4, opt-in only
        "status_palette":    "default",        # named palette for the RGB status LED

        # Display. The shipped OLED is 128x32 (4 text lines). Set
        # display_height to 64 only for a 128x64 panel; any other value is
        # treated as 32.
        "display_brightness": 255,
        "display_height":    32,

        # Battery volts under which the LED blinks amber and the OLED says
        # LOW BATTERY (only when USB is not plugged in).
        "low_battery_v":     3.55,
    }

    @staticmethod
    def load():
        try:
            with open('config/settings.json', 'r') as f:
                d = json.load(f)
        except Exception:
            d = {}

        # Backfill defaults (handy across firmware upgrades; new keys land
        # with their default value without losing existing user settings).
        for k, v in SettingsManager.DEFAULTS.items():
            d.setdefault(k, v)

        # Mint a stable device id on first boot if absent. Save it back so
        # the next boot reads the same value.
        if not d.get("device_id"):
            d["device_id"] = _mint_device_id()
            SettingsManager.save(d)

        return d

    @staticmethod
    def save(settings):
        try:
            try:
                uos.stat('config')
            except OSError:
                uos.mkdir('config')
            with open('config/settings.json', 'w') as f:
                json.dump(settings, f)
            return True
        except Exception as e:
            # Print so it's visible on a connected UART AND log so it's
            # captured in the persistent log for unattended failures
            # (e.g. flash full, FS corruption mid-provisioning). Audit
            # pass per board #0188.
            print("[ERR] Could not save settings:", e)
            try:
                import logger
                logger.error("settings.save failed: {} ({})".format(
                    type(e).__name__, e))
            except Exception:
                pass
            return False
