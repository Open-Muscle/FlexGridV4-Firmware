"""USB console (lib/usb_console.py): the command channel the PC app uses to give
a band its Wi-Fi over the cable. The tests drive the parser and dispatcher with
plain strings, a fake settings dict and recording callables, so no board and no
serial port are involved."""
import asyncio
import json
import os
import tempfile
import unittest

import mp_stubs

import commands
import flexgrid
import provisioning
import usb_console as uc
from settings_manager import SettingsManager

CODES = {"bad_json", "unknown_verb", "bad_ssid", "bad_password", "save_failed"}


def run(coro):
    return asyncio.run(coro)


def cmd(verb, msg_id="c1", **fields):
    data = dict(fields)
    data["verb"] = verb
    return json.dumps({"v": "1.0", "type": "cmd", "id": msg_id, "data": data})


def fresh_settings():
    return {
        "device_id": "flexgrid-abc123", "device_type": "flexgrid", "fw_version": "v4.0.0",
        "wifi_ssid": "", "wifi_password": "", "wifi_joined_ok": True,
        "last_ssid": "old net", "last_wifi_error": "wrong_password",
        "provisioning_psk": "kd3mpq72xy", "role": "left", "boot_seq": 7,
    }


class Rig:
    """A console wired to recording fakes. `events` keeps the order in which
    replies were written and the restart ran."""

    def __init__(self, settings=None, save_ok=True, set_role=None, ip=""):
        self.settings = settings if settings is not None else fresh_settings()
        self.events = []
        self.writes = []
        self.saved = []
        self.save_ok = save_ok

        def save():
            self.saved.append(dict(self.settings))
            return self.save_ok

        def write(line):
            self.writes.append(line)
            self.events.append("reply")

        def restart():
            self.events.append("restart")

        async def sleep_ms(ms):
            self.events.append("sleep:{}".format(ms))

        self.console = uc.UsbConsole(self.settings, save, restart, set_role=set_role,
                                     get_ip=lambda: ip, write=write, sleep_ms=sleep_ms)

    def send(self, line):
        return run(self.console.process(line))


class ReplyShape(unittest.TestCase):
    def test_ok_reply_has_exactly_the_documented_keys_in_order(self):
        rig = Rig()
        out = rig.send(cmd("get_info", "abc"))
        self.assertEqual(list(out), ["v", "type", "id", "ok", "data"])
        self.assertEqual((out["v"], out["type"], out["id"], out["ok"]), ("1.0", "ack", "abc", True))
        self.assertEqual(json.loads(rig.writes[0]), out)
        self.assertEqual(len(rig.writes), 1)

    def test_error_reply_has_exactly_the_documented_keys_in_order(self):
        rig = Rig()
        out = rig.send(cmd("set_wifi", "e9", ssid="", password=""))
        self.assertEqual(list(out), ["v", "type", "id", "ok", "error", "message"])
        self.assertEqual((out["v"], out["type"], out["id"], out["ok"]), ("1.0", "ack", "e9", False))
        self.assertIsInstance(out["message"], str)
        self.assertTrue(out["message"])
        self.assertNotIn("data", out)

    def test_reply_is_one_line_of_json(self):
        rig = Rig()
        rig.send(cmd("get_info"))
        self.assertNotIn("\n", rig.writes[0])
        self.assertTrue(rig.writes[0].startswith("{"))

    def test_id_is_echoed_even_when_it_is_a_number(self):
        self.assertEqual(Rig().send(cmd("get_info", 41))["id"], 41)

    def test_id_missing_gives_an_empty_id(self):
        line = json.dumps({"v": "1.0", "type": "cmd", "data": {"verb": "get_info"}})
        self.assertEqual(Rig().send(line)["id"], "")

    def test_a_line_with_no_type_is_still_a_command(self):
        line = json.dumps({"id": "x", "data": {"verb": "get_info"}})
        self.assertTrue(Rig().send(line)["ok"])


class GetInfo(unittest.TestCase):
    def test_fields(self):
        rig = Rig(ip="192.168.1.50")
        rig.settings["wifi_ssid"] = "HomeNet"
        data = rig.send(cmd("get_info"))["data"]
        self.assertEqual(data, {
            "id": "flexgrid-abc123", "role": "left", "fw": "v4.0.0",
            "state": "provisioned", "wifi_ssid": "HomeNet",
            "ip": "192.168.1.50", "usb_console": 1})

    def test_unprovisioned_when_no_ssid(self):
        data = Rig().send(cmd("get_info"))["data"]
        self.assertEqual(data["state"], "unprovisioned")
        self.assertEqual(data["wifi_ssid"], "")
        self.assertEqual(data["ip"], "")

    def test_never_reports_the_password_or_the_setup_password(self):
        rig = Rig()
        rig.settings["wifi_ssid"] = "HomeNet"
        rig.settings["wifi_password"] = "hunter2hunter2"
        rig.send(cmd("get_info"))
        self.assertNotIn("hunter2hunter2", rig.writes[0])
        self.assertNotIn("kd3mpq72xy", rig.writes[0])

    def test_does_not_restart_or_save(self):
        rig = Rig()
        rig.send(cmd("get_info"))
        self.assertEqual(rig.events, ["reply"])
        self.assertEqual(rig.saved, [])


class SetWifi(unittest.TestCase):
    def test_saves_the_credentials_and_clears_the_failure_state(self):
        rig = Rig()
        out = rig.send(cmd("set_wifi", ssid="HomeNet", password="correct horse"))
        self.assertEqual(out["data"], {"restart": True})
        s = rig.settings
        self.assertEqual(s["wifi_ssid"], "HomeNet")
        self.assertEqual(s["wifi_password"], "correct horse")
        self.assertIs(s["wifi_joined_ok"], False)
        self.assertEqual(s["last_ssid"], "")
        self.assertEqual(s["last_wifi_error"], "")
        self.assertEqual(len(rig.saved), 1)
        self.assertEqual(rig.saved[0]["wifi_ssid"], "HomeNet")

    def test_keeps_device_id_role_and_setup_password(self):
        rig = Rig()
        keys = ("device_id", "role", "provisioning_psk", "boot_seq")
        before = {k: rig.settings[k] for k in keys}
        rig.send(cmd("set_wifi", ssid="HomeNet", password="correct horse"))
        self.assertEqual(before, {k: rig.settings[k] for k in keys})
        self.assertEqual(rig.saved[0]["provisioning_psk"], "kd3mpq72xy")
        self.assertEqual(rig.saved[0]["device_id"], "flexgrid-abc123")
        self.assertEqual(rig.saved[0]["role"], "left")

    def test_open_network_with_empty_or_missing_password(self):
        for fields in ({"ssid": "Cafe", "password": ""}, {"ssid": "Cafe"},
                       {"ssid": "Cafe", "password": None}):
            rig = Rig()
            self.assertTrue(rig.send(cmd("set_wifi", **fields))["ok"], fields)
            self.assertEqual(rig.settings["wifi_password"], "")

    def test_ssid_and_password_stored_exactly_as_sent(self):
        rig = Rig()
        rig.send(cmd("set_wifi", ssid=" My Net ", password=" spaced pass "))
        self.assertEqual(rig.settings["wifi_ssid"], " My Net ")
        self.assertEqual(rig.settings["wifi_password"], " spaced pass ")

    def test_length_limits_are_in_utf8_bytes(self):
        self.assertTrue(Rig().send(cmd("set_wifi", ssid="a" * 32, password=""))["ok"])
        self.assertTrue(Rig().send(cmd("set_wifi", ssid="x", password="p" * 8))["ok"])
        self.assertTrue(Rig().send(cmd("set_wifi", ssid="x", password="p" * 63))["ok"])
        euro = chr(0x20AC)   # three bytes each in UTF-8
        self.assertEqual(Rig().send(cmd("set_wifi", ssid=euro * 11, password=""))["error"], "bad_ssid")
        self.assertTrue(Rig().send(cmd("set_wifi", ssid=euro * 10, password=""))["ok"])

    def test_bad_ssid(self):
        for ssid in ("", "a" * 33, "   ", None, 12, ["x"]):
            rig = Rig()
            before = dict(rig.settings)
            out = rig.send(cmd("set_wifi", ssid=ssid, password="correct horse"))
            self.assertEqual((out["ok"], out["error"]), (False, "bad_ssid"), repr(ssid))
            self.assertEqual(rig.settings, before)
            self.assertEqual(rig.saved, [])
            self.assertNotIn("restart", rig.events)

    def test_missing_ssid_field_is_bad_ssid(self):
        self.assertEqual(Rig().send(cmd("set_wifi", password="correct horse"))["error"], "bad_ssid")

    def test_bad_password(self):
        for pw in ("short", "a" * 7, "a" * 64, 12345678, ["abcdefgh"]):
            rig = Rig()
            before = dict(rig.settings)
            out = rig.send(cmd("set_wifi", ssid="HomeNet", password=pw))
            self.assertEqual((out["ok"], out["error"]), (False, "bad_password"), repr(pw))
            self.assertEqual(rig.settings, before)
            self.assertEqual(rig.saved, [])
            self.assertNotIn("restart", rig.events)

    def test_bad_ssid_is_reported_before_bad_password(self):
        self.assertEqual(Rig().send(cmd("set_wifi", ssid="", password="x"))["error"], "bad_ssid")

    def test_save_failed_rolls_back_and_does_not_restart(self):
        rig = Rig(save_ok=False)
        before = dict(rig.settings)
        out = rig.send(cmd("set_wifi", ssid="HomeNet", password="correct horse"))
        self.assertEqual((out["ok"], out["error"]), (False, "save_failed"))
        self.assertEqual(rig.settings, before)
        self.assertEqual(rig.events, ["reply"])

    def test_save_raising_is_save_failed(self):
        rig = Rig()

        def boom():
            raise OSError("flash full")
        rig.console._save = boom
        out = rig.send(cmd("set_wifi", ssid="HomeNet", password="correct horse"))
        self.assertEqual(out["error"], "save_failed")
        self.assertEqual(rig.settings["wifi_ssid"], "")

    def test_reply_comes_before_the_restart(self):
        rig = Rig()
        rig.send(cmd("set_wifi", ssid="HomeNet", password="correct horse"))
        self.assertEqual(rig.events, ["reply", "sleep:1000", "restart"])

    def test_the_reply_is_already_written_when_the_restart_callback_runs(self):
        rig = Rig()
        seen = []
        rig.console._restart = lambda: seen.append(list(rig.writes))
        rig.send(cmd("set_wifi", ssid="HomeNet", password="correct horse"))
        self.assertEqual(len(seen), 1)
        self.assertEqual(json.loads(seen[0][0])["data"], {"restart": True})

    def test_name_and_password_never_reach_the_log_or_the_reply(self):
        import logger
        lines = []
        real = logger._emit
        logger._emit = lambda level, msg, persist, flush: lines.append(msg)
        try:
            rig = Rig()
            rig.send(cmd("set_wifi", ssid="UniqueNetName", password="UniquePassw0rd"))
        finally:
            logger._emit = real
        blob = " ".join(lines) + " ".join(rig.writes)
        self.assertTrue(lines)
        self.assertNotIn("UniquePassw0rd", blob)
        self.assertNotIn("UniqueNetName", blob)


class ForgetWifi(unittest.TestCase):
    def test_blanks_the_same_fields_and_restarts(self):
        rig = Rig()
        rig.settings.update(wifi_ssid="HomeNet", wifi_password="correct horse")
        out = rig.send(cmd("forget_wifi"))
        self.assertEqual(out["data"], {"restart": True})
        s = rig.settings
        self.assertEqual((s["wifi_ssid"], s["wifi_password"]), ("", ""))
        self.assertIs(s["wifi_joined_ok"], False)
        self.assertEqual((s["last_ssid"], s["last_wifi_error"]), ("", ""))
        self.assertEqual((s["device_id"], s["role"], s["provisioning_psk"]),
                         ("flexgrid-abc123", "left", "kd3mpq72xy"))
        self.assertEqual(rig.events, ["reply", "sleep:1000", "restart"])

    def test_save_failed(self):
        rig = Rig(save_ok=False)
        rig.settings["wifi_ssid"] = "HomeNet"
        out = rig.send(cmd("forget_wifi"))
        self.assertEqual(out["error"], "save_failed")
        self.assertEqual(rig.settings["wifi_ssid"], "HomeNet")
        self.assertEqual(rig.events, ["reply"])

    def test_extra_fields_are_ignored(self):
        self.assertTrue(Rig().send(cmd("forget_wifi", junk=1))["ok"])


class SetRole(unittest.TestCase):
    def test_default_handler_stores_and_saves_a_valid_role(self):
        for role in ("left", "right", "labeler", ""):
            rig = Rig()
            out = rig.send(cmd("set_role", role=role))
            self.assertEqual(out["data"], {"role": role})
            self.assertEqual(rig.settings["role"], role)
            self.assertEqual(rig.events, ["reply"])
            self.assertEqual(len(rig.saved), 1)

    def test_unknown_or_missing_role_is_cleared_like_the_tcp_verb(self):
        for fields in ({"role": "middle"}, {}, {"role": 5}, {"role": None}):
            rig = Rig()
            out = rig.send(cmd("set_role", **fields))
            self.assertEqual(out["data"], {"role": ""}, fields)
            self.assertEqual(rig.settings["role"], "")

    def test_reuses_the_tcp_handler_from_commands_py(self):
        class State:
            role = ""

            def __init__(self):
                self.saved = []

            def set_role(self, role):
                self.role = role if role in ("left", "right", "labeler", "") else ""

            def save_settings(self, key, value):
                self.saved.append((key, value))

        st = State()
        tcp = commands.build_handlers(st)["set_role"]

        async def usb_set_role(role):
            return (await tcp({"role": role}, ("usb", 0)))["role"]

        rig = Rig(set_role=usb_set_role)
        out = rig.send(cmd("set_role", role="right"))
        self.assertEqual(out["data"], {"role": "right"})
        self.assertEqual(st.saved, [("role", "right")])
        self.assertEqual(rig.send(cmd("set_role", role="bogus"))["data"], {"role": ""})

    def test_does_not_touch_wifi_or_restart(self):
        rig = Rig()
        rig.settings["wifi_ssid"] = "HomeNet"
        rig.send(cmd("set_role", role="right"))
        self.assertEqual(rig.settings["wifi_ssid"], "HomeNet")
        self.assertNotIn("restart", rig.events)


class ErrorCodes(unittest.TestCase):
    def test_bad_json(self):
        for line in ('{"v":"1.0","type":"cmd"', '{not json}', '{"a":}', '{}{}',
                     '{"data": 5, "id": "k"}', '{"data": [1], "id": "k"}'):
            out = Rig().send(line)
            self.assertEqual((out["ok"], out["error"]), (False, "bad_json"), line)

    def test_lines_that_are_not_brace_objects_are_ignored(self):
        self.assertIsNone(Rig().send("[1]"))
        self.assertIsNone(Rig().send("5"))

    def test_invalid_utf8_after_the_brace_is_bad_json(self):
        out = Rig().send(b'{"id":"k","data":{"verb":"get_info","x":"\xff\xfe"}}')
        self.assertEqual(out["error"], "bad_json")

    def test_unknown_verb(self):
        for data in ({"verb": "reboot"}, {"verb": ""}, {}, {"verb": 5}, {"verb": None}):
            line = json.dumps({"v": "1.0", "type": "cmd", "id": "u1", "data": data})
            out = Rig().send(line)
            self.assertEqual((out["ok"], out["error"], out["id"]), (False, "unknown_verb", "u1"), data)

    def test_missing_data_is_unknown_verb(self):
        out = Rig().send(json.dumps({"v": "1.0", "type": "cmd", "id": "u2"}))
        self.assertEqual(out["error"], "unknown_verb")

    def test_every_documented_code_is_reachable(self):
        seen = {
            Rig().send("{oops}")["error"],
            Rig().send(cmd("nope"))["error"],
            Rig().send(cmd("set_wifi", ssid="", password=""))["error"],
            Rig().send(cmd("set_wifi", ssid="a", password="b"))["error"],
            Rig(save_ok=False).send(cmd("set_wifi", ssid="a", password=""))["error"],
        }
        self.assertEqual(seen, CODES)

    def test_a_crashing_verb_gives_a_reply_not_a_dead_task(self):
        async def boom(role):
            raise RuntimeError("kaput")
        out = Rig(set_role=boom).send(cmd("set_role", role="left"))
        self.assertFalse(out["ok"])
        self.assertIn(out["error"], CODES)


class IgnoredLines(unittest.TestCase):
    def test_blank_and_junk_lines_get_no_reply(self):
        for line in ("", "   ", "\r", "\t", "hello", "[INF] Wi-Fi saved", "ok", "}", "# {",
                     b"", b"\x04", b"\x01", b"\x03\x03", "0x{"):
            rig = Rig()
            self.assertIsNone(rig.send(line), repr(line))
            self.assertEqual(rig.writes, [], repr(line))
            self.assertEqual(rig.events, [], repr(line))

    def test_raw_repl_control_bytes_are_ignored(self):
        good = cmd("get_info")
        for bad in ("\x01" + good, good[:5] + "\x04" + good[5:], good + "\x00", "\x03" + good,
                    good[:10] + "\x1b[A" + good[10:]):
            self.assertIsNone(Rig().send(bad), repr(bad))
        # Surrounding whitespace and a CR are fine.
        self.assertTrue(Rig().send("  " + good + "\r")["ok"])

    def test_our_own_ack_echoed_back_is_not_answered(self):
        rig = Rig()
        echoed = json.dumps({"v": "1.0", "type": "ack", "id": "c1", "ok": True, "data": {}})
        self.assertIsNone(rig.send(echoed))
        self.assertEqual(rig.writes, [])

    def test_bytes_input_works_like_str(self):
        out = Rig().send(cmd("get_info").encode("utf-8") + b"\r\n")
        self.assertTrue(out["ok"])

    def test_clean_line(self):
        self.assertIsNone(uc.clean_line(""))
        self.assertIsNone(uc.clean_line("abc"))
        self.assertIsNone(uc.clean_line("{\x01}"))
        self.assertEqual(uc.clean_line('  {"a": 1}\r\n'), '{"a": 1}')
        self.assertEqual(uc.clean_line('{"a":\t1}'), '{"a":\t1}')


class LineBufferTests(unittest.TestCase):
    def feed_all(self, buf, data):
        out = []
        for b in data:
            r = buf.feed(b)
            if r is not None:
                out.append(r)
        return out

    def test_splits_lines(self):
        buf = uc.LineBuffer(now_ms=lambda: 1000)
        self.assertEqual(self.feed_all(buf, b'{"a":1}\n{"b":2}\n'), [b'{"a":1}', b'{"b":2}'])

    def test_accepts_one_byte_bytes_objects(self):
        buf = uc.LineBuffer(now_ms=lambda: 1000)
        got = [buf.feed(bytes([b])) for b in b'{}\n']
        self.assertEqual(got[-1], b'{}')

    def test_crlf_leaves_a_cr_for_clean_line_to_strip(self):
        buf = uc.LineBuffer(now_ms=lambda: 1000)
        (line,) = self.feed_all(buf, b'{"a":1}\r\n')
        self.assertEqual(uc.clean_line(line), '{"a":1}')

    def test_oversize_line_is_dropped_up_to_its_newline_then_recovers(self):
        buf = uc.LineBuffer(now_ms=lambda: 1000)
        data = b"{" + b"x" * (uc.MAX_LINE_BYTES + 50) + b"\n" + b'{"ok":1}\n'
        self.assertEqual(self.feed_all(buf, data), [b'{"ok":1}'])

    def test_stale_half_line_is_discarded(self):
        t = [1000]
        buf = uc.LineBuffer(now_ms=lambda: t[0])
        self.feed_all(buf, b'{"half')
        t[0] += uc.STALE_LINE_MS + 10
        self.assertEqual(self.feed_all(buf, b'{"a":1}\n'), [b'{"a":1}'])

    def test_fresh_half_line_is_kept(self):
        t = [1000]
        buf = uc.LineBuffer(now_ms=lambda: t[0])
        self.feed_all(buf, b'{"a":')
        t[0] += 500
        self.assertEqual(self.feed_all(buf, b'1}\n'), [b'{"a":1}'])

    def test_a_long_valid_set_wifi_line_fits(self):
        line = cmd("set_wifi", "id", ssid="s" * 32, password="p" * 63).encode()
        self.assertLess(len(line), uc.MAX_LINE_BYTES)

    # MicroPython's sys.stdin is a text stream: read(1) gives a 1-char str.
    def test_accepts_one_char_strs(self):
        buf = uc.LineBuffer(now_ms=lambda: 1000)
        self.assertIsNone(buf.feed("{"))
        self.assertIsNone(buf.feed("}"))
        self.assertEqual(buf.feed("\n"), b"{}")

    def test_a_str_stream_gives_the_same_lines_as_bytes(self):
        text = cmd("get_info", "s1") + "\r\n" + cmd("set_role", "s2", role="left") + "\n"
        a = uc.LineBuffer(now_ms=lambda: 1000)
        b = uc.LineBuffer(now_ms=lambda: 1000)
        from_str = [r for r in (a.feed(ch) for ch in text) if r is not None]
        from_bytes = self.feed_all(b, text.encode())
        self.assertEqual(from_str, from_bytes)
        self.assertEqual(len(from_str), 2)

    def test_non_ascii_str_chars_keep_all_their_utf8_bytes(self):
        buf = uc.LineBuffer(now_ms=lambda: 1000)
        line = None
        for ch in '{"ssid":"café"}\n':
            r = buf.feed(ch)
            if r is not None:
                line = r
        self.assertEqual(line, '{"ssid":"café"}'.encode("utf-8"))
        self.assertEqual(uc.clean_line(line), '{"ssid":"café"}')

    def test_a_lone_high_str_char_does_not_raise(self):
        buf = uc.LineBuffer(now_ms=lambda: 1000)
        self.assertIsNone(buf.feed("ÿ"))
        self.assertIsNone(buf.feed(0xFF))
        self.assertIsNone(buf.feed(b"\xff"))

    def test_empty_inputs_are_ignored(self):
        buf = uc.LineBuffer(now_ms=lambda: 1000)
        self.assertIsNone(buf.feed(""))
        self.assertIsNone(buf.feed(b""))
        self.assertIsNone(buf.feed(bytearray()))
        buf.feed("{")
        buf.feed("}")
        self.assertEqual(buf.feed("\n"), b"{}")


class ServeTask(unittest.TestCase):
    """serve() against a scripted byte source."""

    def run_serve(self, rig, read_byte):
        async def main():
            try:
                await uc.serve(rig.console, read_byte)
            except asyncio.CancelledError:
                pass
        run(main())

    def byte_source(self, data):
        data = list(data)

        async def read_byte():
            if not data:
                raise asyncio.CancelledError()
            return bytes([data.pop(0)])
        return read_byte

    def test_answers_a_command_stream(self):
        stream = (b"boot noise\r\n[INF] hello\r\n" + cmd("get_info", "a").encode() + b"\r\n\r\n"
                  + cmd("set_role", "b", role="right").encode() + b"\n")
        rig = Rig()
        self.run_serve(rig, self.byte_source(stream))
        replies = [json.loads(w) for w in rig.writes]
        self.assertEqual([r["id"] for r in replies], ["a", "b"])
        self.assertEqual(replies[1]["data"], {"role": "right"})

    def test_set_wifi_through_the_task_replies_then_restarts(self):
        rig = Rig()
        line = cmd("set_wifi", "w", ssid="HomeNet", password="correct horse").encode() + b"\n"
        self.run_serve(rig, self.byte_source(line))
        self.assertEqual(rig.events, ["reply", "sleep:1000", "restart"])

    def test_an_exception_in_the_source_does_not_end_the_task(self):
        rig = Rig()
        script = [OSError("usb glitch"), b"", ValueError("bad byte")]
        script += [bytes([b]) for b in (cmd("get_info", "z") + "\n").encode()]

        async def read_byte():
            if not script:
                raise asyncio.CancelledError()
            item = script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        self.run_serve(rig, read_byte)
        self.assertEqual([json.loads(w)["id"] for w in rig.writes], ["z"])

    def test_str_source_gets_a_correct_ack_end_to_end(self):
        # The shape sys.stdin has on the board: 1-char strs, not bytes.
        text = ("boot noise\r\n" + cmd("get_info", "str1") + "\r\n"
                + cmd("set_role", "str2", role="right") + "\n")
        chars = list(text)

        async def read_byte():
            if not chars:
                raise asyncio.CancelledError()
            return chars.pop(0)
        rig = Rig()
        self.run_serve(rig, read_byte)
        replies = [json.loads(w) for w in rig.writes]
        self.assertEqual([r["id"] for r in replies], ["str1", "str2"])
        self.assertTrue(all(r["ok"] for r in replies))
        self.assertEqual(replies[0]["data"]["id"], "flexgrid-abc123")
        self.assertEqual(replies[1]["data"], {"role": "right"})

    def test_str_set_wifi_replies_then_restarts(self):
        chars = list(cmd("set_wifi", "w", ssid="Café Net", password="correct horse") + "\n")

        async def read_byte():
            if not chars:
                raise asyncio.CancelledError()
            return chars.pop(0)
        rig = Rig()
        self.run_serve(rig, read_byte)
        self.assertEqual(rig.events, ["reply", "sleep:1000", "restart"])
        self.assertEqual(rig.settings["wifi_ssid"], "Café Net")

    def run_script(self, script):
        """Run serve() over a script of exceptions / byte values; returns the
        list of logger.warn messages."""
        warns = []
        real = uc.logger.warn
        uc.logger.warn = warns.append
        script = list(script)

        async def read_byte():
            if not script:
                raise asyncio.CancelledError()
            item = script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        try:
            self.run_serve(Rig(), read_byte)
        finally:
            uc.logger.warn = real
        return warns

    def test_a_persistent_read_error_is_logged_once_then_rarely(self):
        n = uc.ERROR_LOG_EVERY * 2 + 5
        warns = self.run_script([OSError("usb gone")] * n)
        # first failure, then one per ERROR_LOG_EVERY repeats
        self.assertEqual(len(warns), 3)
        self.assertIn("OSError", warns[0])
        self.assertIn("x{}".format(uc.ERROR_LOG_EVERY + 1), warns[1])

    def test_a_new_kind_of_error_is_logged_at_once(self):
        warns = self.run_script([OSError("a")] * 5 + [ValueError("b")] * 5 + [OSError("c")])
        self.assertEqual(len(warns), 3)
        self.assertIn("ValueError", warns[1])

    def test_errors_between_good_bytes_are_each_logged(self):
        # a good byte resets the counter, so isolated glitches are not hidden
        warns = self.run_script([OSError("g1"), b" ", OSError("g2"), b" "])
        self.assertEqual(len(warns), 2)

    def test_keyboard_interrupt_is_not_swallowed(self):
        rig = Rig()

        async def read_byte():
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            run(uc.serve(rig.console, read_byte))

    def test_a_handler_failure_does_not_end_the_task(self):
        rig = Rig()
        calls = {"n": 0}
        real = rig.console.process

        async def flaky(line):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("bad byte")
            return await real(line)
        rig.console.process = flaky
        stream = (cmd("get_info", "1") + "\n" + cmd("get_info", "2") + "\n").encode()
        self.run_serve(rig, self.byte_source(stream))
        self.assertEqual([json.loads(w)["id"] for w in rig.writes], ["2"])

    def test_junk_then_a_command_still_works(self):
        rig = Rig()
        stream = b"\x01\x03\x04 garbage" + b"\n" + cmd("get_info", "k").encode() + b"\n"
        self.run_serve(rig, self.byte_source(stream))
        self.assertEqual([json.loads(w)["id"] for w in rig.writes], ["k"])


class WiringTests(unittest.TestCase):
    def test_limits_match_the_http_provisioning_path(self):
        self.assertEqual(uc.SSID_MAX_BYTES, provisioning.SSID_MAX_BYTES)
        self.assertEqual(uc.PSK_MIN_BYTES, provisioning.PSK_MIN_BYTES)
        self.assertEqual(uc.PSK_MAX_BYTES, provisioning.PSK_MAX_BYTES)

    def test_wifi_fields_written_match_the_http_provision_handler(self):
        import inspect
        src = inspect.getsource(provisioning._handle)
        keys = ("wifi_ssid", "wifi_password", "wifi_joined_ok", "last_wifi_error", "last_ssid")
        for key in keys:
            self.assertIn('settings["{}"]'.format(key), src)
        self.assertEqual(set(Rig().console._wifi_changes("a", "b")), set(keys))

    def test_flexgrid_starts_the_console_in_both_branches(self):
        import inspect
        main_src = inspect.getsource(flexgrid.main)
        self.assertEqual(main_src.count("start_usb_console("), 2)
        # Setup branch: right after the LED loop task, before the access point comes up.
        setup = main_src.index("setup_led_loop(status_led, led_name)")
        first = main_src.index("start_usb_console(")
        self.assertLess(setup, first)
        # The setup branch starts the console only after the access point is
        # up: compiling the module first left too little internal RAM for the
        # Wi-Fi driver on a kit band (OSError: WiFi Out of Memory).
        self.assertGreater(first, main_src.index("provisioning.start_ap("))
        self.assertLess(first, main_src.index("provisioning.serve("))
        # Provisioned branch: with the other tasks.
        second = main_src.rindex("start_usb_console(")
        self.assertGreater(second, main_src.index("cmd_server.serve_forever()"))
        self.assertLess(second, main_src.index("sensor_loop(state"))

    def _with_fake_create_task(self, fn):
        real = asyncio.create_task
        created = []

        def fake(coro):
            created.append(coro)
            coro.close()
        asyncio.create_task = fake
        try:
            return fn(), created
        finally:
            asyncio.create_task = real

    def test_start_usb_console_builds_a_task_with_machine_reset(self):
        import machine
        console, created = self._with_fake_create_task(
            lambda: flexgrid.start_usb_console(fresh_settings(), None))
        self.assertIsNotNone(console)
        self.assertEqual(len(created), 1)
        self.assertIs(console._restart, machine.reset)

    def test_start_usb_console_survives_a_failure(self):
        real = asyncio.create_task

        def boom(coro):
            coro.close()
            raise RuntimeError("no loop")
        asyncio.create_task = boom
        try:
            self.assertIsNone(flexgrid.start_usb_console(fresh_settings(), None))
        finally:
            asyncio.create_task = real

    def test_save_goes_through_settings_manager_with_the_live_dict(self):
        s = fresh_settings()
        saved = []
        real = SettingsManager.save
        SettingsManager.save = staticmethod(lambda st: saved.append(dict(st)) or True)
        try:
            console, _ = self._with_fake_create_task(lambda: flexgrid.start_usb_console(s, None))
            console._write = lambda line: None

            async def nosleep(ms):
                pass
            console._sleep_ms = nosleep
            console._restart = lambda: None
            run(console.process(cmd("set_wifi", ssid="HomeNet", password="correct horse")))
        finally:
            SettingsManager.save = real
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["wifi_ssid"], "HomeNet")
        self.assertEqual(saved[0]["provisioning_psk"], "kd3mpq72xy")

    def test_real_settings_file_round_trip(self):
        s = fresh_settings()
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            try:
                console, _ = self._with_fake_create_task(lambda: flexgrid.start_usb_console(s, None))
                console._write = lambda line: None

                async def nosleep(ms):
                    pass
                console._sleep_ms = nosleep
                console._restart = lambda: None
                run(console.process(cmd("set_wifi", ssid="HomeNet", password="correct horse")))
                with open(os.path.join("config", "settings.json"), encoding="utf-8") as f:
                    on_disk = json.load(f)
            finally:
                os.chdir(cwd)
        self.assertEqual(on_disk["wifi_ssid"], "HomeNet")
        self.assertEqual(on_disk["wifi_password"], "correct horse")
        self.assertEqual(on_disk["device_id"], "flexgrid-abc123")
        self.assertEqual(on_disk["provisioning_psk"], "kd3mpq72xy")
        self.assertEqual(on_disk["role"], "left")
        self.assertIs(on_disk["wifi_joined_ok"], False)

    def test_info_and_announce_carry_usb_console(self):
        import inspect
        from discovery import Discovery
        self.assertIn('"usb_console": 1', inspect.getsource(Discovery._announce_payload))
        self.assertGreaterEqual(inspect.getsource(flexgrid.main).count('"usb_console": 1'), 2)

    def test_keyboard_interrupt_handling_is_untouched(self):
        for name in ("flexgrid.py", "boot.py", "main.py", os.path.join("lib", "usb_console.py")):
            with open(os.path.join(mp_stubs.ROOT, name), encoding="utf-8") as f:
                self.assertNotIn("kbd_intr", f.read(), name)

    def test_the_module_is_pushed_to_the_band(self):
        import flash
        self.assertIn("lib/usb_console.py", [r for _l, r in flash.firmware_files(True)])


class DocsTests(unittest.TestCase):
    """docs/usb-console.md must keep matching the code: every example is run."""

    def text(self, *parts):
        with open(os.path.join(mp_stubs.ROOT, *parts), encoding="utf-8") as f:
            return f.read().replace("\r\n", "\n")

    def json_blocks(self):
        import re
        found = re.findall(r"```json\n(.*?)\n```", self.text("docs", "usb-console.md"), re.S)
        return [json.loads(b) for b in found]

    def test_every_command_example_gets_the_documented_reply(self):
        blocks = self.json_blocks()
        pairs = [(blocks[i], blocks[i + 1]) for i in range(len(blocks) - 1)
                 if blocks[i].get("type") == "cmd" and blocks[i + 1].get("type") == "ack"]
        verbs = set()
        self.assertGreaterEqual(len(pairs), 6)
        for command, ack in pairs:
            settings = fresh_settings()
            settings["wifi_ssid"] = "HomeNet"
            rig = Rig(settings=settings, ip="192.168.1.50")
            got = rig.send(json.dumps(command))
            self.assertEqual(got, ack, command)
            verbs.add(command["data"]["verb"])
        self.assertEqual(verbs, {"get_info", "set_wifi", "forget_wifi", "set_role", "reboot"})

    def test_the_error_table_lists_every_code(self):
        text = self.text("docs", "usb-console.md")
        for code in CODES:
            self.assertIn("`{}`".format(code), text, code)

    def test_house_rules(self):
        text = self.text("docs", "usb-console.md")
        self.assertNotIn(chr(0x2014), text)
        text.encode("ascii")
        low = text.lower()
        for word in ("participant", "university", "in fab", "onedrive", "ai_team_state"):
            self.assertNotIn(word, low, word)
        self.assertNotIn(chr(0x2014), self.text("lib", "usb_console.py"))
        self.assertNotIn(chr(0x2014), self.text("tests", "test_usb_console.py"))

    def test_it_is_linked_from_the_other_documents(self):
        for parts in (("README.md",), ("BRINGUP.md",), ("docs", "device-reference.md"),
                      ("docs", "kit-prep.md")):
            self.assertIn("usb-console.md", self.text(*parts), parts)

    def test_card_and_reference_say_app_over_usb_and_keep_the_fallback(self):
        ref = self.text("docs", "device-reference.md")
        self.assertIn("## Setting up over the USB cable", ref)
        card = ref.split("## Quick card (print this section)")[1].split("\n---\n")[0]
        self.assertIn("over the USB cable", card)
        self.assertIn("(`/setup`)", card)
        self.assertIn("http://192.168.4.1", card)
        self.assertIn("fallback", card)
        self.assertIn("fallback", ref.split("## Setup mode and getting back to it")[1])


if __name__ == "__main__":
    unittest.main()
