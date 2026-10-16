# V4 Bring-Up Guide

How to put MicroPython and the V4 firmware on a FlexGrid V4 board, check that it works, and fix the usual problems. If you only want to use a band that was given to you, you do not need this page: see the [README](README.md). If you are preparing bands for someone else, follow [docs/kit-prep.md](docs/kit-prep.md), which uses the steps below.

The short version:

```
pip install "esptool==4.8.1" "mpremote==1.25.0" pyserial
python flash.py --factory COM7
python tools/kit_check.py COM8
```

## One-time setup

1. **Install the Python tools** into the Python you will run the scripts with:

   ```
   pip install "esptool==4.8.1" "mpremote==1.25.0" pyserial
   ```

   `flash.py` was tested with esptool 4.8.1 and mpremote 1.25.0, and it calls esptool with the 4.x command names (`erase_flash`, `write_flash`, `--before usb_reset`). Newer esptool releases have not been tried with it here.

2. **Download the MicroPython image** for the ESP32-S3 generic board and put it in a `firmware/` folder in this repository:

   ```
   mkdir firmware
   curl -L "https://micropython.org/resources/firmware/ESP32_GENERIC_S3-20260406-v1.28.0.bin" -o firmware/ESP32_GENERIC_S3-v1.28.0.bin
   ```

   `flash.py` finds `firmware/ESP32_GENERIC_S3*.bin` by itself (or use the `MICROPYTHON_BIN` environment variable). Newer images are at <https://micropython.org/download/ESP32_GENERIC_S3/>. The `firmware/` folder is ignored by git.

3. **Know your serial ports.** List them with:

   ```
   python -m mpremote connect list
   ```

   A blank band, or one in download mode, shows as USB id `303a:1001` (the **bootloader port**). A band running MicroPython shows as `303a:4001` (the **runtime port**). The number changes when the band restarts. On Windows ports are `COM7`; on macOS `/dev/cu.usbmodem...`; on Linux `/dev/ttyACM0` (you may need to be in the `dialout` group).

## Flash a band (the normal way)

Plug the band in with a USB-C **data** cable and run:

```
python flash.py --factory COM7
```

`COM7` is the port the band shows now. The script then:

1. Puts the band into download mode itself if it is running MicroPython (a 1200-baud touch). No button is needed.
2. Erases the whole chip and writes MicroPython.
3. Waits for the band to restart and find its new port.
4. Pushes the firmware files over one raw-REPL connection, with `boot.py` and `main.py` last so a half-installed band never starts.
5. Restarts the band.

**`--factory` pushes no `config/settings.json`, and none is needed in your checkout.** The erase already cleared the chip, so the band starts with no Wi-Fi details, makes a new id and setup password, and starts setup mode within a few seconds.

**Kit bands have no screen** (its header is left unplugged), and the firmware carries on without it (it logs `No screen`). The light blinks blue, **two quick blinks then a pause**: that is the normal setup pattern for a band without a screen, not a fault. Setup mode needs the USB cable plugged in: on battery alone the band cannot start its setup network, and its light stays at a faint glow (always on while the band is powered; completely dark means switched off). A band that already has its Wi-Fi joins and runs on battery. Read the setup network name and password with `python tools/kit_check.py <port>`; it prints them as the sticker text on a PASS, and still prints them (marked read only, not for a sticker) when the check FAILs, for example while the battery is low or after the band has joined a network. The sticker is the only place the password is shown, so print one for every band.

A band that does have a screen blinks its light blue once a second and shows this:

```
WIFI SETUP
OM-flexgrid-
d7af0b
PW kd3mpq72xy
```

The top line alternates with `see guide`. The network name is split over two lines (read them as one name). If you expected a screen and the light still blinks twice, check the display connector, and run `python tools/kit_check.py <port> --require-screen`, which fails and prints the I2C scan when nothing answers at 0x3C.

Every flash gives the band a **new id and a new setup password**. A band you flashed before shows up as a new device, and any sticker made earlier is wrong.

### Options

| Command | Use |
|---|---|
| `python flash.py --factory COM7` | Erase and flash; no Wi-Fi details. For any band that will be handed to someone. |
| `python flash.py --factory COM7 COM8` | The same, naming the runtime port yourself (use when the script cannot tell which port is new). |
| `python flash.py COM7` | Also pushes your `config/settings.json`. For your own bench boards (see below). |

## Check a band

### The pre-ship check

```
python tools/kit_check.py COM8 --role left --label "Band A" --clear-logs
```

Reads the band over USB. It fails with a readable reason when the band is not in factory state, the battery is missing, or the firmware differs from this checkout. A missing screen is only a warning (kit bands ship without one); `--require-screen` makes it a failure. On PASS it prints the text for the band's sticker. Options and the failure messages are in the [README](README.md#pre-ship-check) and [docs/kit-prep.md](docs/kit-prep.md#e-roles-clean-up-and-the-pre-ship-check).

### The announcement check (after the band has joined a network)

A band that has joined a network broadcasts a small JSON announcement to UDP port **3140** about once a second, until a computer subscribes to it. Listen for it from a computer on the same network:

```
python -c "import socket, json; s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(('', 3140)); s.settimeout(15); print(json.loads(s.recvfrom(4096)[0]))"
```

You should see the band's `id` (`flexgrid-<6hex>`), `dev`, `fw`, `caps` and `services` fields. (Port 3141 is where the sensor data goes after a computer subscribes; announcements are not sent there.) If nothing arrives: the band may not have joined (look at the light, and the screen if it has one), the computer may be on a different network or behind "client isolation", the computer's firewall may be blocking UDP 3140, or the app is already receiving from the band, which silences the announcements.

### What you should see after joining

Blue steady while joining, green steady for 2 s once joined, then the pulse in the role colour. A band with a screen shows `WIFI OK` and the band's address for 15 s; tap MENU at any time to see it again (a second tap shows battery, mode and the number of computers receiving, a third returns to the live grid). A band without a screen shows only the light; find its address in your router's device list or in the app. You can use that address to add the band by hand in the app if automatic discovery does not find it.

## Give a band Wi-Fi over the USB cable

A band running this firmware accepts its Wi-Fi name and password as a JSON command on its USB serial port, in setup mode and on a joined band, which is how the PC app sets a band up. It is also the quickest way to move a bench band to another network without the setup page. See [docs/usb-console.md](docs/usb-console.md) for the commands and a short `pyserial` example. `flash.py` pushes `lib/usb_console.py` with the rest of the firmware. Ctrl-C from `mpremote` still stops the program as before.

## Developer shortcut: preload Wi-Fi details

For your own bench boards you can skip the setup screen:

```
cp config/settings.example.json config/settings.json
```

Put your Wi-Fi name and password in it, then run `python flash.py COM7` (no `--factory`). `config/settings.json` is ignored by git so credentials are never committed. The example has blank details, so a board flashed with the unedited copy still boots to `WIFI SETUP`. A band flashed this way holds a Wi-Fi password and is **not** in factory state: do not give it to anyone without running `python wipe.py <runtime_com> --reset` first.

## Clear a used band without reflashing

```
python wipe.py COM8          # COM8 = the band's runtime port (303a:4001)
python wipe.py COM8 --reset  # same, then restart it
```

This deletes `config/settings.json`, `log.txt` and `log.txt.bak`. The firmware stays installed; the next boot starts at `WIFI SETUP` with a new id and setup password. Do this before a band changes hands: the settings hold the Wi-Fi password and the log can hold the network name. Without `--reset` the band is left idle at the REPL so the files stay gone; unplug it or press BOOT to start it.

## Flash by hand (for debugging)

Use this only when `flash.py` cannot do what you need. Replace the ports with yours.

```
# 1. Find the bootloader port (303a:1001)
python -m mpremote connect list

# 2. Erase and write MicroPython (esptool 4.8.1 command names)
python -m esptool --chip esp32s3 --port COM7 --before usb_reset --after no_reset erase_flash
python -m esptool --chip esp32s3 --port COM7 --before usb_reset --after hard_reset write_flash 0x0 firmware/ESP32_GENERIC_S3-v1.28.0.bin

# 3. Wait for the band to restart; it appears on a new port (303a:4001)
python -m mpremote connect list

# 4. Confirm MicroPython is running
python -m mpremote connect COM8 exec "import sys; print(sys.implementation)"

# 5. Push the files, from inside this folder. Everything first, boot.py and main.py last.
python -m mpremote connect COM8 fs mkdir :config
python -m mpremote connect COM8 fs cp flexgrid.py :flexgrid.py
python -m mpremote connect COM8 fs cp -r lib :
python -m mpremote connect COM8 fs cp boot.py :boot.py
python -m mpremote connect COM8 fs cp main.py :main.py
# only for a band that should hold Wi-Fi details:
python -m mpremote connect COM8 fs cp config/settings.json :config/settings.json

# 6. Restart
python -m mpremote connect COM8 exec "import machine; machine.reset()"
```

The OLED driver `ssd1306` is not part of the stock MicroPython image. This repository carries the `micropython-lib` copy at `lib/ssd1306.py`, and both `flash.py` and the `fs cp -r lib :` line above install it. The manual route is slower and flakier on the native USB port than `flash.py`; prefer the script.

## Download mode

You do not need to hold a button to flash a band.

- A blank band, and a band that `flash.py` bounces with its 1200-baud touch, appears as `303a:1001` and esptool can reach it.
- The silkscreen **BOOT** button (BOOT1) on the V4 board is wired to the chip's **reset** line (EN), not to GPIO 0. Pressing it restarts the band; holding it does not enter download mode. (This is why the firmware gives BOOT no action.)
- The switch printed **SW_RST1** (bottom left) is wired to the net the schematic labels `100`, which is the ESP32-S3 **IO0** boot-strap pin. For a stuck band that `flash.py` cannot reach: hold SW_RST1, press and release BOOT, then let go of SW_RST1. The chip starts in download mode and shows up as `303a:1001`; run `flash.py` again. In normal use SW_RST1 does nothing, and the firmware never reads that pin.

If a band does not respond to `flash.py` at all, try the SW_RST1 and BOOT sequence above, then a different cable and port, then see the USB notes below.

## Troubleshooting

### "Failed to connect to ESP32-S3: No serial data received"

- Check that the board shows up: `python -m mpremote connect list` should list a `303a:1001` or `303a:4001` device. If nothing is listed, Windows is not enumerating the chip: try another cable (many are charge-only) and another port, then see the USB notes below.
- Check the tool versions: `python -m esptool version` (4.8.1 is the tested one) and `python -m mpremote version` (1.25.0).
- If the band is running MicroPython, give `flash.py` its port: it will bounce the band into the bootloader and carry on. It prints which port the bootloader appeared on.

### "Could not enter raw REPL"

The running firmware can flood the serial line. Unplug the band, plug it in again, wait 10 s and retry. `flash.py` pushes everything over one connection and writes `main.py` last to avoid this.

### Windows shows "Unknown USB Device (Device Descriptor Request Failed)", or the chime plays but no COM port appears

The USB data lines are not working. See the USB notes below (assembly), and try another cable.

### `ImportError: no module named 'ssd1306'`

The OLED driver is missing on the band. Push it:

```
python -m mpremote connect COM8 fs cp lib/ssd1306.py :lib/ssd1306.py
```

### The band never shows up in the app

Check the log on the band:

```
python -m mpremote connect COM8 fs cat :log.txt
```

If the log says "Wi-Fi did not join within 20s (wrong_password)" (or `no_ap_found`, `timeout`), the Wi-Fi name or password is wrong, or the network is 5 GHz only. On a band whose first join has never succeeded, the firmware then clears the saved password and restarts into `WIFI SETUP`, which shows the same reason on the screen (if fitted) and in `GET http://192.168.4.1/info` as `last_wifi_error`. Enter the details again from there. A band that has joined once keeps its details and keeps retrying, so a router that is off for a while is not a problem. If the log looks fine, run the announcement check above and check the computer's network and firewall.

If you preloaded details, check what is on the band: `python -m mpremote connect COM8 fs cat :config/settings.json` (this prints the Wi-Fi password; do not paste it anywhere).

### Pressing SELECT stopped the streaming

A short tap on SELECT pauses (slow cyan breathing light; `PAUSED` on a screen); tap again to resume. Holding SELECT for 2 s starts RECORD (SD logging, Wi-Fi off). It only starts when an SD card mounts; without one a screen shows `NO SD CARD` and the band keeps streaming. If the light is breathing green (`RECORDING` on a screen), tap SELECT once: the band stops the session, restarts and rejoins Wi-Fi. To disable RECORD on a band, set `"record_enabled": false` in its `config/settings.json`.

### The band disappeared from the app after sitting on USB

Charge mode turns Wi-Fi off. It starts when you hold MENU for 2 s, or by itself after `auto_charge_idle_s` (default 300) seconds with USB detected and no computer receiving. The light pulses purple (a screen shows `CHARGING / Wi-Fi off / unplug to resume / or tap SELECT`). Unplug USB, tap SELECT or hold MENU for 2 s to restart onto Wi-Fi.

## Notes for people assembling bare boards

These apply to boards you populate yourself, not to a band that arrived assembled.

### USB protection chip orientation (as-built V4 schematic)

The V4 schematic wires the USBLC6-2SC6 with pin 2 to VBUS and pin 5 to GND, which is backwards: the chip's pin 2 is GND and pin 5 is VCC. With the schematic-correct placement USB shows power only (you hear the connect chime) but the data lines are clamped by the chip's protection diodes, so no COM port appears. The workaround on the as-built boards is to rotate the chip 180 degrees from the silkscreen; the package is symmetric so the signal pin pairs still route correctly. A later schematic revision (V4.2) carries the fix. After the rotation the board enumerates as `303a:1001` (bootloader) or `303a:4001` (running).

### USB-detect line

The as-built V4 does not wire a USB-detect line to the processor, so the firmware infers USB from the battery voltage (see [docs/device-reference.md](docs/device-reference.md#power-charging-and-the-battery)). A V4.2 board wires one to GPIO 39; enable it with `"vbus_stat_pin": 39` in `config/settings.json` on those boards only (on an as-built V4 that pin floats and would toggle charge mode at random).

## Running the desktop tests

```
python -m unittest discover -s tests
```

No board is needed; the MicroPython modules are stubbed.
