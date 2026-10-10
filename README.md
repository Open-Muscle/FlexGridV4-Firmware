# FlexGrid V4 Firmware

MicroPython firmware for the OpenMuscle FlexGrid V4 wearable pressure-sensor band: a 60-sensor (15 by 4) Velostat grid, a status light, a power button and two firmware buttons, an optional 128x32 screen (kit bands ship without it), an optional microSD card, a motion sensor and a Wi-Fi radio (ESP32-S3). The band streams its pressure grid to the OpenMuscle app on your computer over your own Wi-Fi.

Firmware version `v4.0.0`. MIT licence.

## Where to start

| You are... | Read |
|---|---|
| **Using a band you were given** | [Using your band](#using-your-band) below, then the one-page card in [docs/device-reference.md](docs/device-reference.md#quick-card-print-this-section) |
| Wondering what a light, screen or button means | [docs/device-reference.md](docs/device-reference.md) |
| **Preparing bands to ship to someone** | [docs/kit-prep.md](docs/kit-prep.md) |
| Flashing or rebuilding a band, or developing the firmware | [BRINGUP.md](BRINGUP.md) and [For developers](#for-developers) below |
| Installing the computer app | The OpenMuscle-Software repository: `docs/getting-started.md`, and `docs/wifi-setup.md` for the Wi-Fi steps |

---

## Using your band

### 1. Charge it and switch it on

Plug in a USB-C **data** cable (some cables only charge; either way the band charges). The small CHRG light on the board shows that the charger is working, whatever the band's light says. The band is switched on and off with its power button, the small switch at the top edge: one short press switches it on, one short press again switches it off (press feel is on the confirm list in [docs/device-reference.md](docs/device-reference.md#facts-still-to-confirm-on-a-physical-band)).

Details, charge times and the parts still to be confirmed on hardware are in [docs/device-reference.md](docs/device-reference.md#power-charging-and-the-battery).

### 2. Give it your Wi-Fi (first time only)

You need a Wi-Fi network that is **2.4 GHz**, has a **password**, has **no sign-in page**, and lets devices on it see each other. A home router, an office network without a login page, or a phone hotspot set to 2.4 GHz all work. Hotel, school and cafe networks with a sign-in page, and username-and-password (enterprise) networks, do not.

**Kit bands have no screen**, so a new band shows only its light: **blue, two quick blinks then a pause**. That is the normal setup pattern, not a fault. The setup Wi-Fi name and password are on the **sticker** on the band (`Setup Wi-Fi` and `Setup password`).

A band that does have a screen blinks its light blue once a second and shows this:

```
WIFI SETUP        <- this top line alternates with "see guide"
OM-flexgrid-
d7af0b
PW kd3mpq72xy
```

1. On a phone or laptop open Wi-Fi settings and join the network on the sticker (or named on the screen, if your band has one; read its two lines as one name, `OM-flexgrid-d7af0b`). The password is the `Setup password` on the sticker, or the 10 characters after `PW` on the screen (lower-case letters and the digits 2 to 9 only: never a zero, a one, an `o` or an `l`).
2. Your phone or laptop may say "no internet" or ask to stay connected. Stay connected. Some laptops jump back to a known network: turn off auto-join for it until you are done.
3. Open a web browser and go to `http://192.168.4.1` (type `http://`, not `https://`).
4. Type your Wi-Fi name and password and press **Connect**.
5. The page says the band is restarting. Join your normal Wi-Fi again on the phone or laptop.

The band now tries to join: blue steady light (and `Joining Wi-Fi...` on a screen) for up to 20 seconds. On success the light turns **green** for 2 seconds (a screen shows `WIFI OK` and the band's address), then the band streams the live pressure grid. The light settles to a brief pulse every 3 seconds in the band's role colour (blue left, red right, green labeler, white if no role is set) while it waits for the app.

The computer app can run these steps for you from its device setup page; see the app's Wi-Fi guide.

**If it fails** the light blinks amber (a screen also says `WIFI FAILED` and the reason: `check password`, `network not found or 5 GHz`, or `join timed out`), and about 30 seconds later the band is back at `WIFI SETUP` (blue, two quick blinks then a pause, on a band without a screen) with the same network name and password from the sticker. Join it again and re-enter the details. The reasons and fixes are in [docs/device-reference.md](docs/device-reference.md#if-it-will-not-connect).

### 3. Use it with the app

Start the OpenMuscle app on a computer that is on the same Wi-Fi network. It finds the band by itself. The light turns steady **orange** while the app is receiving data. If it does not appear, see [If it will not connect](docs/device-reference.md#if-it-will-not-connect). If the app asks for the band's address, a band with a screen shows it when you tap MENU; without a screen, read it from your router's device list.

### 4. Everyday controls

| Do this | What happens |
|---|---|
| Tap MENU | Status page on a band with a screen: Wi-Fi, address, id, role. Tap again for battery and mode, again to return to the grid |
| Tap SELECT | Pause or resume sending (Wi-Fi stays on) |
| Hold MENU 2 s | Charge mode on or off (Wi-Fi off while charging) |
| Hold SELECT 2 s | Record to the SD card on or off (needs a card) |
| Hold MENU and SELECT 3 s | Forget the Wi-Fi and go back to `WIFI SETUP` |
| Power button (top edge) | Short press: switch on. Short press again: switch off |
| BOOT | Restart the band |

Two more switches are on the board that you do not normally touch: BOOT restarts the band (it is wired to the chip's reset line), and SW_RST1 (bottom left) is wired to the chip's IO0 boot-strap pin: hold it while pressing BOOT to put a stuck board into download mode for reflashing. The complete tables of lights, screens and buttons are in [docs/device-reference.md](docs/device-reference.md).

### 5. Moving to a different Wi-Fi

Hold MENU and SELECT together for 3 seconds (the light blinks magenta, then the band restarts). The band returns to `WIFI SETUP` (blue, two quick blinks then a pause, on a band without a screen) with the same name and password printed on its sticker. Repeat step 2.

---

## For people preparing kits

[docs/kit-prep.md](docs/kit-prep.md) is a checklist: factory flash, first-boot check, reset-path test, roles, the pre-ship check, stickers, charge level and box contents.

Short version of the tools it uses:

```
python flash.py --factory COM7        # erase, install MicroPython and the firmware, no Wi-Fi details
python tools/kit_check.py COM8 --role left --label "Band A" --clear-logs
python wipe.py COM8 --reset            # clear a used band without reflashing
```

`COM7` is the port a blank band shows (USB id `303a:1001`); `COM8` is the port of a band running MicroPython (`303a:4001`). Details are below and in [BRINGUP.md](BRINGUP.md).

---

## For developers

### What this firmware does

- Scans the 15 by 4 Velostat matrix every 20 ms by default (about 50 Hz; `scan_interval_ms`).
- Reads the ICM-42688-P motion sensor and sends its readings with each frame.
- Announces itself on the local Wi-Fi by mDNS (`_openmuscle._udp`) and a UDP broadcast beacon to port 3140. No hard-coded destination address.
- Accepts subscriptions on a small command channel (TCP 8001) and unicasts each frame to every subscribed computer (data on UDP 3141; at most 4 subscribers, 5 s heartbeat timeout).
- Logs sessions to microSD when asked (hold SELECT).
- Shows its state on an RGB status light, and on a 128x32 screen when one is fitted (kit bands ship without it; the firmware logs `No screen` and carries on).
- BLE is planned for a later phase and is not in this firmware.

### How V4 differs from V3

| Subsystem | V3 | V4 |
|---|---|---|
| Sensor matrix | 15 x 4 Velostat | 15 x 4 Velostat (unchanged) |
| Controller | ESP32-S3-WROOM-1-N16R8 | ESP32-S3-WROOM-1-N16R8 (unchanged) |
| Display | OLED SSD1306 128x32 | OLED SSD1306 128x32 (unchanged; optional, header left unplugged on kit bands) |
| Storage | None | **microSD via SPI** (new) |
| Status indication | OLED only | **3-channel RGB LED on GPIO 40/41/42** (new) |
| Motion sensor | Footprint only, never populated | **ICM-42688-P populated and active** (new) |
| Network model | Hard-coded destination IP in firmware | **Discovery + subscribe** (new protocol) |
| Haptic motor | Footprint only | Driver populated but **defaults off**, opt-in only |

### Flashing

The short route (full steps and the manual route are in [BRINGUP.md](BRINGUP.md)):

```
pip install "esptool==4.8.1" "mpremote==1.25.0" pyserial
python flash.py --factory COM7
```

`--factory` erases the board, installs MicroPython and the firmware, and pushes **no** `config/settings.json`. The band boots to `WIFI SETUP` with no file edits and no credentials. Without `--factory`, `flash.py` pushes your `config/settings.json` (copy `config/settings.example.json`; its Wi-Fi fields are blank, so it also boots to setup unless you fill them in). `config/settings.json` is ignored by git.

A re-flash erases the whole chip, so the band gets a **new device id** (`flexgrid-<6hex>`) and a **new setup password**. A band you flashed earlier shows up as a different device.

### Clearing a used band

`python wipe.py <runtime_com> [--reset]` deletes `config/settings.json`, `log.txt` and `log.txt.bak` over the serial port without reflashing. The next boot mints a new device id and setup password and shows `WIFI SETUP`. Wipe before handing a band to someone else; the log can contain the network name that was used.

### Pre-ship check

`python tools/kit_check.py <runtime_com>` reads a band over USB (it needs `mpremote`) and says PASS or FAIL. It checks that the band is in **factory state** (no Wi-Fi name or password saved, no join history, no `log.txt.bak`, no line in `log.txt` naming a Wi-Fi network), whether the screen answers at I2C address 0x3C (a missing screen is only a warning, because kit bands ship without one and the sticker is then the only place the setup password lives), that the battery is at least 3.7 V, that the band has a well-formed setup password, and that every firmware file on the band is byte-identical to this checkout. The Wi-Fi password is never read back to the PC.

On PASS it prints the sticker text: id, setup network name, setup password, and the role and label if set.

Options: `--role left|right|labeler` and `--label TEXT` write those settings first; `--clear-logs` deletes `log.txt` and `log.txt.bak`; `--require-sd`, `--require-screen` and `--require-tag` turn the SD-card, missing-screen and "checkout is on a git tag" warnings into failures; `--min-battery V` changes the 3.7 V floor; `--reset` reboots the band afterwards. Exit status: 0 PASS, 1 FAIL, 2 the band could not be reached.

### What the band does when the first join fails

Right after provisioning the band tries to join for `sta_join_timeout_s` seconds (default 20). If that very first join fails it does not retry for ever:

| Step | What happens |
|---|---|
| Joining | Screen (if fitted): boot screen with `Joining Wi-Fi...` and your network name. Light: blue, steady. |
| Failed | Screen (if fitted): `WIFI FAILED`, the reason, `back to setup` for about 2 s. Light: fast amber blink. The Wi-Fi password is cleared, the network name is kept as `last_ssid`, then the band reboots. |
| Back in setup mode | Same `OM-flexgrid-<6hex>` name and same `PW` as before (on the sticker). Without a screen the light returns to blue, two quick blinks then a pause. The top line of a screen adds `LAST TRY FAILED` and the reason to its rotation. `GET /info` reports `last_wifi_error` and `last_ssid`. About 30 s pass between the reboot after provisioning and the band being back in setup mode. |
| Joined | Light: green, steady, for 2 s. Screen (if fitted): the status page (`WIFI OK` and the band's IP address) for 15 s, then the grid. `wifi_joined_ok` becomes true. From now on a failed join (router off, out of range) never clears the credentials; the radio keeps retrying. |

`last_wifi_error` values: `wrong_password` (screen `check password`), `no_ap_found` (`network not found or 5 GHz`), `timeout` (`join timed out`). `POST /provision`, `POST /reprovision` and the MENU + SELECT wipe all reset `wifi_joined_ok` to false.

### Provisioning HTTP details

The setup server (`http://192.168.4.1`, WPA2 network `OM-flexgrid-<6hex>`, channel 1, up to 4 clients) reads the request properly: headers up to the blank line, then exactly `Content-Length` bytes, within 3 seconds. `GET /` is a small web form, `GET /info` returns the band's identity as JSON, and `POST /provision` takes `{"ssid": "...", "password": "..."}`. The SSID is limited to 32 bytes and the password to 8..63 bytes (UTF-8 bytes, not characters; an empty password means an open network), and both are stored exactly as sent. `POST /reprovision` also works on a joined band (on its LAN address): it clears the Wi-Fi settings, mints a new setup password and restarts.

| Status | `data.message` | Meaning |
|---|---|---|
| 400 | `ssid_length_invalid (1..32 bytes)` | empty, blank, not a string, or over 32 bytes |
| 400 | `password_length_invalid (8..63 bytes for WPA2, or empty)` | wrong length |
| 400 | `invalid_json: ...` | body is not a JSON object |
| 400 | `incomplete_body` | fewer bytes arrived than `Content-Length` promised |
| 400 | `bad_content_length` | `Content-Length` is not a number |
| 413 | `request_too_large` | body over 2048 bytes or headers over 4096 bytes |

### Settings

Set these in `config/settings.json` (a missing key takes its default; see `lib/settings_manager.py` for the full list).

| Key | Default | Meaning |
|---|---|---|
| `wifi_ssid`, `wifi_password` | empty | empty means "not set up": the band boots to setup mode |
| `role` | empty | `left`, `right` or `labeler`; sets the idle light colour (`kit_check --role` or the app sets it) |
| `device_label` | empty | free text for the sticker, set by `tools/kit_check.py --label` |
| `scan_interval_ms` | `20` | sensor scan period |
| `sta_join_timeout_s` | `20` | seconds to wait for the join (clamped to 5..60) |
| `record_enabled` | `true` | allow the SELECT-hold RECORD mode |
| `auto_charge_idle_s` | `300` | idle seconds on USB before automatic charge mode; `0` disables |
| `display_height` | `32` | screen height in pixels for a band that has a screen: `32` (128x32) or `64` |
| `low_battery_v` | `3.55` | volts under which the light blinks amber and the screen warns (USB not detected) |
| `vbus_stat_pin` | none | GPIO of a real USB-detect line; set to `39` only on V4.2 boards, where it is wired |
| `wifi_joined_ok`, `last_wifi_error`, `last_ssid` | | kept by the firmware; do not set by hand |

### Tests

The Wi-Fi fallback, the provisioning HTTP handler, the power-mode guards, the LED state table, the screens, the flashing and pre-ship tools and the documentation are covered by desktop tests that stub out the MicroPython modules. They need only Python 3 (no board, no network):

```
python -m unittest discover -s tests
```

(`python -m pytest tests` works too if pytest is installed.) Set `OM_PROVISIONING_PATH` to another copy of the provisioning module, for example the LASK5 one, to run the same HTTP checks against it.

### Repo layout

```
FlexGridV4-Firmware/
├── README.md
├── BRINGUP.md                    (flashing and bring-up steps)
├── LICENSE                       (MIT, OpenMuscle)
├── docs/
│   ├── device-reference.md       (lights, screens, buttons, power; one-page quick card)
│   └── kit-prep.md               (checklist for preparing bands to ship)
├── boot.py                       (boot sequence, sys.path setup)
├── main.py                       (entry point)
├── flexgrid.py                   (main loop, wires the subsystems together)
├── flash.py                      (erase + flash + push; --factory for a no-credentials band)
├── wipe.py                       (clear settings.json and logs on a used band)
├── tools/kit_check.py            (pre-ship check: factory state, screen, battery, firmware, sticker text)
├── config/settings.example.json  (template; blank Wi-Fi credentials)
├── tests/                        (desktop unit tests, run with unittest)
└── lib/
    ├── pinmap.py                 (V4 GPIO assignments)
    ├── settings_manager.py       (settings + device id)
    ├── logger.py                 (persistent log to flash)
    ├── power_manager.py          (battery, USB guess, MAX16054 soft power)
    ├── sensor_matrix.py          (15x4 Velostat scan, mux driver)
    ├── display_manager.py        (OLED SSD1306)
    ├── ssd1306.py                (OLED driver from micropython-lib)
    ├── screens.py                (text layouts: setup, join failure, status, mode banners)
    ├── provisioning.py           (setup-mode Wi-Fi network + HTTP provisioning API)
    ├── menu_manager.py           (retired on-screen menu; not started)
    ├── status_led.py             (RGB LED PWM control + state palette)
    ├── imu.py                    (ICM-42688-P read)
    ├── sd_logger.py              (microSD session recording)
    ├── discovery.py              (mDNS + UDP broadcast announce)
    ├── subscribers.py            (subscriber list + heartbeat)
    ├── commands.py               (command channel decode/dispatch)
    └── network_manager.py        (Wi-Fi + protocol send/receive)
```

### Protocol summary

Implements the OpenMuscle v1.0 protocol per the canonical specs:
- Discovery + transport spec: [`OpenMuscle-Connect/docs/DEVICE-DISCOVERY-SPEC.md`](https://github.com/Open-Muscle/OpenMuscle-Connect/blob/main/docs/DEVICE-DISCOVERY-SPEC.md)
- Wire format: [`OpenMuscle-Connect/docs/WIRE-FORMAT.md`](https://github.com/Open-Muscle/OpenMuscle-Connect/blob/main/docs/WIRE-FORMAT.md)

Announce (mDNS TXT keys and the UDP broadcast JSON to port 3140) carries, among other fields, the band's id (`flexgrid-<6hex>`), `dev`, `fw`, `caps`, the matrix size `[15, 4]` and the service ports (`sensor` 3141, `cmd` 8001). See `lib/discovery.py` for the exact payload.

## Licence

MIT. See `LICENSE`.
