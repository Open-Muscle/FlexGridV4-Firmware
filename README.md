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

A band with no Wi-Fi credentials boots into **setup mode**: it raises its own WPA2 network (`OM-flexgrid-<6hex>`, with a random per-device password) and serves a small provisioning API on `http://192.168.4.1`. The OLED shows everything needed to join it; nothing is truncated. The network name is longer than the 16-character screen, so it is split over two lines (read them as one name), and the password line starts with `PW `:

```
WIFI SETUP        <- this top row rotates every 3 s:
OM-flexgrid-         WIFI SETUP, "see guide", and after a
d7af0b               failed join "LAST TRY FAILED" and the reason
PW kd3mpq72xy
```

The LED blinks blue once a second while the band is in setup mode. If the OLED does not answer (a dead or unplugged display) the LED blinks blue twice in quick succession instead, because then nobody can read the password; see the LED guide below.

Join that network from a phone or laptop, then open `http://192.168.4.1/` (a simple page) or `POST /provision` with `{"ssid": "...", "password": "..."}`. The band saves the credentials, reboots and joins your network. Use a **2.4 GHz** network: the radio cannot see 5 GHz, and sign-in pages (school, hotel or office) will not work.

### What the band does when the first join fails

Right after provisioning the band tries to join for `sta_join_timeout_s` seconds (default 20). If that very first join fails it does not retry forever:

| Step | What happens |
|---|---|
| Joining | OLED: the boot screen with `Joining Wi-Fi...` and your network name. LED: blue, steady. |
| Failed | OLED `WIFI FAILED`, the reason and `back to setup` for about 2 s, LED fast amber blink. The Wi-Fi password is cleared, the network name is kept as `last_ssid`, then the band reboots. |
| Back in setup mode | Same `OM-flexgrid-<6hex>` name and same `PW` as before. The top row of the OLED adds `LAST TRY FAILED` and the reason to its rotation. `GET /info` reports `last_wifi_error` and `last_ssid`. About 30 s pass between the reboot after provisioning and the band being back in setup mode. |
| Joined | LED: green, steady, for 2 s. OLED: the status page (`WIFI OK` and the band's IP address) for 15 s, then the heatmap. `wifi_joined_ok` becomes true. From now on a failed join (router off, out of range) never clears the credentials; the radio keeps retrying and the band reconnects when the router returns. |

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

## What the band shows (OLED and LED)

The OLED is **128x32**: four lines of 16 characters, and the heatmap is the full 15 x 4 grid at 8 pixels per cell. Every screen below is written for four lines. A 128x64 panel works too: set `"display_height": 64` in `config/settings.json` and the text screens use eight lines.

### Screens

| Screen | When | Lines |
|---|---|---|
| Boot | power-up, while joining | `OpenMuscle V4` / device id / `Joining Wi-Fi...` / your network name |
| Setup | no Wi-Fi saved | top row rotates `WIFI SETUP`, `see guide`, failure reason; then the network name over two lines and `PW <password>` |
| Join failed | first join did not work | `WIFI FAILED` / the reason (one or two lines) / `back to setup` |
| Status page 1 | MENU tap, and 15 s after joining | `WIFI OK` / **IP address** / device id / role (`role: left`, or `no role set`) |
| Status page 2 | second MENU tap | battery (`BAT 87% 3.95V`, or `LOW 12% 3.42V`) / `MODE STREAM` / `HUBS 1` / `NET <your network>` |
| Heatmap | normal running | the live 15 x 4 pressure grid |
| Paused | tap SELECT | `PAUSED` / `Wi-Fi is on` / `not streaming` / `SELECT to resume` |
| Charging | hold MENU 2 s, or USB plugged in and idle | `CHARGING` / `Wi-Fi off` / `unplug to resume` / `or tap SELECT` |
| Recording | hold SELECT 2 s (SD card needed) | `RECORDING` / `to SD, Wi-Fi OFF` / `tap SELECT to` / `stop & reconnect` |
| Notices (3 to 4 s) | something refused or low | `NO SD CARD`, `RECORD OFF`, `LOW BATTERY` |

If Wi-Fi is down the first status page reads `WIFI LOST / retrying...`; in CHARGE and RECORD it reads `WIFI OFF`. The page cycle is heatmap, page 1, page 2, heatmap; each page also times out after 15 s. A MENU tap is a short press; holding MENU for 2 s still switches CHARGE on and off.

### LED guide

One table for every state the LED can show.

| LED | Meaning | Palette name |
|---|---|---|
| Blue, one blink per second | **Setup mode**: read the OLED, join the band's own network | `setup` |
| Blue, two quick blinks then a pause | Setup mode but the **OLED is not answering** (service needed) | `setup_no_oled` |
| Blue, steady | **Joining** your Wi-Fi | `joining` |
| Green, steady for 2 s | **Joined** (also each time Wi-Fi comes back); then the idle pulse | `joined` |
| Amber, fast blink | First join **failed**; the band is going back to setup | `join_failed` |
| Red, fast blink | Recovered from a crash (watchdog), for the first minute | `fault` |
| Magenta, blinking | MENU + SELECT held: letting go now **wipes the Wi-Fi settings** | `ap_provisioning` |
| Purple, pulsing | MENU held 2 s: letting go switches CHARGE; also CHARGE while charging | `charging` |
| Purple, steady | CHARGE and the battery is full | `charge_full` |
| Green, slow breathing | RECORD (and the preview while SELECT is held 2 s) | `record` |
| Cyan, slow breathing | PAUSED (standby) | `standby` |
| Amber, one blink per second | **Low battery** (under 3.55 V, USB not plugged in) | `low_battery` |
| Amber, slow pulse | **Wi-Fi lost**; the band keeps retrying | `wifi_lost` |
| Orange, steady | Streaming to a hub | `streaming` |
| Orange with a cyan flash every 5 s | Streaming while USB is plugged in | `streaming_on_usb` |
| Short pulse every 3 s | Waiting for a hub; colour is the **role**: blue left, red right, green labeler, white not set | `idle_left`, `idle_right`, `idle_labeler`, `idle_unassigned` |

The first rows are the start-up story in order: blue blink (setup), blue steady (joining), green (joined), then the idle pulse in the role colour. The three amber states differ by pattern: fast blink (join failed), one blink a second (low battery), slow pulse (Wi-Fi lost).

## Buttons and power modes

| Control | Action |
|---|---|
| Tap MENU | Show the status page (IP address, Wi-Fi, battery, id, role); tap again for the next page, then the heatmap |
| Tap SELECT | Toggle PAUSED (standby): Wi-Fi stays on, streaming stops |
| Hold MENU 2 s | Toggle CHARGE: Wi-Fi radio off, CPU 80 MHz. Leaving CHARGE reboots the band |
| Hold SELECT 2 s | Start RECORD (SD logging, Wi-Fi off). Tap SELECT to stop; the band reboots back onto Wi-Fi |
| Hold MENU + SELECT 3 s | Forget Wi-Fi and return to setup mode |

Every mode other than streaming replaces the heatmap on the OLED with a banner (`PAUSED`, `CHARGING`, `RECORDING`) that says whether Wi-Fi is on and how to get out. If you let go of one button of a MENU + SELECT hold before the 3 s are up, nothing happens: the other button is ignored until both are released.

RECORD cannot cost you your Wi-Fi by accident. It only starts once the SD card has mounted and a session file is open. With no card the band stays on Wi-Fi and streaming and shows `NO SD CARD / Record needs one / Wi-Fi stays on` for 3 seconds. Set `"record_enabled": false` in `config/settings.json` to disable the SELECT hold entirely.

CHARGE also starts by itself when USB is present and no hub has been subscribed for `auto_charge_idle_s` seconds (default 300; it used to be 30). Set it to `0` to turn the automatic switch off and use the MENU hold only.

## Settings added for setup, recovery and status

| Key | Default | Meaning |
|---|---|---|
| `wifi_joined_ok` | `false` | set after the first successful join; while false a failed join returns the band to setup mode |
| `last_wifi_error` | `""` | why the last first-join attempt failed (see table above) |
| `last_ssid` | `""` | the network that attempt used |
| `sta_join_timeout_s` | `20` | seconds to wait for the join (clamped to 5..60) |
| `record_enabled` | `true` | allow the SELECT-hold RECORD mode |
| `auto_charge_idle_s` | `300` | idle seconds on USB before auto-CHARGE; `0` disables |
| `display_height` | `32` | OLED height in pixels: `32` (shipped 128x32) or `64` |
| `low_battery_v` | `3.55` | volts under which the LED blinks amber and the OLED warns (USB unplugged) |
| `device_label` | `""` | free text for the sticker, set by `tools/kit_check.py --label` |

The first three are maintained by the firmware; do not set them by hand.

## Factory flashing and wiping

`python flash.py --factory <bootloader_com>` erases the board, installs MicroPython and the firmware, and pushes **no** `config/settings.json`. The band boots to `WIFI SETUP` with no file edits. Without `--factory`, `flash.py` pushes your `config/settings.json` (copy `config/settings.example.json`; its Wi-Fi fields are blank, so it also boots to setup unless you fill them in).

A re-flash erases the whole chip, so the band gets a **new device id** (`flexgrid-<6hex>`) and a **new setup password**. A band you flashed earlier will show up as a different device.

`python wipe.py <runtime_com>` clears a used band without re-flashing: it deletes `config/settings.json`, `log.txt` and `log.txt.bak` over the serial port (use `--reset` to reboot it afterwards). The next boot mints a new device id and setup password and shows `WIFI SETUP`. Wipe before handing a band to someone else; the log can contain the network name that was used.

## Pre-ship check

`python tools/kit_check.py <runtime_com>` reads a band over USB (it needs `mpremote`) and says PASS or FAIL. It checks that the band is in **factory state** (no Wi-Fi name or password saved, no join history, no `log.txt.bak`, no line in `log.txt` naming a Wi-Fi network), that the OLED answers at 0x3C, that the battery is at least 3.7 V, that the band has a well-formed setup password, and that every firmware file on the band is byte-identical to this checkout. A `log.txt` that holds only boot lines from the band's own setup network is fine. The Wi-Fi password is never read back to the PC.

On PASS it prints the sticker text: device id, setup network name, setup password, and the role and label if set.

```
python tools/kit_check.py COM7 --role left --label "Band A" --clear-logs
```

`--role left|right|labeler` and `--label` write those settings first; `--clear-logs` deletes `log.txt` and `log.txt.bak`; `--require-sd` and `--require-tag` turn the SD card and "this checkout is exactly on a git tag" warnings into failures; `--min-battery V` changes the 3.7 V floor; `--reset` reboots the band afterwards. Exit status is 0 for PASS, 1 for FAIL, 2 when the band could not be reached. A reboot after the check makes a fresh `log.txt` that only holds the band's own boot lines, which still passes.

## Tests

The Wi-Fi fallback, the provisioning HTTP handler, the power-mode guards, the LED state table, the OLED screens and the flashing and pre-ship tools are covered by desktop tests that stub out the MicroPython modules. They need only Python 3 (no board, no network):

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
├── tools/kit_check.py            (pre-ship check: factory state, OLED, battery, firmware, sticker text)
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
