# Preparing FlexGrid V4 bands for shipping

A checklist for the person who flashes, tests, labels and packs bands that will go to someone else. Follow it top to bottom, once per band. When you finish, each band is in **factory state**: no Wi-Fi saved, no log of anyone's network, a known firmware, and a **sticker** with its setup Wi-Fi name and password. The recipient then gives the band their own Wi-Fi.

**Kit bands ship without the small screen** (its header on the board is left unplugged). That is normal and the firmware copes with it. It means the sticker is mandatory: it is the only place the recipient can read the setup name and password, and the blue light with two quick blinks then a pause is the normal setup pattern, not a fault. `tools/kit_check.py` prints the sticker text.

Each step says what the firmware does, so you can see why it is there. The light patterns (and the screen wording, for a band that has a screen) are in [device-reference.md](device-reference.md); keep it open while you test.

## Two kinds of kit

| Profile | What the recipient does | What you do |
|---|---|---|
| **Factory** (the normal kit) | Joins the band's own setup Wi-Fi and enters their own Wi-Fi name and password | Everything in this document |
| **Preconfigured** (a band that must join one particular router you also supply) | Plugs in and waits | Flash without `--factory` using a `config/settings.json` that holds that router's name and password (copy `config/settings.example.json`). The band then holds a password, so `kit_check` reports "not in factory state" by design (that line is its only expected failure). Keep that password out of every repository. |

The rest of this document is the factory profile.

## What you need

- A computer with Python 3.10 or newer and a USB-C **data** cable per band (a charge-only cable shows no COM port).
- The tools, installed with the same Python that runs the scripts:

  ```
  pip install "esptool==4.8.1" "mpremote==1.25.0" pyserial
  ```

  Those are the versions `flash.py` was tested with. Newer esptool releases are untested here.
- This repository checked out at the **release tag** you will ship (see "Pick the release" below).
- The MicroPython image `ESP32_GENERIC_S3-*.bin` from <https://micropython.org/download/ESP32_GENERIC_S3/> saved in a `firmware/` folder in this repository (see [BRINGUP.md](../BRINGUP.md)).
- A Wi-Fi network for testing that is 2.4 GHz with a password (a phone hotspot is fine), and a phone or laptop to join the band's setup network.
- A label printer or sticker paper, a camera, a pen and paper.
- The bands, charged enough to read at least 3.7 V.

Terms: **bootloader port** is the serial port a blank band shows (USB id `303a:1001`); **runtime port** is the port a band shows while running (`303a:4001`). List them with `python -m mpremote connect list`. On Windows ports are named `COM7`; on macOS `/dev/cu.usbmodem...`; on Linux `/dev/ttyACM0`.

## The checklist

Print this page or copy the boxes. One pass per band; the band's id (`flexgrid-` plus six characters) is your key.

### A. Pick the release (once per batch)

- [ ] Choose the firmware version to ship and check out exactly that git tag: `git checkout <tag>`. `tools/kit_check.py` warns whenever the checkout is not exactly on a tag, because the tag is what you will record in the release notes. If the project has not tagged this version yet, tag it first.
- [ ] Run the desktop tests: `python -m unittest discover -s tests`. They should say `OK`.
- [ ] Put the MicroPython image in `firmware/` (the folder is ignored by git).

### B. Flash the band

- [ ] Plug the band into the computer with a data cable. Note its port.
- [ ] Run the factory flash:

  ```
  python flash.py --factory COM7
  ```

  It erases the whole chip, writes MicroPython, waits for the band to restart, and pushes the firmware. **It pushes no `settings.json` and needs none.** No Wi-Fi name or password is ever required. If the band is already running MicroPython, `flash.py` puts it into download mode itself; there is no button to hold. If you have several bands plugged in, the script works out which port is new by comparing before and after, but plug in one at a time anyway.
- [ ] Expect the last lines to say the board has no Wi-Fi credentials and is in `WIFI SETUP` (a kit band has no screen, so it shows as the blue two-blink light).

> A flash gives the band a **new id and a new setup password**. Do not print a sticker until the very last flash is done (step F).

### C. First look at the band

- [ ] Within a few seconds the light blinks blue, **two quick blinks then a pause**. This is the normal setup pattern for a band without a screen. (If the light blinks blue once a second instead, the band has a screen fitted; it then shows four lines, `WIFI SETUP`, `OM-flexgrid-`, `d7af0b` and `PW kd3mpq72xy`, and you can read the name and password from it.)
- [ ] Read the setup name and password from the tool, since there is no screen to show them. This is read only and changes nothing:

  ```
  python tools/kit_check.py COM8
  ```

  `COM8` is the runtime port. The `Sticker text` block at the end names the setup Wi-Fi (`OM-flexgrid-` plus six characters) and the 10 character setup password. Keep this window open for step D. The line `[warn] OLED: not fitted` is expected (see step E). A `[FAIL]` on battery at this point only means the band needs charging first: the tool still prints the setup name and password under "Setup name and password (read only ...)" (it appears whenever the band holds a valid password, even on FAIL), so you can read them now. That block is not sticker text; print the sticker only from a PASS.

### D. Hardware and reset-path tests (uses your test Wi-Fi)

The buttons only work after the band has joined a network, so do these now. The checks below name the light first; a band that does have a screen also shows the words quoted in [device-reference.md](device-reference.md).

**Keep USB plugged in whenever the band is in setup mode** (the first check below, and the reset path after it). On battery alone the band cannot start its setup network: the light stays at the faint glow and never blinks blue. A band that has already joined runs on battery, so the power-button check can be done with USB unplugged. Plug USB back in before any reset.

- [ ] With the band on USB, join the band's setup network from a phone or laptop (name and password from the `kit_check` output in step C), open `http://192.168.4.1`, enter your **test** Wi-Fi name and password and press Connect.
- [ ] The band restarts. Blue steady for a few seconds, then green for 2 s, then the brief pulse every 3 s in the role colour (white while no role is set). Nothing else is visible without a screen; the pulse is your sign that it joined.
- [ ] If you have the PC app, run `openmuscle web` and press on the sensors with a finger: the heatmap shows the pressure. (This is the only way to see the grid on a band without a screen.)
- [ ] Tap MENU. On a band with a screen the status page appears (`WIFI OK`, address, id, role; tap again for battery, mode, hubs, network; tap again for the grid). Without a screen nothing visible happens, which is expected.
- [ ] **Power button** (the switch at the top edge): with USB unplugged, one short press switches the band off (the light goes dark) and one more short press switches it on again (the faint glow appears at once, then the green join light a few seconds later). Note in your own words how firm the press feels; that is on the confirm list. Then press BOOT once: the band restarts (the light restarts its start-up pattern).
- [ ] Run the button tests below within five minutes of plugging in USB, or keep `openmuscle web` running, or unplug USB. A band on USB with no app listening drops Wi-Fi after five minutes and goes into charge mode (purple light, `CHARGING` on a screen), and a SELECT tap then restarts it instead of pausing.
- [ ] Tap SELECT: light slow cyan breathing (`PAUSED` on a screen). Tap SELECT again: back to the role-colour pulse or steady orange.
- [ ] Plug in USB and hold MENU for 2 s: light purple (`CHARGING / Wi-Fi off / unplug to resume` on a screen). Look at the small CHRG light on the board (D2): it should be lit while the battery charges (see the confirm list in device-reference.md). Tap SELECT: the band restarts and rejoins.
- [ ] If the kit offers SD recording: with a card in, hold SELECT for 2 s and see the light breathe green (`RECORDING` on a screen); tap SELECT to stop. With **no card**, holding SELECT must leave Wi-Fi on and change nothing (`NO SD CARD` on a screen). If the kit ships without a card, check the second case and then confirm the band is still streaming and joined.
- [ ] If you have the PC app: run `openmuscle web` and confirm the band appears. The light changes from the role-colour pulse to steady orange while the app receives.
- [ ] **Reset path** (USB plugged in): hold MENU and SELECT together for 3 s. The light blinks magenta, then the band restarts and shows the setup pattern again (blue, two quick blinks then a pause). Run `python tools/kit_check.py COM8` again and check the setup name and password are **the same as in step C** (a reset keeps them). The port may have changed number after the restart; list ports with `python -m mpremote connect list`.
- [ ] Optional failure test: enter the setup page again with a deliberately wrong test Wi-Fi password. After about 30 s the light blinks amber fast (`WIFI FAILED / check password / back to setup` on a screen), then returns to the blue setup pattern with the same name and password (`LAST TRY FAILED` in the screen's rotating top line). This proves the recipient cannot get stuck after a typo. The band now remembers the network it tried, which the pre-ship check treats as "not in factory state", so afterwards enter the **correct** test details to join once, then use the reset gesture to clear it.
- [ ] Before moving on, with USB plugged in, reset once more (hold MENU and SELECT 3 s) if the band is joined, so it is back in setup (blue, two quick blinks then a pause).

### E. Roles, clean-up and the pre-ship check

Each band wears one role colour in its idle pulse: **left** blue, **right** red, **labeler** green, none white. A kit of two bands normally has one **left** and one **right**. Decide which physical band is which and keep to it.

- [ ] Put the band back in setup (step D end). Run the check, setting the role and a short label, and clearing the logs (they hold the name of your test network):

  ```
  python tools/kit_check.py COM8 --role left --label "Band A" --clear-logs --reset
  ```

  `COM8` is the runtime port. Use `--role right --label "Band B"` for the other band.
- [ ] Read the result. It prints `[ok]` or `[FAIL]` per check and ends with **PASS** or **FAIL**. Exit codes: 0 pass, 1 fail, 2 could not reach the band.
- [ ] On **PASS** it prints the sticker text. Keep it for step F. On a kit band the result includes `[warn] OLED: not fitted`, which is expected and still PASSes; the warning is a reminder that the sticker is the only place the setup password lives.
- [ ] On **FAIL**, fix what it names and run it again:

  | Message | Fix |
  |---|---|
  | `not in factory state: a Wi-Fi name is saved ...` | Run `python wipe.py COM8 --reset`, then run the check again. A wipe gives the band a new id and password. |
  | `log.txt names a Wi-Fi network` or `log.txt.bak exists` | Add `--clear-logs` (logs only) or use `wipe.py`. |
  | `OLED: not fitted` (a warning) | Nothing to fix: kit bands ship without the screen. Print the sticker. |
  | `OLED: not found at 0x3C ... --require-screen was given` | You asked for a band with a screen and it does not answer. Check the connector and solder joints. |
  | `the I2C bus did not answer` | The motion sensor or the I2C wiring is at fault (the bus carries the motion sensor even when no screen is fitted). Check the solder joints. |
  | `battery low: ... charge it before shipping` | Charge the band and run again. The floor is 3.7 V (`--min-battery` changes it). |
  | `no battery detected` | The battery connector is loose or the cell is missing. |
  | `differs from this checkout` | Reflash with `python flash.py --factory`. |
  | `no readable settings file: boot the band once` | Unplug and replug; let it reach setup (blue, two quick blinks then a pause); run again. |
  | `checkout is not on a git tag` (a warning) | Check out the release tag. Add `--require-tag` to make it a failure. |

  A missing screen, a missing SD card, or a missing motion sensor, shows as a warning, not a failure. Add `--require-sd` if the kit includes cards, and `--require-screen` only for a batch that is meant to carry the screen.
- [ ] After `--reset` the band reboots and is in setup again (blue, two quick blinks then a pause). It may come back on a different COM number; run `mpremote connect list` again if `COM8` no longer answers. The log it makes now holds only the band's own boot lines, which still passes.

The check never reads the Wi-Fi password back; it only reports whether one is saved.

### F. Stickers and photos (after the final flash)

The sticker is **mandatory** on a kit band. With no screen, it is the only place the recipient can read the setup Wi-Fi name and password, so a band without its sticker cannot be set up.

- [ ] Print the sticker text from the PASS output. It looks like this (example values):

  ```
  OpenMuscle FlexGrid V4
  Band             flexgrid-d7af0b
  Role             left
  Label            Band A
  Setup Wi-Fi      OM-flexgrid-d7af0b
  Setup password   kd3mpq72xy
  ```

  Add one more line on the sticker: **"Wi-Fi reset: hold MENU + SELECT 3 s"**.
- [ ] Stick it on the band where it survives wear and does not cover the light, the buttons, the power button or the CHRG light.
- [ ] Run `python tools/kit_check.py COM8` once more and **compare the sticker text it prints to the sticker you stuck on**: same network name, same password. If they differ, the band was flashed or wiped after the sticker was made. Reprint.
- [ ] Photograph each band: the sticker (sharp enough to read) and the whole band. Keep the photos with the release notes. Do not photograph faces or anything that identifies a person or place.

### G. Charge level and transport

- [ ] Charge the band, then let it settle for a few minutes and read the voltage from the check output: run `python tools/kit_check.py COM8` with no options (it only reads, and it still passes in factory state) and look at the `[ok] battery: x.xx V` line. The band is in setup mode by now, so the MENU button does nothing and there is no status page to read. A typical shipping level is around 3.8 to 4.0 V. That is a starting suggestion, not a rule: **check the carrier's rules for lithium batteries before you choose a level and a package.** Never ship a flat battery (the check fails below 3.7 V) and never ship a band with a swollen or damaged cell.
- [ ] Power state for transport: switch the band **off** (USB unplugged) with a short press of the power button and check that the light goes dark. A band left switched on will go flat in days. How firm the press must be is still on the confirm list in [device-reference.md](device-reference.md#facts-still-to-confirm-on-a-physical-band); if the light does not go dark, write on the packing slip that the first step is to charge the band.
- [ ] Do not leave USB plugged in inside the box.
- [ ] Re-read the battery after a day if the box will sit before shipping: run `python tools/kit_check.py COM8` again and compare the `battery` line.

### H. Box contents

- [ ] Two bands (one labelled left, one labelled right), stickers on. The sticker must be on every band: there is no screen to fall back on.
- [ ] One USB-C **data** cable per band.
- [ ] The printed **quick card**: the "Quick card" section of [device-reference.md](device-reference.md), one page, double-checked against the band you packed.
- [ ] A "start here" sheet with the link to the app's getting-started guide (the OpenMuscle-Software repository, `docs/getting-started.md`) and a line saying the computer needs Python 3.10 or newer.
- [ ] Whatever fits the band to a forearm (sleeves, straps), with the band-fit guide link.
- [ ] A line on the sheet about the recipient's Wi-Fi: **2.4 GHz, password, no sign-in page, devices may see each other**.
- [ ] Optional: a USB power adapter, and an SD card if the kit offers recording.

### I. Record keeping

- [ ] In the release notes record: the firmware tag, the MicroPython version, each band's id and role, the date, and "kit_check PASS". Nothing else: no names of people or places, and no Wi-Fi names or passwords.
- [ ] Keep the sticker photos with the release notes.

## If something goes wrong

| Symptom | What to do |
|---|---|
| `kit_check` (or `mpremote`) says `could not enter raw repl` on a band that answered earlier | The band has been on USB with no app listening for five minutes and dropped into charge mode, where its USB prompt stops answering. Press BOOT (or unplug and replug USB), wait for the setup light, and run the check again within five minutes. |
| No COM port when plugged in | Try another cable (many are charge-only) and another port. On a band that is running firmware, `flash.py` puts it into download mode by itself. A blank band appears as `303a:1001`. |
| `flash.py` says no firmware found | Put the `.bin` in `firmware/` or set `MICROPYTHON_BIN` to its path. |
| `mpremote` says it could not enter the raw REPL | Wait 10 s and try again. The flash script pushes files over its own connection precisely to avoid this. |
| The light only glows faintly and never blinks blue on a band with no Wi-Fi saved | The band is on battery alone, which cannot start the setup network. Plug in USB; if the blue blink does not start, press BOOT. |
| Band is in setup (blue, two quick blinks then a pause) but your phone cannot see the network | Move closer. The setup network is 2.4 GHz channel 1 and appears only while the band is in setup mode. |
| A band does not answer `flash.py` at all (stuck board) | Hold the SW_RST1 switch (bottom left, wired to the chip's IO0 pin), press and release BOOT, then let go of SW_RST1. The chip starts in download mode (`303a:1001`). Run `flash.py --factory` again. |
| You must make the band look new without reflashing | `python wipe.py COM8 --reset`. This deletes the saved settings (including the Wi-Fi password), `log.txt` and `log.txt.bak`. The band mints a new id and password, so reprint the sticker. |
| Not sure which band is which | Plug in one at a time. The id is on the sticker and in the `kit_check` output (and on the boot screen of a band that has a screen). |

## Notes on what is not checked here

- **Whether the lights are readable and the buttons feel right** is for your eyes and fingers (step D). The kit has no screen to check.
- **Charge time, running time and the power button's press feel** are open items in the confirm list of [device-reference.md](device-reference.md#facts-still-to-confirm-on-a-physical-band).
- **Anything about the sensor band itself** (the flexible sensor sheet and its connector) is outside this checklist; the grid test in step D is a quick check that the sensors respond.
