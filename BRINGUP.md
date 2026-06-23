# V4 Bring-Up Guide

Step-by-step for flashing MicroPython + V4 firmware onto a fresh V4 board. Captures the gotchas from the first board so subsequent boards go quickly.

## One-time setup (do once on your dev machine)

1. **Install Python tools** (esptool 5.x + mpremote):

   ```bash
   pip install --upgrade esptool mpremote pyserial
   ```

   The bring-up was done with esptool 5.2.0. ESP32-S3 native-USB reset only works reliably on esptool 5.x; older 4.x fails to sync via the USB-Serial-JTAG path.

2. **Download the MicroPython firmware**:

   ```bash
   mkdir -p firmware
   curl -L "https://micropython.org/resources/firmware/ESP32_GENERIC_S3-20260406-v1.28.0.bin" -o firmware/ESP32_GENERIC_S3-v1.28.0.bin
   ```

   The repo's `flash.py` looks for `firmware/ESP32_GENERIC_S3*.bin` automatically. Newer releases are at <https://micropython.org/download/ESP32_GENERIC_S3/>.

3. **Create `config/settings.json` from the example**:

   ```bash
   cp config/settings.example.json config/settings.json
   ```

   Edit it and put your real Wi-Fi SSID and password in. This file is gitignored so the credentials never get pushed.

## Known hardware quirks on V4 boards

### USBLC6 ESD chip pinout error (HARDWARE BUG)

The V4 KiCad schematic wires the USBLC6-2SC6 with pin 2 = VBUS and pin 5 = GND. **This is backwards.** The actual chip pinout is pin 2 = GND and pin 5 = VCC. With the schematic-correct placement, USB will enumerate VBUS only (you hear the Windows chime) but the data lines are clamped to garbage by the now-forward-biased internal ESD diodes, so no COM port appears.

**Workaround until the schematic is fixed:** when populating the board, rotate the USBLC6 chip 180 degrees from the silkscreen orientation. The SOT-23-6 footprint is symmetric, so the I/O pin pairs (1+4, 3+6) still route correctly, and VCC and GND end up on the right pins. After rotation, USB enumerates correctly as either 303a:1001 (DFU mode) or 303a:4001 (USB-Serial-JTAG mode) depending on the bootloader state.

**Fix the schematic in the next revision.** Swap pin 2 and pin 5 connections on the USBLC6 footprint so future fabs come up correctly without rotation.

### ssd1306 OLED driver not in stock MicroPython

The OLED driver `ssd1306` is not bundled with the ESP32_GENERIC_S3 MicroPython build. The V4 firmware imports it via `display_manager.py`. A canonical copy from `micropython-lib` is committed at `lib/ssd1306.py` and `flash.py` pushes it as part of the firmware load.

## Putting a V4 board in DFU mode (download mode)

Required before esptool can flash MicroPython on a fresh board.

1. Hold the BOOT button (BTN_BOOT, GPIO 0)
2. While holding BOOT, briefly tap RESET (or unplug + replug USB if RESET is not soldered)
3. Continue holding BOOT for ~1 second after RESET
4. Release BOOT

Verify with `python -m mpremote connect list`. The board should show VID:PID `303a:1001` (ROM bootloader CDC). If it still shows `303a:4001`, the BOOT button is not holding GPIO 0 low during reset (button not soldered, cold joint, or wrong button press sequence).

If the BOOT or RESET buttons are not yet hand-soldered, short GPIO 0 to GND with a jumper at the BOOT button footprint or via the J1 programming header during the reset.

## Flashing a board: the easy way

Once the board is in DFU mode and you know its COM port:

```bash
python flash.py COM7
```

This runs the full sequence: erase, write MicroPython, wait for re-enumeration, push all firmware files, reset. See `flash.py --help` (well, just the docstring at the top) for arguments.

After flash completes, give the board ~20 seconds to boot and join Wi-Fi, then verify discovery is working:

```bash
python -c "import socket, json; s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(('', 3141)); print(json.loads(s.recvfrom(4096)[0]))"
```

You should see a JSON announce payload with the board's auto-minted `id` (format `flexgrid-<6hex>`), `dev`, `fw`, `caps`, and `services` fields.

## Flashing a board: the manual way (for debugging)

```bash
# 1. Identify the DFU COM port
python -m mpremote connect list   # look for 303a:1001

# 2. Erase + flash MicroPython
python -m esptool --chip esp32s3 --port COM7 --before usb-reset erase-flash
python -m esptool --chip esp32s3 --port COM7 --before usb-reset write-flash 0 firmware/ESP32_GENERIC_S3-v1.28.0.bin

# 3. Wait for board to reset and re-enumerate (usually on a different COM number)
python -m mpremote connect list   # now look for 303a:4001

# 4. Confirm MicroPython is running
python -m mpremote connect COM8 exec "import sys; print(sys.implementation)"

# 5. Push files (assumes you cd'd into the FlexGridV4-Firmware folder)
python -m mpremote connect COM8 fs mkdir :lib
python -m mpremote connect COM8 fs mkdir :config
python -m mpremote connect COM8 fs cp boot.py :boot.py
python -m mpremote connect COM8 fs cp main.py :main.py
python -m mpremote connect COM8 fs cp flexgrid.py :flexgrid.py
python -m mpremote connect COM8 fs cp lib/ :lib/
python -m mpremote connect COM8 fs cp config/settings.json :config/settings.json

# 6. Reset and boot V4 firmware
python -m mpremote connect COM8 exec "import machine; machine.reset()"
```

## Troubleshooting

### "Failed to connect to ESP32-S3: No serial data received"

- Confirm esptool version: `python -m esptool version` should be 5.x. Older 4.x does not handle native USB reset.
- Confirm the board is in DFU mode: `python -m mpremote connect list` should show `303a:1001` for the V4 board. If it shows `303a:4001`, repeat the BOOT + RESET dance.
- If `mpremote connect list` shows nothing for the V4 board (only the TourBox and other devices), Windows is not enumerating the chip. Most likely the USBLC6 wiring bug (see Known hardware quirks above) or a bad USB cable / port.

### "Unknown USB Device (Device Descriptor Request Failed)" in Device Manager

The USB data lines are broken. On a V4 board this is almost always the USBLC6 chip being placed with the silkscreen orientation rather than rotated 180 degrees. See Known hardware quirks.

### `ImportError: no module named 'ssd1306'`

The OLED driver is not in stock MicroPython. The firmware repo includes a copy at `lib/ssd1306.py`. If you flashed via the manual path and skipped that file, push it explicitly:

```bash
python -m mpremote connect COM8 fs cp lib/ssd1306.py :lib/ssd1306.py
```

### Board boots but never broadcasts a beacon

Check the persistent log:

```bash
python -m mpremote connect COM8 fs cat :log.txt
```

If the log says "Wi-Fi did not join within 20s", verify `config/settings.json` on the device has the right SSID and password:

```bash
python -m mpremote connect COM8 fs cat :config/settings.json
```
