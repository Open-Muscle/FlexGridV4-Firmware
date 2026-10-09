"""First-join fallback: a wrong password / 5 GHz-only network must send the
band back to setup (AP) mode, while a band that has joined once never drops
its credentials (PROVISIONING.md section 2)."""
import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

import mp_stubs

import flexgrid
import network
import network_manager as nm
import provisioning
import screens
from settings_manager import SettingsManager


async def _fast_sleep(_s):
    await _REAL_SLEEP(0)

_REAL_SLEEP = asyncio.sleep


class FakeSta:
    """network.WLAN(STA_IF) stand-in. `statuses` is consumed one per
    status() call (the last value repeats); the link comes up after
    `up_after` isconnected() polls, or never if None."""

    def __init__(self, statuses=(1001,), up_after=None):
        self.statuses = list(statuses)
        self.up_after = up_after
        self.polls = 0
        self.connect_calls = []
        self._active = False

    def active(self, v=None):
        if v is None:
            return self._active
        self._active = v

    def isconnected(self):
        self.polls += 1
        return self.up_after is not None and self.polls > self.up_after

    def connect(self, ssid, pw):
        self.connect_calls.append((ssid, pw))

    def status(self, *a):
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def ifconfig(self):
        return ("192.168.1.50", "255.255.255.0", "192.168.1.1", "192.168.1.1")


def make_nm(sta, **settings):
    s = {"device_id": "flexgrid-abc123", "device_type": "flexgrid",
         "wifi_ssid": "HomeNet", "wifi_password": "correct-horse-1"}
    s.update(settings)
    mgr = nm.NetworkManager(s, mock.MagicMock())
    mgr.sta = sta
    return mgr, s


def run(coro):
    with mock.patch("asyncio.sleep", _fast_sleep):
        return asyncio.new_event_loop().run_until_complete(coro)


class ClassifyTests(unittest.TestCase):
    def test_status_codes(self):
        self.assertEqual(nm.classify_join_status(network.STAT_WRONG_PASSWORD), "wrong_password")
        self.assertEqual(nm.classify_join_status(network.STAT_HANDSHAKE_TIMEOUT), "wrong_password")
        self.assertEqual(nm.classify_join_status(network.STAT_NO_AP_FOUND), "no_ap_found")
        for benign in (network.STAT_IDLE, network.STAT_CONNECTING, network.STAT_GOT_IP, None, 7):
            self.assertIsNone(nm.classify_join_status(benign))


class ConnectTests(unittest.TestCase):
    def test_wrong_password_reported(self):
        mgr, _ = make_nm(FakeSta([1001, 1001, 202, 1001]))
        self.assertFalse(run(mgr.connect()))
        # The final sample is "connecting" again, but the failure seen earlier sticks.
        self.assertEqual(mgr.last_error, "wrong_password")

    def test_no_ap_found_reported(self):
        mgr, _ = make_nm(FakeSta([201]))
        self.assertFalse(run(mgr.connect()))
        self.assertEqual(mgr.last_error, "no_ap_found")

    def test_plain_timeout(self):
        mgr, _ = make_nm(FakeSta([1001]))
        self.assertFalse(run(mgr.connect()))
        self.assertEqual(mgr.last_error, "timeout")

    def test_success_clears_error(self):
        mgr, _ = make_nm(FakeSta([1001], up_after=3))
        mgr.last_error = "timeout"
        self.assertTrue(run(mgr.connect()))
        self.assertEqual(mgr.last_error, "")

    def test_join_timeout_setting_and_clamp(self):
        for setting, expect in ((20, 20), (7, 7), (1, nm.MIN_JOIN_TIMEOUT_S),
                                (999, nm.MAX_JOIN_TIMEOUT_S), ("junk", 20)):
            sta = FakeSta([1001])
            mgr, _ = make_nm(sta, sta_join_timeout_s=setting)
            run(mgr.connect())
            # one isconnected() before connect(), then one per second waited,
            # then the final check
            self.assertEqual(sta.polls, 1 + expect + 1, setting)

    def test_no_ssid_does_not_connect(self):
        sta = FakeSta()
        mgr, _ = make_nm(sta, wifi_ssid="")
        self.assertFalse(run(mgr.connect()))
        self.assertEqual(sta.connect_calls, [])
        self.assertEqual(mgr.last_error, "")

    def test_credentials_passed_unstripped(self):
        sta = FakeSta([1001], up_after=1)
        mgr, _ = make_nm(sta, wifi_ssid=" Cafe ", wifi_password="pass word ")
        run(mgr.connect())
        self.assertEqual(sta.connect_calls, [(" Cafe ", "pass word ")])


class RecordTests(unittest.TestCase):
    def test_failure_clears_creds_keeps_ssid(self):
        s = {"wifi_ssid": "HomeNet", "wifi_password": "pw", "wifi_joined_ok": False,
             "provisioning_psk": "abcdefghij"}
        nm.record_join_failure(s, "wrong_password")
        self.assertEqual(s["wifi_ssid"], "")
        self.assertEqual(s["wifi_password"], "")
        self.assertEqual(s["last_ssid"], "HomeNet")
        self.assertEqual(s["last_wifi_error"], "wrong_password")
        self.assertFalse(s["wifi_joined_ok"])
        self.assertEqual(s["provisioning_psk"], "abcdefghij")   # same PSK in AP mode

    def test_success_marks_joined_once(self):
        s = {"wifi_joined_ok": False, "last_wifi_error": "timeout", "last_ssid": "Old"}
        self.assertTrue(nm.record_join_success(s))
        self.assertEqual((s["wifi_joined_ok"], s["last_wifi_error"], s["last_ssid"]),
                         (True, "", ""))
        self.assertFalse(nm.record_join_success(s))   # nothing to write the second time


class FallbackFlowTests(unittest.TestCase):
    """Drives flexgrid._handle_join_result with the real SettingsManager
    writing to a scratch directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, self.cwd)
        flexgrid.machine.reset.reset_mock()
        self.display = mock.MagicMock()
        self.display.rows = 4
        self.led = mock.MagicMock()

    def provisioned_settings(self, **extra):
        """What POST /provision leaves on flash."""
        s = SettingsManager.load()
        s.update({"wifi_ssid": "HomeNet", "wifi_password": "wrong-pass-1",
                  "provisioning_psk": "kd3mpq72xy", "wifi_joined_ok": False})
        s.update(extra)
        SettingsManager.save(s)
        return s

    def saved(self):
        with open(os.path.join("config", "settings.json")) as f:
            return json.load(f)

    def test_wrong_password_returns_to_setup(self):
        settings = self.provisioned_settings()
        mgr, _ = make_nm(FakeSta([1001, 202]), wifi_ssid="HomeNet", wifi_password="wrong-pass-1")
        joined = run(mgr.connect())
        self.assertFalse(joined)
        rebooting = run(flexgrid._handle_join_result(joined, mgr, settings, self.display, self.led))
        self.assertTrue(rebooting)
        flexgrid.machine.reset.assert_called_once()
        on_disk = self.saved()
        self.assertEqual(on_disk["wifi_ssid"], "")
        self.assertEqual(on_disk["wifi_password"], "")
        self.assertEqual(on_disk["last_ssid"], "HomeNet")
        self.assertEqual(on_disk["last_wifi_error"], "wrong_password")
        self.assertEqual(on_disk["provisioning_psk"], "kd3mpq72xy")   # same PSK
        self.assertFalse(on_disk["wifi_joined_ok"])
        # reason was put on the OLED before the reset
        shown = self.display.text_screen.call_args[0][0]
        self.assertEqual(shown, ["WIFI FAILED", "check password", "back to setup"])

        # Next boot: empty wifi_ssid -> AP branch. Its screen and /info say why.
        again = SettingsManager.load()
        self.assertEqual(again["wifi_ssid"], "")
        lines = screens.setup_screen_lines("OM-flexgrid-abc123", again["provisioning_psk"],
                                           again["last_wifi_error"], again["last_ssid"])
        self.assertIn("PW kd3mpq72xy", lines)
        # a 4-line panel rotates its top row through the failure reason
        frames = [screens.setup_screen_lines("OM-flexgrid-abc123", again["provisioning_psk"],
                                             again["last_wifi_error"], again["last_ssid"], tick=t)[0]
                  for t in range(6)]
        self.assertIn("LAST TRY FAILED", frames)
        self.assertIn("check password", frames)
        # a taller panel shows everything at once
        tall = screens.setup_screen_lines("OM-flexgrid-abc123", again["provisioning_psk"],
                                          again["last_wifi_error"], again["last_ssid"], rows=8)
        self.assertIn("LAST TRY FAILED", tall)
        self.assertIn("check password", tall)
        state = provisioning._ProvisioningState(again, SettingsManager, {})
        info = state.info_payload()
        self.assertEqual(info["last_wifi_error"], "wrong_password")
        self.assertEqual(info["last_ssid"], "HomeNet")
        self.assertEqual(info["state"], "unprovisioned")

    def test_no_ap_found_reason(self):
        settings = self.provisioned_settings()
        mgr, _ = make_nm(FakeSta([201]))
        run(flexgrid._handle_join_result(run(mgr.connect()), mgr, settings, self.display, self.led))
        self.assertEqual(self.saved()["last_wifi_error"], "no_ap_found")

    def test_good_join_marks_ok_and_later_outage_never_reverts(self):
        settings = self.provisioned_settings(wifi_password="right-pass-1")
        mgr, _ = make_nm(FakeSta([1001], up_after=2), wifi_password="right-pass-1")
        joined = run(mgr.connect())
        self.assertTrue(joined)
        self.assertFalse(run(flexgrid._handle_join_result(joined, mgr, settings, self.display, self.led)))
        self.assertTrue(self.saved()["wifi_joined_ok"])
        flexgrid.machine.reset.assert_not_called()

        # Reboot with the router off (5 minutes or 5 hours): every join
        # attempt fails, and nothing is ever wiped or reset.
        for _ in range(3):
            settings = SettingsManager.load()
            mgr, _ = make_nm(FakeSta([201]), wifi_password="right-pass-1")
            joined = run(mgr.connect())
            self.assertFalse(joined)
            self.assertEqual(mgr.last_error, "no_ap_found")
            self.assertFalse(run(flexgrid._handle_join_result(
                joined, mgr, settings, self.display, self.led)))
        after = self.saved()
        self.assertEqual(after["wifi_ssid"], "HomeNet")
        self.assertEqual(after["wifi_password"], "right-pass-1")
        self.assertTrue(after["wifi_joined_ok"])
        flexgrid.machine.reset.assert_not_called()

    def test_connect_exception_does_not_wipe(self):
        settings = self.provisioned_settings()
        mgr, _ = make_nm(FakeSta())
        mgr.last_error = ""    # connect() raised before classifying anything
        self.assertFalse(run(flexgrid._handle_join_result(False, mgr, settings, self.display, self.led)))
        self.assertEqual(self.saved()["wifi_ssid"], "HomeNet")

    def test_save_failure_keeps_credentials_and_does_not_reset(self):
        settings = self.provisioned_settings()
        mgr, _ = make_nm(FakeSta([202]))
        with mock.patch.object(SettingsManager, "save", return_value=False):
            rebooting = run(flexgrid._handle_join_result(run(mgr.connect()), mgr, settings,
                                                         self.display, self.led))
        self.assertFalse(rebooting)
        flexgrid.machine.reset.assert_not_called()
        self.assertEqual(settings["wifi_ssid"], "HomeNet")
        self.assertEqual(settings["wifi_password"], "wrong-pass-1")
        self.assertEqual(settings["last_wifi_error"], "")


class ScreenTests(unittest.TestCase):
    def test_every_screen_fits_the_oled(self):
        reasons = ("wrong_password", "no_ap_found", "timeout", "weird", "")
        for reason in reasons:
            for ssid in ("OM-flexgrid-d7af0b", "OM-lask5-01", "x" * 40):
                for tried in ("", "HomeNet", "A very long router name here"):
                    for lines in (screens.setup_screen_lines(ssid, "kd3mpq72xy", reason, tried),
                                  screens.join_failed_lines(reason or "timeout", tried)):
                        self.assertLessEqual(len(lines), screens.MAX_LINES)
                        for ln in lines:
                            self.assertLessEqual(len(ln), screens.COLS, ln)
        for mode in ("standby", "charge", "record"):
            lines = screens.mode_lines(mode)
            self.assertLessEqual(len(lines), screens.MAX_LINES)
            self.assertTrue(all(len(ln) <= screens.COLS for ln in lines), mode)
        self.assertIsNone(screens.mode_lines("stream"))

    def test_ap_name_and_psk_never_truncated(self):
        lines = screens.setup_screen_lines("OM-flexgrid-d7af0b", "kd3mpq72xy", "", "")
        self.assertEqual(lines, ["WIFI SETUP", "OM-flexgrid-", "d7af0b", "PW kd3mpq72xy"])
        self.assertEqual("".join(lines[1:3]), "OM-flexgrid-d7af0b")

    def test_short_ap_name_stays_on_one_line(self):
        self.assertEqual(screens.split_ssid("OM-lask5-01"), ["OM-lask5-01"])

    def test_reason_wording(self):
        text = " ".join(screens.reason_lines("no_ap_found"))
        self.assertIn("5 GHz", text)
        self.assertIn("password", " ".join(screens.reason_lines("wrong_password")))


if __name__ == "__main__":
    unittest.main()
