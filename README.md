# FlexGrid V4 Firmware

MicroPython firmware for the OpenMuscle FlexGrid V4 wearable sensor bracelet. ESP32-S3-WROOM-1-N16R8 host, 60-sensor Velostat matrix on the flex PCB, plus new V4 additions: ICM-42688-P IMU, onboard microSD logging, and an RGB status LED.

> **Status: in development (June 2026).** Built against the OpenMuscle device-discovery + dual-transport protocol (see [`OpenMuscle-Connect/docs/DEVICE-DISCOVERY-SPEC.md`](https://github.com/Open-Muscle/OpenMuscle-Connect/blob/main/docs/DEVICE-DISCOVERY-SPEC.md) and `WIRE-FORMAT.md`). V4 boards are in fab; firmware will be flashed and brought up when boards arrive.

## What this firmware does

- Scans the 15 by 4 Velostat sensor matrix at the configured rate (default ~59 Hz).
- Reads the ICM-42688-P IMU for orientation.
- Announces itself on the local Wi-Fi network via mDNS (`_openmuscle._udp`) and a UDP broadcast beacon fallback. No hardcoded destination IP.
- Accepts hub subscriptions via a small command channel; unicasts each sensor frame to every subscribed hub (cap: 4 subscribers, 5 s heartbeat timeout).
- Logs sessions to microSD when triggered (untethered mode).
- Drives an RGB status LED to surface device state at a glance.
- Adds BLE GATT advertising and notification (phase 4, after Wi-Fi is bedded in).

## How V4 differs from V3 (firmware-relevant)

| Subsystem | V3 | V4 |
|---|---|---|
| Sensor matrix | 15 x 4 Velostat | 15 x 4 Velostat (unchanged) |
| Controller | ESP32-S3-WROOM-1-N16R8 | ESP32-S3-WROOM-1-N16R8 (unchanged) |
| Display | OLED SSD1306 128x32 | OLED SSD1306 128x32 (unchanged) |
| Storage | None | **microSD via SPI** (new) |
| Status indication | OLED only | **3-channel RGB LED on GPIO 40/41/42** (new) |
| IMU | Footprint only, never populated | **ICM-42688-P populated and active** (new) |
| Network model | Hardcoded destination IP in firmware | **Discovery + subscribe** (new protocol) |
| Bluetooth | None | **BLE GATT** (planned phase 4) |
| Haptic motor | Footprint only | Driver populated but **defaults OFF**, opt-in only |

## Wi-Fi setup and recovery

A band with no Wi-Fi credentials boots into **setup mode**: it raises its own WPA2 network (`OM-flexgrid-<6hex>`, with a random per-device password) and serves a small provisioning API on `http://192.168.4.1`. The OLED shows everything needed to join it; nothing is truncated:

```
WIFI SETUP
OM-flexgrid-
d7af0b
PSK kd3mpq72xy
```

Join that network from a phone or laptop, then open `http://192.168.4.1/` (a simple page) or `POST /provision` with `{"ssid": "...", "password": "..."}`. The band saves the credentials, reboots and joins your network. Use a **2.4 GHz** network: the radio cannot see 5 GHz, and sign-in pages (school, hotel or office) will not work.

### What the band does when the first join fails

Right after provisioning the band tries to join for `sta_join_timeout_s` seconds (default 20). If that very first join fails it does not retry forever:

| Step | What happens |
|---|---|
| Joining | OLED: `Joining Wi-Fi...` |
| Failed | OLED `WIFI FAILED` plus the reason for about 2 s, amber LED. The Wi-Fi password is cleared, the network name is kept as `last_ssid`, then the band reboots. |
| Back in setup mode | Same `OM-flexgrid-<6hex>` name and same `PSK` as before. The OLED adds `LAST TRY FAILED`, the reason and the network that was tried. `GET /info` reports `last_wifi_error` and `last_ssid`. About 30 s pass between the reboot after provisioning and the band being back in setup mode. |
| Joined | `wifi_joined_ok` becomes true. From now on a failed join (router off, out of range) never clears the credentials; the radio keeps retrying and the band reconnects when the router returns. |

`last_wifi_error` values:

| Value | OLED text | Usual cause |
|---|---|---|
| `wrong_password` | `check password` | typo in the password |
| `no_ap_found` | `network not found or 5 GHz` | misspelled name, out of range, or a 5 GHz-only network |
| `timeout` | `join timed out` | the router never answered |

A band that has never joined and is provisioned while its router is off will also land back in setup mode; enter the credentials again once the router is up. `POST /provision`, `POST /reprovision` and the MENU+SELECT wipe all reset `wifi_joined_ok` to false.

### Provisioning HTTP details

The setup server reads the request properly: headers up to the blank line, then exactly `Content-Length` bytes, within 3 seconds. A client that sends headers and body in separate TCP segments works. The SSID is limited to 32 bytes and the password to 8..63 bytes (UTF-8 bytes, not characters), and both are stored exactly as sent, with no whitespace stripping.

| Status | `data.message` | Meaning |
|---|---|---|
| 400 | `ssid_length_invalid (1..32 bytes)` | empty, blank, not a string, or over 32 bytes |
| 400 | `password_length_invalid (8..63 bytes for WPA2, or empty)` | wrong length (empty means an open network) |
| 400 | `invalid_json: ...` | body is not a JSON object |
| 400 | `incomplete_body` | fewer bytes arrived than `Content-Length` promised |
| 400 | `bad_content_length` | `Content-Length` is not a number |
| 413 | `request_too_large` | body over 2048 bytes or headers over 4096 bytes |

## Buttons and power modes

| Control | Action |
|---|---|
| Tap SELECT | Toggle PAUSED (standby): Wi-Fi stays on, streaming stops |
| Hold MENU 2 s | Toggle CHARGE: Wi-Fi radio off, CPU 80 MHz. Leaving CHARGE reboots the band |
| Hold SELECT 2 s | Start RECORD (SD logging, Wi-Fi off). Tap SELECT to stop; the band reboots back onto Wi-Fi |
| Hold MENU + SELECT 3 s | Forget Wi-Fi and return to setup mode |

Every mode other than streaming replaces the heatmap on the OLED with a banner (`PAUSED`, `CHARGE MODE`, `RECORDING`) that says whether Wi-Fi is on and how to get out.

RECORD cannot cost you your Wi-Fi by accident. It only starts once the SD card has mounted and a session file is open. With no card the band stays on Wi-Fi and streaming and shows `NO SD CARD / Record needs one / Wi-Fi stays on` for 3 seconds. Set `"record_enabled": false` in `config/settings.json` to disable the SELECT hold entirely.

CHARGE also starts by itself when USB is present and no hub has been subscribed for `auto_charge_idle_s` seconds (default 300; it used to be 30). Set it to `0` to turn the automatic switch off and use the MENU hold only.

## Settings added for setup and recovery

| Key | Default | Meaning |
|---|---|---|
| `wifi_joined_ok` | `false` | set after the first successful join; while false a failed join returns the band to setup mode |
| `last_wifi_error` | `""` | why the last first-join attempt failed (see table above) |
| `last_ssid` | `""` | the network that attempt used |
| `sta_join_timeout_s` | `20` | seconds to wait for the join (clamped to 5..60) |
| `record_enabled` | `true` | allow the SELECT-hold RECORD mode |
| `auto_charge_idle_s` | `300` | idle seconds on USB before auto-CHARGE; `0` disables |

The first three are maintained by the firmware; do not set them by hand.

## Factory flashing and wiping

`python flash.py --factory <bootloader_com>` erases the board, installs MicroPython and the firmware, and pushes **no** `config/settings.json`. The band boots to `WIFI SETUP` with no file edits. Without `--factory`, `flash.py` pushes your `config/settings.json` (copy `config/settings.example.json`; its Wi-Fi fields are blank, so it also boots to setup unless you fill them in).

A re-flash erases the whole chip, so the band gets a **new device id** (`flexgrid-<6hex>`) and a **new setup password**. A band you flashed earlier will show up as a different device.

`python wipe.py <runtime_com>` clears a used band without re-flashing: it deletes `config/settings.json`, `log.txt` and `log.txt.bak` over the serial port (use `--reset` to reboot it afterwards). The next boot mints a new device id and setup password and shows `WIFI SETUP`. Wipe before handing a band to someone else; the log can contain the network name that was used.

## Tests

The Wi-Fi fallback, the provisioning HTTP handler, the power-mode guards and the flashing tools are covered by desktop tests that stub out the MicroPython modules. They need only Python 3 (no board, no network):

```
python -m unittest discover -s tests
```

(`python -m pytest tests` works too if pytest is installed.) Set `OM_PROVISIONING_PATH` to another copy of the provisioning module, for example the LASK5 `om_provisioning.py`, to run the same HTTP checks against it.

## Repo layout

```
FlexGridV4-Firmware/
├── README.md
├── LICENSE                       (MIT, OpenMuscle)
├── boot.py                       (boot sequence, sys.path setup)
├── main.py                       (entry point)
├── flexgrid.py                   (main loop, wires the subsystems together)
├── flash.py                      (erase + flash + push; --factory for a blank-credentials unit)
├── wipe.py                       (clear settings.json and logs on a used band)
├── config/settings.example.json  (template; blank Wi-Fi credentials)
├── tests/                        (desktop unit tests, run with unittest)
└── lib/
    ├── pinmap.py                 (V4 GPIO assignments)
    ├── settings_manager.py       (NVS-backed settings + device UUID)
    ├── logger.py                 (persistent log to flash)
    ├── power_manager.py          (battery, charger, MAX16054 soft power)
    ├── sensor_matrix.py          (15x4 Velostat scan, mux driver)
    ├── display_manager.py        (OLED SSD1306)
    ├── screens.py                (OLED text layouts: setup, join failure, mode banners)
    ├── provisioning.py           (setup-mode Wi-Fi AP + HTTP provisioning API)
    ├── menu_manager.py           (boot/select/menu buttons)
    ├── status_led.py             (NEW: RGB LED PWM control + state palette)
    ├── imu.py                    (NEW: ICM-42688-P read)
    ├── sd_logger.py              (NEW: microSD session recording)
    ├── discovery.py              (NEW: mDNS + UDP broadcast announce)
    ├── subscribers.py            (NEW: subscriber list + heartbeat)
    ├── commands.py               (NEW: command channel decode/dispatch)
    ├── network_manager.py        (REWRITTEN: Wi-Fi + new protocol send/receive)
    └── ble.py                    (PHASE 4: BLE GATT service + advertising)
```

## Protocol summary

Implements the OpenMuscle v1.0 protocol per the canonical specs:
- Discovery + transport spec: [`OpenMuscle-Connect/docs/DEVICE-DISCOVERY-SPEC.md`](https://github.com/Open-Muscle/OpenMuscle-Connect/blob/main/docs/DEVICE-DISCOVERY-SPEC.md)
- Wire format: [`OpenMuscle-Connect/docs/WIRE-FORMAT.md`](https://github.com/Open-Muscle/OpenMuscle-Connect/blob/main/docs/WIRE-FORMAT.md)

Announce payload (mDNS TXT + UDP broadcast JSON):
```json
{
  "v": "1.0",
  "type": "announce",
  "id": "flexgrid-<6hex>",
  "role": "source",
  "dev": "flexgrid",
  "fw": "v4.0.0",
  "transports": ["wifi"],
  "caps": ["sensor", "status", "cmd", "imu"],
  "matrix": [15, 4],
  "services": { "sensor": 3141, "cmd": 8001 }
}
```

In phase 4, `transports` becomes `["wifi","ble"]` and a BLE GATT service appears in advertisements.

## License

MIT. See `LICENSE`.
