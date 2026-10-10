"""tools/kit_check.py: the pre-ship check. The code that runs on the band is
executed here against a temporary directory with a fake machine module, and
the decision logic is driven with hand-built reports."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import mp_stubs

import flash

sys.path.insert(0, os.path.join(mp_stubs.ROOT, "tools"))
import kit_check  # noqa: E402

ROOT = mp_stubs.ROOT
SECRET = "hunter2-wifi-password"

GOOD_PSK = "kd3mpq72xy"


def good_report(**over):
    r = {
        "settings_present": True, "device_id": "flexgrid-d7af0b", "device_type": "flexgrid",
        "role": "", "label": "", "psk": GOOD_PSK,
        "has_ssid": False, "has_password": False, "joined_ok": False, "has_last_ssid": False,
        "bak": False, "log": {"present": True, "bytes": 900, "net_lines": 0},
        "i2c": [0x3C, 0x68], "vbat": 4.01, "sd": True,
        "hashes": {},
    }
    r.update(over)
    return r


OPTS = {"min_battery": 3.7, "require_sd": False, "require_tag": False}
TAGGED = ("v1.0.0", "v1.0.0")


def levels(items):
    return {topic: level for level, topic, _m in items}


class EvaluateTests(unittest.TestCase):
    def ev(self, report=None, opts=None, tag=TAGGED):
        # firmware hashes: the report echoes what the checkout expects
        expected = {"flexgrid.py": {"aa"}}
        r = good_report(hashes={"flexgrid.py": "aa"})
        r.update(report or {})
        o = dict(OPTS)
        o.update(opts or {})
        return kit_check.evaluate(r, o, expected, tag)

    def test_factory_unit_passes(self):
        items = self.ev()
        self.assertNotIn("fail", {lvl for lvl, _t, _m in items})

    def test_each_credential_flag_fails_as_not_factory_state(self):
        for key in ("has_ssid", "has_password", "joined_ok", "has_last_ssid", "bak"):
            items = self.ev({key: True})
            self.assertEqual(levels(items)["factory state"], "fail", key)
            msg = [m for _l, t, m in items if t == "factory state"][0]
            self.assertIn("not in factory state", msg)

    def test_log_naming_a_network_fails_but_a_boot_log_passes(self):
        bad = self.ev({"log": {"present": True, "bytes": 5000, "net_lines": 3}})
        self.assertEqual(levels(bad)["factory state"], "fail")
        good = self.ev({"log": {"present": True, "bytes": 300, "net_lines": 0}})
        self.assertEqual(levels(good)["factory state"], "ok")

    def test_missing_screen_is_a_warning_by_default(self):
        # Kit bands ship without the screen, so a missing OLED must not fail.
        items = self.ev({"i2c": [0x68]})
        self.assertEqual(levels(items)["OLED"], "warn")
        self.assertNotIn("fail", {lvl for lvl, _t, _m in items})
        msg = [m for _l, t, m in items if t == "OLED"][0]
        self.assertIn("not fitted", msg)
        self.assertIn("0x3C", msg)
        self.assertIn("sticker is the only place the setup password lives", msg)
        self.assertIn("normal", msg)

    def test_require_screen_makes_a_missing_screen_fail(self):
        items = self.ev({"i2c": [0x68]}, {"require_screen": True})
        self.assertEqual(levels(items)["OLED"], "fail")
        msg = [m for _l, t, m in items if t == "OLED"][0]
        self.assertIn("not found at 0x3C", msg)
        self.assertIn("--require-screen", msg)
        # a screen that does answer is fine either way
        self.assertEqual(levels(self.ev({}, {"require_screen": True}))["OLED"], "ok")

    def test_a_dead_i2c_bus_still_fails(self):
        self.assertEqual(levels(self.ev({"i2c": None}))["OLED"], "fail")

    def test_missing_imu_only_warns(self):
        self.assertEqual(levels(self.ev({"i2c": [0x3C]}))["IMU"], "warn")

    def test_battery(self):
        self.assertEqual(levels(self.ev({"vbat": 3.55}))["battery"], "fail")
        msg = [m for _l, t, m in self.ev({"vbat": 3.55}) if t == "battery"][0]
        self.assertIn("3.55 V", msg)
        self.assertIn("charge", msg)
        self.assertEqual(levels(self.ev({"vbat": 0.1}))["battery"], "fail")
        self.assertEqual(levels(self.ev({"vbat": None}))["battery"], "fail")
        self.assertEqual(levels(self.ev({"vbat": 3.6}, {"min_battery": 3.5}))["battery"], "ok")

    def test_sd_warns_unless_required(self):
        self.assertEqual(levels(self.ev({"sd": False}))["SD card"], "warn")
        self.assertEqual(levels(self.ev({"sd": False}, {"require_sd": True}))["SD card"], "fail")

    def test_setup_password_shape(self):
        for psk in ("", "short", "0123456789", GOOD_PSK + "x"):
            self.assertEqual(levels(self.ev({"psk": psk}))["setup password"], "fail", psk)

    def test_blank_band_that_never_booted(self):
        items = self.ev({"settings_present": False, "device_id": None, "psk": ""})
        self.assertEqual(levels(items)["settings"], "fail")

    def test_firmware_must_match_the_checkout(self):
        self.assertEqual(levels(self.ev({"hashes": {"flexgrid.py": "bb"}}))["firmware"], "fail")
        self.assertEqual(levels(self.ev({"hashes": {"flexgrid.py": None}}))["firmware"], "fail")
        self.assertEqual(levels(self.ev({"hashes": {}}))["firmware"], "fail")

    def test_tag_is_a_warning_unless_required(self):
        untagged = (None, "v1.0.0-3-gabc123")
        self.assertEqual(levels(self.ev(tag=untagged))["release"], "warn")
        self.assertEqual(levels(self.ev(opts={"require_tag": True}, tag=untagged))["release"], "fail")
        self.assertEqual(levels(self.ev(tag=TAGGED))["release"], "ok")


class StickerTests(unittest.TestCase):
    def test_ap_name_rule_matches_the_firmware(self):
        import provisioning
        for dev_id in ("flexgrid-d7af0b", "flexgrid-abc123", "odd-id"):
            self.assertEqual(kit_check.ap_name("flexgrid", dev_id),
                             provisioning.ssid_for("flexgrid", dev_id))

    def test_sticker_text(self):
        lines = kit_check.sticker_lines(good_report(role="left", label="Band A"))
        text = "\n".join(lines)
        self.assertIn("flexgrid-d7af0b", text)
        self.assertIn("OM-flexgrid-d7af0b", text)
        self.assertIn(GOOD_PSK, text)
        self.assertIn("left", text)
        self.assertIn("Band A", text)
        plain = "\n".join(kit_check.sticker_lines(good_report()))
        self.assertNotIn("Role", plain)


class ArgTests(unittest.TestCase):
    def test_parse(self):
        o = kit_check.parse_args(["COM5", "--role", "left", "--label", "Band A", "--clear-logs",
                                  "--min-battery", "3.8", "--require-sd", "--require-tag", "--reset",
                                  "--require-screen"])
        self.assertEqual((o["port"], o["role"], o["label"], o["min_battery"]), ("COM5", "left", "Band A", 3.8))
        self.assertTrue(o["clear_logs"] and o["require_sd"] and o["require_tag"] and o["reset"])
        self.assertTrue(o["require_screen"])
        self.assertFalse(kit_check.parse_args(["COM5"])["require_screen"])

    def test_bad_arguments(self):
        for argv in ([], ["COM5", "COM6"], ["COM5", "--role", "arm"], ["COM5", "--label", "x" * 17],
                     ["COM5", "--bogus"], ["COM5", "--min-battery", "lots"], ["COM5", "--role"]):
            with self.assertRaises(ValueError, msg=argv):
                kit_check.parse_args(argv)

    def test_main_prints_usage_on_bad_arguments(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(kit_check.main([]), 2)
        self.assertIn("kit_check", out.getvalue())


class FakeBand:
    """Stands in for mpremote_exec: answers the apply and probe code."""

    def __init__(self, report, applied=None):
        self.report = report
        self.applied = applied or {"role_set": True, "label_set": True, "removed": []}
        self.calls = []

    def __call__(self, port, code):
        self.calls.append(code)
        if "machine.reset" in code and "KITCHECK" not in code:
            return ""
        if "'role_set'" in code:
            return "noise\nKITCHECK " + json.dumps(self.applied) + "\n"
        return "KITCHECK " + json.dumps(self.report) + "\n"


class RunCheckTests(unittest.TestCase):
    def run_it(self, argv, report, expected=None, tag=TAGGED):
        out = []
        band = FakeBand(report)
        rc = kit_check.run_check(kit_check.parse_args(argv), runner=band,
                                 expected={} if expected is None else expected,
                                 tag_info=tag, out=out.append)
        return rc, "\n".join(out), band

    def test_pass_prints_sticker(self):
        rc, text, _b = self.run_it(["COM5"], good_report())
        self.assertEqual(rc, 0)
        self.assertIn("PASS", text)
        self.assertIn("Sticker text:", text)
        self.assertIn("OM-flexgrid-d7af0b", text)
        self.assertIn(GOOD_PSK, text)

    def test_band_without_a_screen_passes_and_prints_the_sticker(self):
        rc, text, _b = self.run_it(["COM5"], good_report(i2c=[0x68]))
        self.assertEqual(rc, 0)
        self.assertIn("[warn] OLED: not fitted", text)
        self.assertIn("PASS", text)
        self.assertIn("Sticker text:", text)
        self.assertIn(GOOD_PSK, text)

    def test_require_screen_fails_a_band_without_one(self):
        rc, text, _b = self.run_it(["COM5", "--require-screen"], good_report(i2c=[0x68]))
        self.assertEqual(rc, 1)
        self.assertNotIn("Sticker text", text)

    def test_fail_prints_reasons_and_no_sticker(self):
        rc, text, _b = self.run_it(["COM5"], good_report(has_ssid=True, i2c=None))
        self.assertEqual(rc, 1)
        self.assertIn("FAIL (2 problems)", text)
        self.assertIn("not in factory state", text)
        self.assertNotIn("Sticker text", text)
        self.assertNotIn(GOOD_PSK, text)

    def test_role_label_and_clear_run_before_the_check(self):
        rc, text, band = self.run_it(["COM5", "--role", "left", "--label", "Band A", "--clear-logs"],
                                     good_report(role="left", label="Band A"))
        self.assertEqual(rc, 0)
        self.assertEqual(len(band.calls), 2)
        self.assertIn("'left'", band.calls[0])
        self.assertIn("'Band A'", band.calls[0])
        self.assertIn("os.remove", band.calls[0])
        self.assertIn("Role", text)

    def test_no_apply_step_without_options(self):
        _rc, _t, band = self.run_it(["COM5"], good_report())
        self.assertEqual(len(band.calls), 1)

    def test_reset_flag(self):
        _rc, text, band = self.run_it(["COM5", "--reset"], good_report())
        self.assertIn("machine.reset()", band.calls[-1])
        self.assertIn("rebooted", text)

    def test_communication_failure_is_exit_2(self):
        def broken(port, code):
            raise kit_check.BandError("mpremote failed on COM5: no device")
        out = []
        rc = kit_check.run_check(kit_check.parse_args(["COM5"]), runner=broken,
                                 expected={}, tag_info=TAGGED, out=out.append)
        self.assertEqual(rc, 2)
        self.assertIn("ERROR", out[0])

    def test_garbage_reply_is_exit_2(self):
        out = []
        rc = kit_check.run_check(kit_check.parse_args(["COM5"]), runner=lambda p, c: "Traceback...",
                                 expected={}, tag_info=TAGGED, out=out.append)
        self.assertEqual(rc, 2)

    def test_wifi_password_is_never_printed(self):
        # even a (hypothetical) report that carried it must not reach the output
        rc, text, _b = self.run_it(["COM5"], good_report(has_password=True, has_ssid=True))
        self.assertNotIn(SECRET, text)
        self.assertEqual(rc, 1)


class DeviceCodeTests(unittest.TestCase):
    """Run the code that goes to the band, on this PC, against a temp dir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name.replace("\\", "/") + "/"
        os.makedirs(os.path.join(self.tmp.name, "config"))
        self.machine = sys.modules["machine"]

    def write(self, rel, text):
        with open(os.path.join(self.tmp.name, rel), "w", encoding="utf-8") as f:
            f.write(text)

    def settings(self, **over):
        s = {"device_id": "flexgrid-d7af0b", "device_type": "flexgrid", "wifi_ssid": "",
             "wifi_password": "", "provisioning_psk": GOOD_PSK, "role": ""}
        s.update(over)
        self.write("config/settings.json", json.dumps(s))

    def run_code(self, code, i2c=(0x3C, 0x68), read_uv=2_000_000):
        i2c_obj = mock.MagicMock()
        i2c_obj.scan.return_value = list(i2c)
        adc = mock.MagicMock()
        adc.read_uv.return_value = read_uv
        real_listdir = os.listdir
        out = io.StringIO()
        with mock.patch.object(self.machine, "I2C", return_value=i2c_obj), \
                mock.patch.object(self.machine, "ADC", mock.MagicMock(return_value=adc)), \
                mock.patch.object(self.machine, "SDCard", mock.MagicMock()), \
                mock.patch.object(os, "mount", create=True), \
                mock.patch.object(os, "umount", create=True), \
                mock.patch.object(os, "listdir",
                                  lambda p=".": [] if p == "/" else real_listdir(p)), \
                contextlib.redirect_stdout(out):
            exec(compile(code, "<band>", "exec"), {})
        return kit_check.parse_report(out.getvalue()), out.getvalue()

    def probe(self, **kw):
        return self.run_code(kit_check.probe_code(root=self.root), **kw)

    def test_factory_band(self):
        self.settings()
        self.write("log.txt", "[INF] FlexGrid V4 startup\n[INF] AP up: ssid='OM-flexgrid-d7af0b' ip=192.168.4.1\n")
        r, _raw = self.probe()
        self.assertEqual(r["device_id"], "flexgrid-d7af0b")
        self.assertFalse(r["has_ssid"] or r["has_password"] or r["bak"])
        self.assertEqual(r["log"]["net_lines"], 0)
        self.assertEqual(r["i2c"], [0x3C, 0x68])
        self.assertEqual(r["vbat"], 4.0)
        self.assertEqual(r["psk"], GOOD_PSK)

    def test_used_band_is_detected_and_password_not_returned(self):
        self.settings(wifi_ssid="HomeNet", wifi_password=SECRET, wifi_joined_ok=True, last_ssid="HomeNet")
        self.write("log.txt", "[INF] Connecting to Wi-Fi SSID='HomeNet'\n"
                              "[INF] Settings: {'wifi_ssid': 'HomeNet', 'wifi_password': '<redacted>'}\n"
                              "[INF] Settings: {'wifi_ssid': '', 'last_ssid': ''}\n")
        self.write("log.txt.bak", "old")
        r, raw = self.probe()
        self.assertTrue(r["has_ssid"] and r["has_password"] and r["joined_ok"] and r["has_last_ssid"])
        self.assertTrue(r["bak"])
        self.assertEqual(r["log"]["net_lines"], 2)         # the blank-name line does not count
        self.assertNotIn(SECRET, raw)
        self.assertNotIn("HomeNet", raw)
        items = kit_check.evaluate(r, OPTS, {}, TAGGED)
        self.assertEqual(levels(items)["factory state"], "fail")

    def test_whitespace_ssid_counts_as_blank_like_the_firmware(self):
        self.settings(wifi_ssid="   ")
        r, _ = self.probe()
        self.assertFalse(r["has_ssid"])

    def test_missing_settings_and_missing_oled(self):
        r, _ = self.probe(i2c=())
        self.assertFalse(r["settings_present"])
        self.assertEqual(r["i2c"], [])

    def test_firmware_hashes_match_the_checkout(self):
        self.settings()
        for local, remote in flash.firmware_files(True):
            dst = os.path.join(self.tmp.name, remote)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(os.path.join(ROOT, local), dst)
        r, _ = self.probe()
        expected = kit_check.local_hashes()
        items = kit_check.evaluate(r, OPTS, expected, TAGGED)
        self.assertEqual(levels(items)["firmware"], "ok")
        # change one file on the "band"
        with open(os.path.join(self.tmp.name, "lib", "screens.py"), "a") as f:
            f.write("# edited\n")
        r2, _ = self.probe()
        msg = [m for lvl, t, m in kit_check.evaluate(r2, OPTS, expected, TAGGED) if t == "firmware"][0]
        self.assertIn("lib/screens.py", msg)

    def test_crlf_and_lf_copies_are_both_accepted(self):
        import hashlib
        accepted = kit_check.local_hashes()["main.py"]
        with open(os.path.join(ROOT, "main.py"), "rb") as f:
            raw = f.read()
        lf = raw.replace(b"\r\n", b"\n")
        for form in (raw, lf, lf.replace(b"\n", b"\r\n")):
            self.assertIn(hashlib.sha256(form).hexdigest(), accepted)

    def test_apply_sets_role_and_label_and_keeps_other_keys(self):
        self.settings(boot_seq=7)
        r, _ = self.run_code(kit_check.apply_code("left", "Band A", False, root=self.root))
        self.assertTrue(r["role_set"] and r["label_set"])
        with open(os.path.join(self.tmp.name, "config", "settings.json")) as f:
            s = json.load(f)
        self.assertEqual((s["role"], s["device_label"], s["boot_seq"]), ("left", "Band A", 7))
        self.assertEqual(s["provisioning_psk"], GOOD_PSK)

    def test_apply_without_settings_reports_it(self):
        r, _ = self.run_code(kit_check.apply_code("left", None, False, root=self.root))
        self.assertTrue(r.get("no_settings"))
        self.assertFalse(r["role_set"])

    def test_clear_logs_removes_both_logs_and_leaves_settings(self):
        self.settings()
        self.write("log.txt", "x")
        self.write("log.txt.bak", "y")
        fake_logger = mock.MagicMock()
        fake_logger._log_fp = mock.MagicMock()
        with mock.patch.dict(sys.modules, {"logger": fake_logger}):
            r, _ = self.run_code(kit_check.apply_code(None, None, True, root=self.root))
        self.assertEqual(sorted(r["removed"]), ["log.txt", "log.txt.bak"])
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "config", "settings.json")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "log.txt")))

    def test_pins_come_from_pinmap(self):
        pins = kit_check.pins_from_pinmap()
        self.assertEqual((pins["I2C_SDA"], pins["I2C_SCL"], pins["ADC_BAT"]), (8, 9, 18))
        self.assertIn("scl=machine.Pin(9)", kit_check.probe_code())

    def test_code_is_valid_python_for_the_band(self):
        compile(kit_check.probe_code(), "<probe>", "exec")
        compile(kit_check.apply_code("left", "A", True), "<apply>", "exec")


class MpremoteTransportTests(unittest.TestCase):
    def test_command_line(self):
        with mock.patch.object(kit_check.subprocess, "run") as run:
            run.return_value = mock.MagicMock(returncode=0, stdout="KITCHECK {}", stderr="")
            out = kit_check.mpremote_exec("COM5", "print(1)")
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[1:6], ["-m", "mpremote", "connect", "COM5", "exec"])
        self.assertEqual(out, "KITCHECK {}")

    def test_failure_becomes_band_error(self):
        with mock.patch.object(kit_check.subprocess, "run") as run:
            run.return_value = mock.MagicMock(returncode=1, stdout="", stderr="could not open port COM5")
            with self.assertRaises(kit_check.BandError) as cm:
                kit_check.mpremote_exec("COM5", "print(1)")
        self.assertIn("COM5", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
