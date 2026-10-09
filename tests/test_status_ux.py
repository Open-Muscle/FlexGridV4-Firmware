"""Status UX: what the band shows on its OLED and LED in setup mode, while
joining, after joining, when Wi-Fi drops and when the battery runs low.

The LED table in TestLedTable is the contract documented in README.md
("LED guide"); change one and the other has to change with it.
"""
import asyncio
import gc
import os
import tempfile
import unittest
from unittest import mock

import mp_stubs

import commands
import display_manager
import flexgrid
import provisioning
import screens
import status_led
from settings_manager import SettingsManager

ROOT = mp_stubs.ROOT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_state(**settings):
    s = {"device_id": "flexgrid-d7af0b", "device_type": "flexgrid", "wifi_ssid": "HomeNet"}
    s.update(settings)
    subs = mock.MagicMock()
    subs.count.return_value = 0
    return flexgrid._DeviceState(s, subs)


def make_power(vbus=False, full=False):
    p = mock.MagicMock()
    p.is_vbus_present.return_value = vbus
    p.is_battery_full.return_value = full
    return p


def settled(state):
    """A normal steady state: joined a while ago, link up, no flags."""
    state.joining = False
    state.link_up = True
    state.joined_until = None
    return state


def resolve(state, power=None, now=None):
    return flexgrid._resolve_led_state(state, power or make_power(), now)


NOW = 100000


# ---------------------------------------------------------------------------
# LED state table
# ---------------------------------------------------------------------------

class TestLedTable(unittest.TestCase):
    def check(self, expected, state, power=None):
        self.assertEqual(resolve(state, power, NOW), expected)

    def test_booting_and_joining_is_blue_solid(self):
        st = make_state()                       # joining=True, link down
        self.check("joining", st)
        self.assertEqual(status_led.PALETTE_DEFAULT["joining"][3], "solid")

    def test_joined_flash_then_idle_pulse(self):
        st = settled(make_state())
        st.joined_until = NOW + 1500
        self.check("joined", st)
        st.joined_until = NOW - 1
        self.check("idle_unassigned", st)

    def test_failed_first_join_is_amber_blink(self):
        st = make_state()
        st.join_failed = True
        self.check("join_failed", st)

    def test_wifi_lost_after_the_join_attempt_ended(self):
        st = make_state()
        st.joining = False                      # attempt over, link never came up
        self.check("wifi_lost", st)
        st = settled(make_state())
        st.link_up = False                      # router vanished mid-session
        self.check("wifi_lost", st)

    def test_low_battery_only_when_not_on_usb(self):
        st = settled(make_state())
        st.low_battery = True
        self.check("low_battery", st)
        self.check("idle_unassigned", st, make_power(vbus=True))

    def test_steady_states_keep_their_colours(self):
        st = settled(make_state())
        self.check("idle_unassigned", st)
        for role, name in (("left", "idle_left"), ("right", "idle_right"),
                           ("labeler", "idle_labeler")):
            st.role = role
            self.check(name, st)
        st.role = ""
        st.subscribers.count.return_value = 1
        self.check("streaming", st)
        self.check("streaming_on_usb", st, make_power(vbus=True))
        st.streaming = False                    # hub paused the stream
        self.check("idle_unassigned", st)
        st.streaming = True
        st.power_mode = "standby"
        self.check("standby", st)
        st.power_mode = "record"
        self.check("record", st)
        st.power_mode = "charge"
        self.check("charging", st)
        self.check("charge_full", st, make_power(full=True))

    def test_modes_beat_wifi_lost_because_the_radio_is_off(self):
        for mode, name in (("charge", "charging"), ("record", "record")):
            st = settled(make_state())
            st.link_up = False
            st.power_mode = mode
            self.check(name, st)

    def test_priority_order(self):
        st = settled(make_state())
        st.low_battery = True
        st.power_mode = "standby"
        st.link_up = False
        self.check("standby", st)               # a mode the user picked beats the warnings
        st.power_mode = "stream"
        self.check("low_battery", st)           # low battery beats lost Wi-Fi
        st.joined_until = NOW + 500
        self.check("joined", st)                # the 2 s confirmation beats both
        st.join_failed = True
        self.check("join_failed", st)
        st.hold_preview = "wipe"
        self.check("ap_provisioning", st)       # holding the wipe chord previews the wipe
        st.in_fault = True
        self.check("fault", st)                 # a recent watchdog reset beats everything

    def test_button_hold_previews(self):
        st = settled(make_state())
        for hp, name in (("wipe", "ap_provisioning"), ("charge", "charging"), ("record", "record")):
            st.hold_preview = hp
            self.check(name, st)

    def test_in_sleep_is_gone(self):
        st = settled(make_state())
        st.in_sleep = True                      # nothing sets it any more
        self.check("idle_unassigned", st)
        self.assertNotIn("sleep", status_led.PALETTE_DEFAULT)

    def test_every_resolved_name_is_in_the_palette(self):
        names = set()
        for hp in (None, "wipe", "charge", "record"):
            for mode in ("stream", "standby", "charge", "record"):
                for low in (False, True):
                    for link in (False, True):
                        for joining in (False, True):
                            for subs in (0, 1):
                                for vbus in (False, True):
                                    st = make_state()
                                    st.hold_preview = hp
                                    st.power_mode = mode
                                    st.low_battery = low
                                    st.link_up = link
                                    st.joining = joining
                                    st.subscribers.count.return_value = subs
                                    names.add(resolve(st, make_power(vbus=vbus), NOW))
        for n in names:
            self.assertIn(n, status_led.PALETTE_DEFAULT, n)

    def test_five_bring_up_and_warning_states_look_different(self):
        pal = status_led.PALETTE_DEFAULT
        five = [pal[n] for n in ("setup", "joining", "joined", "wifi_lost", "low_battery")]
        self.assertEqual(len(set(five)), 5)
        # and none of them is a state a healthy band sits in
        healthy = {pal[n] for n in ("streaming", "idle_left", "idle_right", "idle_labeler",
                                    "idle_unassigned", "standby", "record", "charging")}
        self.assertFalse(healthy & set(five))

    def test_palette_has_no_duplicate_look(self):
        legacy = {"idle", "error", "calibrating"}
        seen = {}
        for name, entry in status_led.PALETTE_DEFAULT.items():
            if name in legacy:
                continue
            self.assertNotIn(entry, seen, "{} looks the same as {}".format(name, seen.get(entry)))
            seen[entry] = name

    def test_readme_led_guide_lists_every_state(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        legacy = {"boot", "idle", "error", "calibrating"}
        for name in status_led.PALETTE_DEFAULT:
            if name not in legacy:
                self.assertIn("`{}`".format(name), readme, name)

    def test_setup_blink_is_one_hertz_blue(self):
        r, g, b, mode = status_led.PALETTE_DEFAULT["setup"]
        self.assertEqual((r, g > 0, b > 0, mode), (0, False, True, "blink_slow"))

    def test_no_oled_pattern_differs_from_setup(self):
        pal = status_led.PALETTE_DEFAULT
        self.assertNotEqual(pal["setup"][3], pal["setup_no_oled"][3])
        self.assertEqual(pal["setup"][:3], pal["setup_no_oled"][:3])


class TestLedAnimation(unittest.TestCase):
    def led(self):
        self.writes = []
        pins = []

        class Chan:
            def __init__(s, idx):
                s.idx = idx

            def duty_u16(s, v):
                pins.append((s.idx, v))

        chans = []

        def fake_pwm(pin, freq=0, duty_u16=0):
            c = Chan(len(chans))
            chans.append(c)
            return c

        with mock.patch.object(status_led, "PWM", fake_pwm), mock.patch.object(status_led, "Pin", lambda n: n):
            led = status_led.StatusLed()
        self.pins = pins
        return led

    def level(self, led, t):
        self.pins.clear()
        led.animate(t)
        return max(v for _i, v in self.pins) if self.pins else 0

    def test_setup_blinks_once_a_second(self):
        led = self.led()
        led.set_state("setup")
        self.assertGreater(self.level(led, 100), 0)      # on
        self.assertEqual(self.level(led, 600), 0)        # off
        self.assertGreater(self.level(led, 1100), 0)     # on again one second later

    def test_double_blink_has_two_flashes_per_cycle(self):
        led = self.led()
        led.set_state("setup_no_oled")
        samples = [self.level(led, t) > 0 for t in range(0, 1500, 20)]
        edges = sum(1 for a, b in zip(samples, samples[1:]) if b and not a) + (1 if samples[0] else 0)
        self.assertEqual(edges, 2)

    def test_joining_is_steady_not_blinking(self):
        led = self.led()
        self.pins.clear()
        led.set_state("joining")
        self.assertTrue(self.pins and max(v for _i, v in self.pins) > 0)   # lit at once
        self.pins.clear()
        led.animate(300)
        self.assertEqual(self.pins, [])                  # solid: the animator writes nothing


# ---------------------------------------------------------------------------
# Link tracking, status page, tap
# ---------------------------------------------------------------------------

class TestLinkAndPages(unittest.TestCase):
    def test_link_up_starts_green_flash_and_shows_ip_once(self):
        st = make_state()
        now = time_now()
        flexgrid.note_link(st, True, "192.168.1.50", now)
        self.assertTrue(st.link_up)
        self.assertEqual(st.ip, "192.168.1.50")
        self.assertEqual(resolve(st, None, now + 100), "joined")
        self.assertEqual(resolve(st, None, now + flexgrid.JOINED_LED_MS + 50), "idle_unassigned")
        self.assertEqual(st.status_page_now(now + 1000), 0)
        self.assertIsNotNone(st.status_page_now(now + flexgrid.STATUS_HOLD_MS - 500))
        self.assertIsNone(st.status_page_now(now + flexgrid.STATUS_HOLD_MS + 500))

    def test_ip_stays_up_for_fifteen_seconds(self):
        self.assertEqual(flexgrid.STATUS_HOLD_MS, 15000)

    def test_rejoin_flashes_green_but_does_not_take_over_the_screen(self):
        st = make_state()
        t = time_now()
        flexgrid.note_link(st, True, "192.168.1.50", t)
        st.view_page = None                                   # user went back to the heatmap
        flexgrid.note_link(st, False, None, t + 60000)
        self.assertEqual(st.ip, "")
        flexgrid.note_link(st, True, "192.168.1.51", t + 90000)
        self.assertEqual(st.ip, "192.168.1.51")
        self.assertEqual(resolve(st, None, t + 90100), "joined")
        self.assertIsNone(st.status_page_now(t + 90100))

    def test_tap_cycles_heatmap_page0_page1_heatmap(self):
        st = make_state()
        self.assertIsNone(st.status_page_now(time_now()))
        st.tap_status()
        self.assertEqual(st.status_page_now(time_now()), 0)
        st.tap_status()
        self.assertEqual(st.status_page_now(time_now()), 1)
        st.tap_status()
        self.assertIsNone(st.status_page_now(time_now()))

    def test_tall_panel_has_one_page(self):
        st = make_state()
        st.status_page_count = 1
        st.tap_status()
        self.assertEqual(st.status_page_now(time_now()), 0)
        st.tap_status()
        self.assertIsNone(st.status_page_now(time_now()))

    def test_screen_override_priority(self):
        st = settled(make_state())
        st.ip = "192.168.1.50"
        st.role = "left"
        self.assertIsNone(flexgrid._screen_override(st))
        st.tap_status()
        lines = flexgrid._screen_override(st)
        self.assertEqual(lines, ["WIFI OK", "192.168.1.50", "flexgrid-d7af0b", "role: left"])
        st.power_mode = "standby"                              # a tap still beats the banner
        self.assertEqual(flexgrid._screen_override(st)[0], "WIFI OK")
        st.show_notice(["NO SD CARD"])
        self.assertEqual(flexgrid._screen_override(st), ["NO SD CARD"])

    def test_status_page_two_has_battery_mode_hubs_network(self):
        st = settled(make_state())
        flexgrid.device_status.update(vbat=3.95, pct=64)
        st.subscribers.count.return_value = 2
        st.tap_status()
        st.tap_status()
        self.assertEqual(flexgrid._screen_override(st),
                         ["BAT 64% 3.95V", "MODE STREAM", "HUBS 2", "NET HomeNet"])

    def test_wifi_lost_and_off_wording(self):
        st = settled(make_state())
        st.link_up = False
        self.assertEqual(flexgrid._status_info(st)["wifi"], "lost")
        self.assertEqual(screens.status_pages(flexgrid._status_info(st))[0][:2], ["WIFI LOST", "retrying..."])
        st.power_mode = "charge"
        self.assertEqual(screens.status_pages(flexgrid._status_info(st))[0][:2], ["WIFI OFF", "CHARGE mode"])


def time_now():
    import time
    return time.ticks_ms()


# ---------------------------------------------------------------------------
# Screens: every line fits, on both panel heights
# ---------------------------------------------------------------------------

class TestScreens(unittest.TestCase):
    def assert_fits(self, lines, rows, what=""):
        self.assertLessEqual(len(lines), rows, what)
        for ln in lines:
            self.assertLessEqual(len(ln), screens.COLS, "{} {!r}".format(what, ln))

    def test_every_screen_fits_both_panels(self):
        info = {"wifi": "ok", "ip": "192.168.100.200", "device_id": "flexgrid-d7af0b",
                "role": "labeler", "vbat": 3.95, "pct": 64, "low": True, "mode": "standby",
                "subs": 12, "ssid": "A very long router name that is over thirty two"}
        for rows in (4, 8):
            for wifi in ("ok", "lost", "joining", "off"):
                for page in screens.status_pages(dict(info, wifi=wifi), rows):
                    self.assert_fits(page, rows, "status " + wifi)
            for reason in ("wrong_password", "no_ap_found", "timeout", "junk"):
                self.assert_fits(screens.join_failed_lines(reason, "HomeNet", rows), rows, reason)
                for tick in range(8):
                    self.assert_fits(screens.setup_screen_lines(
                        "OM-flexgrid-d7af0b", "kd3mpq72xy", reason, "HomeNet", rows, tick), rows)
            for mode in ("standby", "charge", "record"):
                self.assert_fits(screens.mode_lines(mode, rows), rows, mode)
            self.assert_fits(screens.boot_lines("flexgrid-d7af0b", "HomeNet", rows), rows)
            self.assert_fits(screens.low_battery_lines(3.4, 5, rows), rows)

    def test_setup_credentials_visible_on_every_frame(self):
        for tick in range(12):
            for err in ("", "wrong_password", "no_ap_found"):
                lines = screens.setup_screen_lines("OM-flexgrid-d7af0b", "kd3mpq72xy", err, "HomeNet", 4, tick)
                self.assertEqual(len(lines), 4)
                self.assertEqual(lines[1] + lines[2], "OM-flexgrid-d7af0b")
                self.assertEqual(lines[3], "PW kd3mpq72xy")

    def test_setup_header_rotates_title_guide_and_reason(self):
        frames = {screens.setup_screen_lines("OM-flexgrid-d7af0b", "kd3mpq72xy", "wrong_password",
                                             "HomeNet", 4, t)[0] for t in range(8)}
        self.assertEqual(frames, {"WIFI SETUP", "see guide", "LAST TRY FAILED", "check password"})

    def test_setup_on_tall_panel_is_static_and_complete(self):
        a = screens.setup_screen_lines("OM-flexgrid-d7af0b", "kd3mpq72xy", "", "", 8, 0)
        self.assertEqual(a, ["WIFI SETUP", "OM-flexgrid-", "d7af0b", "PW kd3mpq72xy", "see guide"])
        self.assertEqual(a, screens.setup_screen_lines("OM-flexgrid-d7af0b", "kd3mpq72xy", "", "", 8, 5))

    def test_psk_always_fits_with_prefix(self):
        worst = "".join(provisioning.PSK_CHARSET[0] for _ in range(provisioning.PSK_LENGTH))
        self.assertLessEqual(len("PW " + worst), screens.COLS)

    def test_joined_screen_is_wifi_ok_ip_id_role(self):
        pages = screens.status_pages({"wifi": "ok", "ip": "10.0.0.7", "device_id": "flexgrid-d7af0b",
                                      "role": "", "mode": "stream", "ssid": "x"}, 4)
        self.assertEqual(pages[0], ["WIFI OK", "10.0.0.7", "flexgrid-d7af0b", "no role set"])

    def test_join_failed_screen_text(self):
        self.assertEqual(screens.join_failed_lines("wrong_password", "HomeNet", 4),
                         ["WIFI FAILED", "check password", "back to setup"])
        self.assertEqual(screens.join_failed_lines("no_ap_found", "HomeNet", 4),
                         ["WIFI FAILED", "network not", "found or 5 GHz", "back to setup"])

    def test_mode_banners_explain_the_way_out(self):
        self.assertEqual(screens.mode_lines("standby")[0], "PAUSED")
        charge = screens.mode_lines("charge")
        self.assertEqual(charge[:3], ["CHARGING", "Wi-Fi off", "unplug to resume"])
        self.assertIsNone(screens.mode_lines("stream"))

    def test_boot_screen_has_four_lines(self):
        self.assertEqual(screens.boot_lines("flexgrid-d7af0b", "HomeNet"),
                         ["OpenMuscle V4", "flexgrid-d7af0b", "Joining Wi-Fi...", "HomeNet"])


# ---------------------------------------------------------------------------
# Low battery
# ---------------------------------------------------------------------------

class TestLowBattery(unittest.TestCase):
    def test_threshold_and_hysteresis(self):
        f = flexgrid.update_low_battery
        self.assertFalse(f(False, 3.80, False))
        self.assertTrue(f(False, 3.50, False))
        self.assertTrue(f(True, 3.60, False))         # still inside the 0.1 V band
        self.assertFalse(f(True, 3.70, False))
        self.assertFalse(f(False, 3.30, True))        # USB present: charging, not low
        self.assertFalse(f(False, 0.1, False))        # nothing on the ADC is not an empty cell
        self.assertFalse(f(False, None, False))

    def test_notice_when_it_trips_and_every_five_minutes_after(self):
        st = make_state()
        t = time_now()
        st.update_battery(3.40, 0, False, t)
        self.assertTrue(st.low_battery)
        self.assertEqual(st.notice[0], ["LOW BATTERY", "BAT 0% 3.40V", "charge soon"])
        st.notice = None
        st.update_battery(3.40, 0, False, t + 5000)
        self.assertIsNone(st.notice)                   # not repeated straight away
        st.update_battery(3.40, 0, False, t + flexgrid.LOW_BATTERY_NOTICE_EVERY_MS + 1)
        self.assertIsNotNone(st.notice)
        st.update_battery(3.40, 0, True, t + 400000)    # plugged in
        self.assertFalse(st.low_battery)

    def test_status_page_says_low(self):
        self.assertEqual(screens.battery_line(3.42, 3, True), "LOW 3% 3.42V")
        self.assertEqual(screens.battery_line(3.95, 64), "BAT 64% 3.95V")
        self.assertEqual(screens.battery_line(None, None), "BAT --")

    def test_threshold_from_settings(self):
        self.assertEqual(make_state(low_battery_v=3.7).low_battery_v, 3.7)
        self.assertEqual(make_state(low_battery_v="junk").low_battery_v, flexgrid.LOW_BATTERY_V_DEFAULT)
        self.assertEqual(SettingsManager.DEFAULTS["low_battery_v"], flexgrid.LOW_BATTERY_V_DEFAULT)


# ---------------------------------------------------------------------------
# Display geometry
# ---------------------------------------------------------------------------

class TestDisplayGeometry(unittest.TestCase):
    def build(self, devices=(0x3C,), **kw):
        i2c = mock.MagicMock()
        i2c.scan.return_value = list(devices)
        oled_cls = mock.MagicMock()
        with mock.patch.object(display_manager, "I2C", return_value=i2c), \
                mock.patch.object(display_manager.ssd1306, "SSD1306_I2C", oled_cls):
            dm = display_manager.DisplayManager(**kw)
        return dm, oled_cls

    def test_default_is_128x32(self):
        dm, oled_cls = self.build()
        self.assertEqual((dm.width, dm.height, dm.rows), (128, 32, 4))
        self.assertEqual(oled_cls.call_args[0][:2], (128, 32))
        self.assertTrue(dm.present)

    def test_height_64_gives_eight_rows(self):
        dm, oled_cls = self.build(height=64)
        self.assertEqual((dm.height, dm.rows), (64, 8))
        self.assertEqual(oled_cls.call_args[0][:2], (128, 64))

    def test_text_screen_stops_at_the_panel_height(self):
        dm, _ = self.build()
        dm.text_screen(["a", "b", "c", "d", "e", "f"])
        self.assertEqual(dm.oled.text.call_count, 4)
        dm64, _ = self.build(height=64)
        dm64.text_screen(["a"] * 10)
        self.assertEqual(dm64.oled.text.call_count, 8)

    def test_no_oled_is_reported(self):
        dm, _ = self.build(devices=())
        self.assertFalse(dm.present)
        dm.text_screen(["x"])          # must not raise

    def test_heatmap_fills_the_32_px_panel(self):
        dm, _ = self.build()
        matrix = [[3500] * 4 for _ in range(15)]
        dm.draw_sensor_matrix(matrix)
        ys = [c[0][1] for c in dm.oled.fill_rect.call_args_list]
        self.assertEqual(min(ys), 0)
        self.assertEqual(max(ys) + 7, 31)       # last cell ends on the bottom row

    def test_height_setting(self):
        h = flexgrid.display_height_from
        self.assertEqual(h({}), 32)
        self.assertEqual(h({"display_height": 64}), 64)
        self.assertEqual(h({"display_height": "64"}), 64)
        self.assertEqual(h({"display_height": 48}), 32)
        self.assertEqual(h({"display_height": "x"}), 32)
        self.assertEqual(SettingsManager.DEFAULTS["display_height"], 32)

    def test_docs_agree_on_the_size(self):
        for name in ("README.md", os.path.join("lib", "pinmap.py")):
            with open(os.path.join(ROOT, name), encoding="utf-8") as f:
                text = f.read()
            self.assertIn("128x32", text, name)
            self.assertNotIn("128x64 OLED", text, name)


# ---------------------------------------------------------------------------
# Setup mode boot branch (factory-state routing)
# ---------------------------------------------------------------------------

class FakeDisplay:
    def __init__(self, present=True, rows=4):
        self.present = present
        self.rows = rows
        self.shown = []

    def text_screen(self, lines):
        self.shown.append(list(lines))


class FakeLed:
    def __init__(self):
        self.states = []
        self.animated = 0

    def set_state(self, name):
        self.states.append(name)

    def animate(self, t):
        self.animated += 1

    def set_rgb(self, *a):
        pass


class TestSetupBranch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, cwd)

    def run_main(self, display, settings_extra=None):
        led = FakeLed()
        served = []

        async def fake_serve(state, bind_ip=provisioning.AP_IP):
            served.append(state.state)
            await asyncio.sleep(0.12)          # let the setup loops run a few ticks

        def boom(*a, **k):
            raise AssertionError("normal-mode subsystem built in setup mode")

        patches = [
            mock.patch.object(flexgrid, "DisplayManager", lambda **kw: display),
            mock.patch.object(flexgrid, "StatusLed", lambda: led),
            mock.patch.object(flexgrid.provisioning, "start_ap", lambda s, t, i: "kd3mpq72xy"),
            mock.patch.object(flexgrid.provisioning, "serve", fake_serve),
            mock.patch.object(flexgrid, "NetworkManager", boom),
            mock.patch.object(flexgrid, "SensorMatrix", boom),
            mock.patch.object(flexgrid, "PowerManager", boom),
            mock.patch.object(flexgrid.logger, "init_persistent", lambda: None),
            mock.patch.object(flexgrid.screens, "SETUP_FRAME_S", 0.02),
        ]
        if settings_extra:
            os.makedirs("config", exist_ok=True)
            s = SettingsManager.load()
            s.update(settings_extra)
            SettingsManager.save(s)
        for p in patches:
            p.start()
        try:
            asyncio.run(flexgrid.main())
        finally:
            for p in reversed(patches):
                p.stop()
        return led, served

    def test_factory_state_goes_to_setup_with_blue_blink(self):
        display = FakeDisplay()
        led, served = self.run_main(display)
        self.assertEqual(served, ["unprovisioned"])
        self.assertEqual(led.states, ["setup"])
        self.assertGreater(led.animated, 2)               # the animator really runs in AP mode
        first = display.shown[0]
        self.assertEqual(first[1] + first[2], provisioning.ssid_for("flexgrid", SettingsManager.load()["device_id"]))
        self.assertEqual(first[3], "PW kd3mpq72xy")
        headers = {f[0] for f in display.shown}
        self.assertIn("WIFI SETUP", headers)
        self.assertIn("see guide", headers)

    def test_no_oled_uses_the_distinct_led_pattern(self):
        led, _ = self.run_main(FakeDisplay(present=False))
        self.assertEqual(led.states, ["setup_no_oled"])

    def test_wiped_band_with_failure_reason_shows_it(self):
        display = FakeDisplay()
        self.run_main(display, {"last_wifi_error": "wrong_password", "last_ssid": "HomeNet"})
        headers = {f[0] for f in display.shown}
        self.assertIn("LAST TRY FAILED", headers)
        self.assertIn("check password", headers)

    def test_info_in_setup_mode_reports_oled_and_role(self):
        st = provisioning._ProvisioningState({"device_id": "flexgrid-abc123", "device_type": "flexgrid",
                                              "role": "left"}, SettingsManager, {"oled": True})
        info = st.info_payload()
        self.assertEqual(info["role"], "left")
        self.assertTrue(info["oled"])
        self.assertEqual(provisioning._ProvisioningState(
            {"device_id": "x", "device_type": "flexgrid"}, SettingsManager, {}).info_payload()["role"], "")


class TestPskRules(unittest.TestCase):
    def test_charset_and_length(self):
        for _ in range(200):
            psk = provisioning.mint_psk()
            self.assertEqual(len(psk), provisioning.PSK_LENGTH)
            self.assertEqual(provisioning.PSK_LENGTH, 10)
            self.assertTrue(set(psk) <= set(provisioning.PSK_CHARSET))
        for ch in "01lo":
            self.assertNotIn(ch, provisioning.PSK_CHARSET)


# ---------------------------------------------------------------------------
# ip + role reach the hub
# ---------------------------------------------------------------------------

class TestIpAndRole(unittest.TestCase):
    def call_get_info(self, state):
        handlers = commands.build_handlers(state)
        return asyncio.run(handlers["get_info"]({}, ("10.0.0.2", 1234)))

    def test_get_info_carries_ip_and_role(self):
        st = settled(make_state())
        st.subscribers.snapshot.return_value = []
        st.ip = "192.168.1.50"
        st.role = "right"
        reply = self.call_get_info(st)
        self.assertEqual(reply["ip"], "192.168.1.50")
        self.assertEqual(reply["role"], "right")

    def test_get_info_omits_ip_before_joining(self):
        st = make_state()
        st.subscribers.snapshot.return_value = []
        self.assertNotIn("ip", self.call_get_info(st))

    def test_status_frame_has_ip_and_role_keys(self):
        self.assertIn("ip", flexgrid.device_status)
        self.assertIn("role", flexgrid.device_status)

    def test_status_loop_publishes_ip_role_and_low_battery(self):
        st = settled(make_state())
        st.role = "left"
        st.subscribers.count.return_value = 0
        power = mock.MagicMock()
        power.battery_voltage.return_value = 3.4
        power.battery_percent.return_value = 0
        power.is_vbus_present.return_value = False
        network = mock.MagicMock()
        network.is_connected.return_value = True
        network.local_ip.return_value = "192.168.1.50"
        network.rssi.return_value = -50
        imu = mock.MagicMock()
        imu.present = False

        async def go():
            task = asyncio.create_task(flexgrid.status_loop(st, power, network, imu, interval_s=0.01))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        with mock.patch.object(gc, "mem_free", lambda: 1000, create=True):
            asyncio.run(go())
        self.assertEqual(flexgrid.device_status["ip"], "192.168.1.50")
        self.assertEqual(flexgrid.device_status["role"], "left")
        self.assertTrue(st.low_battery)


# ---------------------------------------------------------------------------
# MENU tap / hold / chord
# ---------------------------------------------------------------------------

class ScriptedPin:
    def __init__(self, levels=()):
        self.levels = list(levels)

    def value(self):
        return self.levels.pop(0) if self.levels else 1


class TestMenuButton(unittest.TestCase):
    def run_buttons(self, menu, select=(), timeout=3.0):
        """Run power_mode_loop until both button scripts are used up (one
        value per 5 ms tick), then a little longer for the last action."""
        st = settled(make_state())
        power = make_power()
        network = mock.MagicMock()
        menu_pin, select_pin = ScriptedPin(menu), ScriptedPin(select)

        async def go():
            task = asyncio.create_task(flexgrid.power_mode_loop(
                st, power, select_pin, {"auto_charge_idle_s": 0},
                menu_pin=menu_pin, network=network, sd=None,
                tick_ms=5, charge_hold_s=0.03))
            end = asyncio.get_running_loop().time() + timeout
            while (menu_pin.levels or select_pin.levels) and asyncio.get_running_loop().time() < end:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.06)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        flexgrid.machine.reset.reset_mock()
        asyncio.run(go())
        return st, network

    def test_tap_shows_status_page_and_does_not_charge(self):
        st, network = self.run_buttons([0, 0])
        self.assertEqual(st.power_mode, "stream")
        self.assertEqual(st.status_page_now(time_now()), 0)
        network.sta.active.assert_not_called()

    def test_two_taps_reach_the_device_page(self):
        st, _ = self.run_buttons([0, 0] + [1] * 70 + [0, 0])
        self.assertEqual(st.status_page_now(time_now()), 1)

    def test_hold_still_toggles_charge_and_is_not_also_a_tap(self):
        st, network = self.run_buttons([0] * 14)
        self.assertEqual(st.power_mode, "charge")
        self.assertEqual(network.sta.active.call_args_list, [mock.call(False)])
        self.assertIsNone(st.status_page_now(time_now()))

    def test_aborted_chord_is_not_a_tap_or_a_hold(self):
        # both buttons for 8 ticks (under the 3 s chord time), SELECT let go
        # first, MENU kept down 4 more ticks, then released
        menu = [0] * 12
        select = [0] * 8
        st, network = self.run_buttons(menu, select)
        self.assertIsNone(st.status_page_now(time_now()))
        self.assertEqual(st.power_mode, "stream")
        flexgrid.machine.reset.assert_not_called()


# ---------------------------------------------------------------------------
# Join-failure path through the running LED animator
# ---------------------------------------------------------------------------

class TestJoinFailedUsesAnimator(unittest.TestCase):
    def test_join_failed_flag_set_when_state_given(self):
        st = make_state()
        display = FakeDisplay()
        led = FakeLed()
        settings = {"wifi_ssid": "HomeNet", "wifi_password": "pw", "wifi_joined_ok": False}
        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                flexgrid.machine.reset.reset_mock()
                asyncio.run(flexgrid._fall_back_to_setup(settings, display, led, "wrong_password",
                                                         hold_s=0, state=st))
            finally:
                os.chdir(cwd)
        self.assertTrue(st.join_failed)
        self.assertEqual(resolve(st, None, NOW), "join_failed")
        self.assertEqual(display.shown[-1], ["WIFI FAILED", "check password", "back to setup"])
        flexgrid.machine.reset.assert_called_once()


if __name__ == "__main__":
    unittest.main()
