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

        # Sensor scan
        "scan_interval_ms":  20,               # ~50 Hz default; can be tuned via cmd
        "status_interval_s": 5,                # how often power/wifi/heap status refreshes

        # New V4 subsystems
        "imu_enabled":       True,             # ICM-42688-P read in main loop
        "sd_logging":        False,            # local microSD session recording (off by default)
        "haptic_enabled":    False,            # IRLML2060 driver untested on V4, opt-in only
        "status_palette":    "default",        # named palette for the RGB status LED

        # Display
        "display_brightness": 255,
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
            print("[ERR] Could not save settings:", e)
            return False
