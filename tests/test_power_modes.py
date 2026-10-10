"""RECORD / CHARGE / STANDBY must not cost a user their Wi-Fi by accident, and
every non-streaming mode must say what it is on the OLED."""
import asyncio
import unittest
from unittest import mock

import mp_stubs

import flexgrid
import screens
from sd_logger import SDLogger


class ScriptedPin:
    """Active-low button. Each value() call consumes one scripted level, then
    the pin reads released (1). One call per power_mode_loop tick, so the
    scripts below are exact tick counts and independent of the clock."""

    def __init__(self, levels=()):
        self.levels = list(levels)

    def value(self):
        return self.levels.pop(0) if self.levels else 1


HOLD = [0] * 14     # well over the 6-tick hold threshold used below
TAP = [0] * 2


class FakeSD:
    def __init__(self, card=True):
        self.card = card
        self.enabled = False
        self.mount_calls = 0
        self.recording = False

    def force_enable(self):
        self.enabled = True

    def mount(self):
        self.mount_calls += 1
        return self.card

    def start_session(self):
        self.recording = self.card
        return "session-1" if self.card else None

    def end_session(self):
        self.recording = False

    def is_recording(self):
        return self.recording


class Env:
    def __init__(self, sd, **settings):
        self.settings = {"device_id": "flexgrid-abc123", "device_type": "flexgrid"}
        self.settings.update(settings)
        subs = mock.MagicMock()
        subs.count.return_value = 0
        self.state = flexgrid._DeviceState(self.settings, subs)
        self.power = mock.MagicMock()
        self.power.is_vbus_present.return_value = False
        self.power.is_battery_full.return_value = False
        self.network = mock.MagicMock()
        self.sd = sd
        self.select = ScriptedPin()
        self.menu = ScriptedPin()

    @property
    def wifi_off_calls(self):
        return self.network.sta.active.call_args_list

    def screen(self):
        return flexgrid._screen_override(self.state)


def run_loop(env, until, timeout=4.0, **loop_kw):
    """Run power_mode_loop until `until()` is true (or timeout), then stop it."""
    async def go():
        kw = dict(menu_pin=env.menu, network=env.network, sd=env.sd,
                  tick_ms=5, charge_hold_s=0.03)
        kw.update(loop_kw)
        task = asyncio.create_task(flexgrid.power_mode_loop(
            env.state, env.power, env.select, env.settings, **kw))
        end = asyncio.get_running_loop().time() + timeout
        try:
            while not until() and asyncio.get_running_loop().time() < end:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)     # let any trailing action land
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    asyncio.run(go())


class RecordGuardTests(unittest.TestCase):
    def setUp(self):
        flexgrid.machine.reset.reset_mock()

    def hold_select(self, env, **kw):
        env.select.levels = list(HOLD)
        # done once the script has been consumed
        run_loop(env, lambda: not env.select.levels, **kw)

    def test_no_sd_card_keeps_wifi_up_and_streaming(self):
        env = Env(FakeSD(card=False))
        self.hold_select(env)
        self.assertEqual(env.state.power_mode, "stream")
        self.assertTrue(env.state.streaming)
        self.assertEqual(env.wifi_off_calls, [])
        self.assertEqual(env.screen(), ["NO SD CARD", "Record needs one", "Wi-Fi stays on"])
        self.assertFalse(env.sd.enabled)          # failed attempt does not leave logging armed
        flexgrid.machine.reset.assert_not_called()

    def test_real_sdlogger_without_a_card_is_refused(self):
        # On a desktop os.mount does not exist, which is the same outcome as
        # no card on the board: mount() returns False.
        env = Env(None, sd_logging=False)
        env.sd = SDLogger(env.settings)
        self.hold_select(env)
        self.assertEqual(env.state.power_mode, "stream")
        self.assertEqual(env.wifi_off_calls, [])
        self.assertFalse(env.sd.enabled)

    def test_missing_sd_logger_is_refused(self):
        env = Env(None)
        self.hold_select(env)
        self.assertEqual(env.state.power_mode, "stream")
        self.assertEqual(env.wifi_off_calls, [])

    def test_card_present_enters_record_and_turns_wifi_off(self):
        env = Env(FakeSD(card=True))
        self.hold_select(env)
        self.assertEqual(env.state.power_mode, "record")
        self.assertFalse(env.state.streaming)
        self.assertEqual(env.wifi_off_calls, [mock.call(False)])
        self.assertTrue(env.sd.is_recording())
        self.assertEqual(env.screen()[0], "RECORDING")

    def test_record_enabled_false_disables_the_hold(self):
        env = Env(FakeSD(card=True), record_enabled=False)
        self.hold_select(env)
        self.assertEqual(env.state.power_mode, "stream")
        self.assertEqual(env.sd.mount_calls, 0)
        self.assertEqual(env.wifi_off_calls, [])
        self.assertEqual(env.screen()[0], "RECORD OFF")
        self.assertNotEqual(env.state.hold_preview, "record")

    def test_network_record_request_is_guarded_too(self):
        env = Env(FakeSD(card=False))
        env.state.requested_power_mode = "record"
        run_loop(env, lambda: env.state.requested_power_mode is None)
        self.assertEqual(env.state.power_mode, "stream")
        self.assertEqual(env.wifi_off_calls, [])


class ModeBannerTests(unittest.TestCase):
    def test_standby_tap_shows_paused_and_tap_again_resumes(self):
        env = Env(FakeSD())
        env.select.levels = list(TAP)
        run_loop(env, lambda: env.state.power_mode == "standby")
        self.assertEqual(env.state.power_mode, "standby")
        self.assertEqual(env.wifi_off_calls, [])           # standby keeps Wi-Fi on
        self.assertEqual(env.screen()[0], "PAUSED")
        self.assertIn("Wi-Fi is on", env.screen())

    def test_menu_hold_enters_charge_and_says_wifi_is_off(self):
        env = Env(FakeSD())
        env.menu.levels = list(HOLD)
        run_loop(env, lambda: env.state.power_mode == "charge")
        self.assertEqual(env.state.power_mode, "charge")
        self.assertEqual(env.wifi_off_calls, [mock.call(False)])
        lines = env.screen()
        self.assertEqual(lines[0], "CHARGING")
        self.assertIn("Wi-Fi off", lines)
        self.assertIn("unplug to resume", lines)

    def test_stream_shows_heatmap(self):
        env = Env(FakeSD())
        self.assertIsNone(env.screen())

    def test_notice_expires(self):
        env = Env(FakeSD())
        env.state.show_notice(["HELLO"], hold_ms=-10)
        self.assertIsNone(env.screen())
        self.assertIsNone(env.state.notice)
        env.state.show_notice(["HELLO"], hold_ms=60000)
        self.assertEqual(env.screen(), ["HELLO"])

    def test_notice_beats_mode_banner(self):
        env = Env(FakeSD())
        env.state.power_mode = "standby"
        env.state.show_notice(["NO SD CARD"])
        self.assertEqual(env.screen(), ["NO SD CARD"])


class AutoChargeTests(unittest.TestCase):
    """The 30 s auto-CHARGE used to cut Wi-Fi on a band that was merely
    plugged in for power before the hub had found it."""

    def run_idle_on_usb(self, idle_start, **settings):
        env = Env(FakeSD(), **settings)
        env.power.is_vbus_present.return_value = True
        env.state.idle_seconds = idle_start
        # The idle counter and the auto-charge check run on the loop's 1 Hz
        # branch. Stop after a few counted seconds rather than on wall time:
        # how many branches fit in a wall-clock window depends on the Python
        # version's asyncio timer pacing (3.12 spins far faster than 3.13).
        stop_at = idle_start + 3
        run_loop(env, lambda: env.state.power_mode == "charge"
                 or env.state.idle_seconds >= stop_at, timeout=6.0, tick_ms=10)
        return env

    def test_default_threshold_is_five_minutes(self):
        self.assertEqual(SettingsManager_defaults()["auto_charge_idle_s"], 300)
        env = self.run_idle_on_usb(100)              # >30 s idle used to trigger
        self.assertEqual(env.state.power_mode, "stream")
        self.assertEqual(env.wifi_off_calls, [])

    def test_charges_after_the_configured_idle_time(self):
        env = self.run_idle_on_usb(299, auto_charge_idle_s=300)
        self.assertEqual(env.state.power_mode, "charge")

    def test_zero_disables_auto_charge(self):
        env = self.run_idle_on_usb(10 ** 6, auto_charge_idle_s=0)
        self.assertEqual(env.state.power_mode, "stream")
        self.assertEqual(env.wifi_off_calls, [])


def SettingsManager_defaults():
    from settings_manager import SettingsManager
    return SettingsManager.DEFAULTS


class DisplayLoopTests(unittest.TestCase):
    def real_state(self):
        subs = mock.MagicMock()
        subs.count.return_value = 0
        return flexgrid._DeviceState({"device_id": "flexgrid-abc123", "device_type": "flexgrid"}, subs)

    def run_display(self, state, seconds=0.45):
        display = mock.MagicMock()
        sm = mock.MagicMock()

        async def go():
            task = asyncio.create_task(flexgrid.display_loop(display, sm, interval_ms=20, state=state))
            await asyncio.sleep(seconds)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        asyncio.run(go())
        return display

    def test_banner_is_drawn_once_not_every_frame(self):
        state = self.real_state()
        state.power_mode = "charge"
        display = self.run_display(state)
        self.assertEqual(display.text_screen.call_count, 1)
        display.draw_sensor_matrix.assert_not_called()
        self.assertEqual(display.text_screen.call_args[0][0], screens.mode_lines("charge"))

    def test_stream_draws_the_heatmap(self):
        state = self.real_state()
        display = self.run_display(state)
        self.assertGreater(display.draw_sensor_matrix.call_count, 3)
        display.text_screen.assert_not_called()

    def test_no_state_keeps_old_behaviour(self):
        display = self.run_display(None)
        self.assertGreater(display.draw_sensor_matrix.call_count, 3)


if __name__ == "__main__":
    unittest.main()
