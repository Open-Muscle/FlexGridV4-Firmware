"""Factory flash mode, wipe tool and the shipped example settings."""
import contextlib
import io
import json
import os
import sys
import types
import unittest
from unittest import mock

import mp_stubs

import flash
import wipe

ROOT = mp_stubs.ROOT


class ExampleSettingsTests(unittest.TestCase):
    def load(self):
        with open(os.path.join(ROOT, "config", "settings.example.json")) as f:
            return json.load(f)

    def test_credentials_are_blank(self):
        d = self.load()
        self.assertEqual(d["wifi_ssid"], "")
        self.assertEqual(d["wifi_password"], "")

    def test_no_secret_looking_values(self):
        for k, v in self.load().items():
            if isinstance(v, str):
                self.assertNotIn("YOUR_", v, k)
        for k in self.load():
            self.assertNotIn("psk", k.lower())
            self.assertNotIn("token", k.lower())

    def test_keys_are_known_to_settings_manager(self):
        from settings_manager import SettingsManager
        for k in self.load():
            self.assertIn(k, SettingsManager.DEFAULTS, k)


class FlashArgsTests(unittest.TestCase):
    def test_parse_args(self):
        self.assertEqual(flash.parse_args(["--factory", "COM7"]), (True, ["COM7"]))
        self.assertEqual(flash.parse_args(["COM7", "COM8"]), (False, ["COM7", "COM8"]))
        self.assertEqual(flash.parse_args(["COM7", "--factory", "COM8"]), (True, ["COM7", "COM8"]))

    def test_unknown_option_is_an_error(self):
        with self.assertRaises(SystemExit):
            flash.parse_args(["--factroy", "COM7"])

    def test_factory_never_pushes_settings(self):
        remotes = [r for _l, r in flash.firmware_files(True)]
        self.assertNotIn("config/settings.json", remotes)
        self.assertIn("config/settings.json", [r for _l, r in flash.firmware_files(False)])

    def test_main_py_is_pushed_last(self):
        for factory in (True, False):
            files = flash.firmware_files(factory)
            self.assertEqual(files[-1][1], "main.py")
            self.assertEqual(files[-2][1], "boot.py")

    def test_every_lib_module_is_pushed(self):
        lib = {f for f in os.listdir(os.path.join(ROOT, "lib")) if f.endswith(".py")}
        pushed = {os.path.basename(r) for _l, r in flash.firmware_files(True) if r.startswith("lib/")}
        self.assertEqual(lib, pushed)

    def test_pushed_files_exist(self):
        for local, _remote in flash.firmware_files(True):
            self.assertTrue(os.path.exists(os.path.join(ROOT, local)), local)


class FlashMainTests(unittest.TestCase):
    def run_main(self, argv):
        pushed = []
        out = io.StringIO()
        patches = [
            mock.patch.object(sys, "argv", ["flash.py"] + argv),
            mock.patch.object(flash, "ensure_bootloader", lambda com: com),
            mock.patch.object(flash, "find_micropython_bin", lambda: "fw.bin"),
            mock.patch.object(flash, "list_serial_ports", lambda: set()),
            mock.patch.object(flash, "run", lambda *a, **k: None),
            mock.patch.object(flash, "push_files_raw", lambda com, files: pushed.append((com, files))),
            mock.patch.object(flash.time, "sleep", lambda s: None),
            # a checkout that has no config/settings.json, like a fresh clone
            mock.patch.object(flash.os.path, "exists",
                              lambda p: False if p.endswith("settings.json") else os.path.lexists(p)),
        ]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(contextlib.redirect_stdout(out))
            rc = flash.main()
        return rc, pushed, out.getvalue()

    def test_factory_flash_needs_no_settings_file(self):
        rc, pushed, out = self.run_main(["--factory", "COM7", "COM8"])
        self.assertEqual(rc, 0)
        com, files = pushed[0]
        self.assertEqual(com, "COM8")
        self.assertNotIn("config/settings.json", [r for _l, r in files])
        self.assertIn("WIFI SETUP", out)

    def test_normal_flash_without_settings_points_at_factory(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_main(["COM7", "COM8"])
        self.assertIn("--factory", str(cm.exception))

    def test_no_arguments_prints_usage(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["flash.py"]), contextlib.redirect_stdout(out):
            self.assertEqual(flash.main(), 1)
        self.assertIn("--factory", out.getvalue())


class FakeUos(types.ModuleType):
    def __init__(self, existing):
        super().__init__("uos")
        self.existing = set(existing)
        self.removed = []

    def remove(self, path):
        if path not in self.existing:
            raise OSError(2, "ENOENT")
        self.existing.discard(path)
        self.removed.append(path)


class WipeCodeTests(unittest.TestCase):
    """WIPE_CODE is what actually runs on the band; execute it against a fake
    filesystem."""

    def execute(self, existing, log_fp="open"):
        fake_uos = FakeUos(existing)
        fake_logger = types.ModuleType("logger")
        fp = mock.MagicMock()
        fake_logger._log_fp = fp if log_fp == "open" else None
        fake_logger._persist_enabled = True
        out = io.StringIO()
        with mock.patch.dict(sys.modules, {"uos": fake_uos, "logger": fake_logger}):
            with contextlib.redirect_stdout(out):
                exec(compile(wipe.WIPE_CODE, "<wipe>", "exec"), {})
        return fake_uos, fake_logger, fp, out.getvalue().strip()

    def test_removes_settings_and_both_logs(self):
        files = ["/config/settings.json", "/log.txt", "/log.txt.bak", "/main.py"]
        uos, logger_mod, fp, out = self.execute(files)
        self.assertEqual(sorted(uos.removed), ["/config/settings.json", "/log.txt", "/log.txt.bak"])
        self.assertEqual(uos.existing, {"/main.py"})          # firmware untouched
        self.assertEqual(out, "/config/settings.json,/log.txt,/log.txt.bak")

    def test_closes_the_open_log_first(self):
        _u, logger_mod, fp, _o = self.execute(["/log.txt"])
        fp.close.assert_called_once()
        self.assertIsNone(logger_mod._log_fp)
        self.assertFalse(logger_mod._persist_enabled)

    def test_already_clean_band_is_fine(self):
        uos, _l, _fp, out = self.execute([], log_fp="none")
        self.assertEqual(uos.removed, [])
        self.assertEqual(out, "")

    def test_partial_state(self):
        uos, *_ = self.execute(["/log.txt.bak"])
        self.assertEqual(uos.removed, ["/log.txt.bak"])


class WipeCommandTests(unittest.TestCase):
    def run_wipe(self, argv, reply=b"/config/settings.json,/log.txt,/log.txt.bak"):
        serial_obj = mock.MagicMock()
        fake_serial_mod = mock.MagicMock()
        fake_serial_mod.Serial.return_value = serial_obj
        out = io.StringIO()
        with mock.patch.object(flash, "serial", fake_serial_mod), \
                mock.patch.object(flash, "_enter_raw_repl") as enter, \
                mock.patch.object(flash, "_exec_raw", return_value=(reply, b"")) as ex, \
                mock.patch.object(wipe.time, "sleep", lambda s: None), \
                contextlib.redirect_stdout(out):
            rc = wipe.main(argv)
        return rc, serial_obj, enter, ex, out.getvalue()

    def test_wipe_runs_wipe_code_and_leaves_band_idle(self):
        rc, ser, enter, ex, out = self.run_wipe(["COM9"])
        self.assertEqual(rc, 0)
        enter.assert_called_once_with(ser)
        self.assertEqual(ex.call_args[0][1], wipe.WIPE_CODE)
        self.assertIn("/log.txt.bak", out)
        written = b"".join(c[0][0] for c in ser.write.call_args_list)
        self.assertNotIn(b"machine.reset", written)
        ser.close.assert_called_once()

    def test_reset_flag_reboots_the_band(self):
        _rc, ser, *_ = self.run_wipe(["COM9", "--reset"])
        written = b"".join(c[0][0] for c in ser.write.call_args_list)
        self.assertIn(b"machine.reset()", written)

    def test_bad_arguments_print_usage(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(wipe.main([]), 1)
            self.assertEqual(wipe.main(["COM9", "--bogus"]), 1)
        self.assertIn("settings.json", out.getvalue())

    def test_device_error_aborts(self):
        serial_obj = mock.MagicMock()
        fake_serial_mod = mock.MagicMock()
        fake_serial_mod.Serial.return_value = serial_obj
        with mock.patch.object(flash, "serial", fake_serial_mod), \
                mock.patch.object(flash, "_enter_raw_repl"), \
                mock.patch.object(flash, "_exec_raw", return_value=(b"", b"Traceback")), \
                mock.patch.object(wipe.time, "sleep", lambda s: None):
            with self.assertRaises(SystemExit):
                wipe.main(["COM9"])
        serial_obj.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class KitCheckImuAddressesTest(unittest.TestCase):
    """The pre-ship check must accept every IMU address the firmware driver
    does; an as-built board with the TOKMAS part at 0x37 was reported as
    having no IMU while the firmware had found it."""

    def test_kit_check_accepts_every_driver_address(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for sub in ("tools", "lib"):
            p = os.path.join(root, sub)
            if p not in sys.path:
                sys.path.insert(0, p)
        import kit_check
        import imu
        driver = set(imu._INV_ADDRS) | set(imu._TKM_ADDRS) | set(imu._BMI_ADDRS)
        self.assertTrue(driver <= set(kit_check.IMU_ADDRS),
                        sorted(driver - set(kit_check.IMU_ADDRS)))
