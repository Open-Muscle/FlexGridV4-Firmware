"""The user-facing documents (README.md, BRINGUP.md, docs/device-reference.md,
docs/kit-prep.md) must keep matching the firmware and tools they describe.

These tests read the documents as text and compare them with the code: the LED
palette, the screen wording, the button timings, the defaults, the command-line
options, the sticker text and the links. They also guard the house rules for
published files (no long dashes, nobody named, no leftover "in fab" status).
"""
import inspect
import os
import re
import sys
import unittest

import mp_stubs

import flash
import flexgrid
import provisioning
import screens
import status_led
from settings_manager import SettingsManager

ROOT = mp_stubs.ROOT
sys.path.insert(0, os.path.join(ROOT, "tools"))
import kit_check  # noqa: E402

DOCS = {
    "README.md": os.path.join(ROOT, "README.md"),
    "BRINGUP.md": os.path.join(ROOT, "BRINGUP.md"),
    "device-reference": os.path.join(ROOT, "docs", "device-reference.md"),
    "kit-prep": os.path.join(ROOT, "docs", "kit-prep.md"),
}
EM_DASH = chr(0x2014)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def doc(name):
    return read(DOCS[name])


def section(text, heading_start):
    """The text from a '## ' heading that starts with `heading_start` up to the
    next '---' rule or '## ' heading."""
    m = re.search(r"^## " + re.escape(heading_start) + r".*?$", text, re.M)
    assert m, heading_start
    rest = text[m.end():]
    end = re.search(r"^(---|## )", rest, re.M)
    return rest[:end.start()] if end else rest


def slug(heading):
    s = heading.strip().lower()
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", "-")


def anchors(text):
    return {slug(m.group(1)) for m in re.finditer(r"^#{1,6}\s+(.*?)\s*$", text, re.M)}


class TestHouseRules(unittest.TestCase):
    def tracked_text_files(self):
        skip_dirs = {".git", "firmware", "__pycache__"}
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for fn in filenames:
                if fn.endswith((".md", ".py", ".json", ".txt")):
                    yield os.path.join(dirpath, fn)

    def test_no_long_dashes_anywhere(self):
        for path in self.tracked_text_files():
            self.assertNotIn(EM_DASH, read(path), path)

    def test_documents_exist(self):
        for name, path in DOCS.items():
            self.assertTrue(os.path.isfile(path), name)

    def test_nobody_is_named_and_no_internal_paths(self):
        banned = ("participant", "university", "in fab")
        patterns = (r"[A-Za-z]:\\Users", r"\\Users\\", "One" + "Drive", "AI_" + "Team_State")
        for name in DOCS:
            text = doc(name)
            for word in banned:
                self.assertNotIn(word, text.lower(), "{}: {}".format(name, word))
            for pat in patterns:
                self.assertIsNone(re.search(pat, text, re.I), "{}: {}".format(name, pat))

    def test_no_stale_status_claims(self):
        stale = ("in fab", "not yet brought up", "BTN_BOOT", "esptool 5.x",
                 "Hold the BOOT button", "DFU dance", "ble.py")
        for name in DOCS:
            text = doc(name)
            for phrase in stale:
                self.assertNotIn(phrase.lower(), text.lower(), "{}: {}".format(name, phrase))

    def test_doc_text_is_plain_ascii_apart_from_tree_art(self):
        # Box-drawing characters appear only in the README repo layout.
        for name in ("BRINGUP.md", "device-reference", "kit-prep"):
            for ch in doc(name):
                self.assertLess(ord(ch), 128, "{}: {!r}".format(name, ch))


class TestLinks(unittest.TestCase):
    def test_relative_links_and_anchors_resolve(self):
        link = re.compile(r"\]\(([^)#\s]*)(#[^)\s]*)?\)")
        for name, path in DOCS.items():
            text = read(path)
            base = os.path.dirname(path)
            for m in link.finditer(text):
                target, frag = m.group(1), m.group(2)
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                dest = os.path.normpath(os.path.join(base, target)) if target else path
                self.assertTrue(os.path.exists(dest), "{}: {}".format(name, target))
                if frag and dest.endswith(".md"):
                    self.assertIn(frag[1:], anchors(read(dest)), "{}: {}{}".format(name, target, frag))

    def test_entry_points_link_to_the_references(self):
        for name in ("README.md", "BRINGUP.md"):
            text = doc(name)
            self.assertIn("docs/device-reference.md", text, name)
            self.assertIn("docs/kit-prep.md", text, name)
        self.assertIn("device-reference.md", doc("kit-prep"))
        self.assertIn("BRINGUP.md", doc("kit-prep"))

    def test_quick_card_is_linked_from_the_user_sections(self):
        self.assertIn("#quick-card-print-this-section", doc("README.md"))


class TestBringup(unittest.TestCase):
    def test_announcement_check_binds_the_announce_port(self):
        text = doc("BRINGUP.md")
        self.assertIn("bind(('', 3140))", text)
        self.assertNotIn("bind(('', 3141))", text)
        self.assertEqual(SettingsManager.DEFAULTS["udp_announce_port"], 3140)

    def test_factory_flash_needs_no_credentials(self):
        text = doc("BRINGUP.md")
        self.assertIn("python flash.py --factory", text)
        factory = text.split("## Flash a band (the normal way)")[1].split("### Options")[0]
        self.assertIn("pushes no `config/settings.json`", factory)
        self.assertNotIn("cp config/settings.example.json", factory)
        # the credentials route is labelled as a developer shortcut
        self.assertIn("## Developer shortcut", text)

    def test_tool_versions_match_flash_py(self):
        versions = re.search(r"esptool (\d+\.\d+\.\d+) and mpremote (\d+\.\d+\.\d+)",
                             inspect.getdoc(flash))
        self.assertTrue(versions)
        text = doc("BRINGUP.md") + doc("README.md") + doc("kit-prep")
        self.assertIn('esptool=={}'.format(versions.group(1)), text)
        self.assertIn('mpremote=={}'.format(versions.group(2)), text)

    def test_manual_route_uses_the_same_esptool_commands_as_flash_py(self):
        text = doc("BRINGUP.md")
        src = inspect.getsource(flash.main)
        for cmd in ("erase_flash", "write_flash", "usb_reset"):
            self.assertIn(cmd, src)
            self.assertIn(cmd, text)
        self.assertNotIn("erase-flash", text)

    def test_boot_button_is_described_as_reset(self):
        text = doc("BRINGUP.md")
        self.assertIn("reset", text.split("## Download mode")[1].split("## Troubleshooting")[0])
        from_pinmap = read(os.path.join(ROOT, "lib", "pinmap.py"))
        self.assertIn("EN", from_pinmap)

    def test_firmware_files_pushed_are_present(self):
        for local, _remote in flash.firmware_files(True):
            self.assertTrue(os.path.isfile(os.path.join(ROOT, local)), local)


class TestCommandLines(unittest.TestCase):
    """Every option shown next to a tool in the docs is one the tool accepts."""

    def lines_with(self, tool):
        """The command text that follows `tool` on each line that mentions it
        as a command (`python <tool> ...`)."""
        pattern = re.compile(r"python " + re.escape(tool) + r"[^`\n]*")
        for name in DOCS:
            for line in doc(name).splitlines():
                m = pattern.search(line)
                if m:
                    yield name, m.group(0)

    def test_kit_check_options(self):
        known = set(re.findall(r"--[a-z][a-z-]*", inspect.getdoc(kit_check)))
        seen = set()
        for name, line in self.lines_with("tools/kit_check.py"):
            for flag in re.findall(r"--[a-z][a-z-]*", line):
                self.assertIn(flag, known, "{}: {}".format(name, line))
                seen.add(flag)
        self.assertTrue({"--role", "--label", "--clear-logs"} <= seen)

    def test_flash_and_wipe_options(self):
        for name, line in self.lines_with("flash.py"):
            for flag in re.findall(r"--[a-z][a-z-]*", line):
                self.assertEqual(flag, "--factory", "{}: {}".format(name, line))
        for name, line in self.lines_with("wipe.py"):
            for flag in re.findall(r"--[a-z][a-z-]*", line):
                self.assertEqual(flag, "--reset", "{}: {}".format(name, line))

    def test_kit_prep_example_command_parses(self):
        text = doc("kit-prep")
        m = re.search(r"python tools/kit_check\.py (COM8 .*)", text)
        self.assertTrue(m)
        import shlex
        opts = kit_check.parse_args(shlex.split(m.group(1)))
        self.assertEqual(opts["role"], "left")
        self.assertTrue(opts["clear_logs"] and opts["reset"])

    def test_kit_check_floor_is_documented(self):
        self.assertEqual(kit_check.DEFAULT_MIN_BATTERY_V, 3.7)
        for name in ("README.md", "kit-prep"):
            self.assertIn("3.7 V", doc(name), name)


class TestKitPrep(unittest.TestCase):
    def test_sticker_example_matches_kit_check(self):
        text = doc("kit-prep")
        block = text.split("It looks like this (example values):")[1].split("```")[1]
        shown = [ln.strip() for ln in block.strip().splitlines()]
        report = {"device_id": "flexgrid-d7af0b", "device_type": "flexgrid", "role": "left",
                  "label": "Band A", "psk": "kd3mpq72xy"}
        want = [ln.strip() for ln in kit_check.sticker_lines(report)]
        self.assertEqual(shown, want)

    def test_has_a_real_checklist(self):
        text = doc("kit-prep")
        self.assertGreaterEqual(len(re.findall(r"^\s*- \[ \]", text, re.M)), 25)

    def test_covers_the_required_steps_in_order(self):
        text = doc("kit-prep")
        order = ["### A. Pick the release", "### B. Flash the band", "### C. First look",
                 "### D. Hardware and reset-path tests", "### E. Roles, clean-up",
                 "### F. Stickers and photos", "### G. Charge level and transport",
                 "### H. Box contents", "### I. Record keeping"]
        pos = [text.index(h) for h in order]
        self.assertEqual(pos, sorted(pos))
        for word in ("--factory", "kit_check", "MENU and SELECT together", "after the final flash",
                     "check the carrier's rules", "left", "right", "quick card", "USB-C"):
            self.assertIn(word, text, word)

    def test_stickers_come_after_the_last_flash_and_wipe(self):
        text = doc("kit-prep")
        self.assertLess(text.index("python flash.py --factory"), text.index("### F. Stickers"))
        self.assertLess(text.index("python tools/kit_check.py"), text.index("### F. Stickers"))
        self.assertIn("new id and a new setup password", text)

    def test_reset_keeps_the_setup_password(self):
        # What step D relies on: the MENU + SELECT wipe leaves the setup password
        # and id alone, so the sticker stays valid.
        src = inspect.getsource(flexgrid.power_mode_loop)
        wipe = src.split("def _wipe_and_reprovision")[1].split("def _enter_record")[0]
        self.assertNotIn("provisioning_psk", wipe)
        self.assertNotIn("device_id", wipe)
        self.assertIn("wifi_joined_ok", wipe)

    def test_every_failure_message_quoted_exists(self):
        src = inspect.getsource(kit_check)
        for quoted in ("not in factory state", "not found at 0x", "charge it before shipping",
                       "no battery detected", "differs from this checkout",
                       "no readable settings file", "not on a git tag"):
            self.assertIn(quoted, src, quoted)
        text = doc("kit-prep")
        for quoted in ("not in factory state", "OLED not found at 0x3C", "no battery detected",
                       "differs from this checkout", "no readable settings file"):
            self.assertIn(quoted, text, quoted)

    def test_names_no_site_or_person(self):
        text = doc("kit-prep").lower()
        for word in ("clinic", "hospital", "patient", "study", "recipient's name"):
            self.assertNotIn(word, text, word)


class TestReferenceMatchesFirmware(unittest.TestCase):
    def setUp(self):
        self.ref = doc("device-reference")

    def test_every_led_state_has_a_row_with_the_right_colour(self):
        words = {
            (0, 0, 255): ("blue",), (0, 0, 200): ("blue",), (200, 0, 0): ("red",),
            (255, 0, 0): ("red",), (0, 200, 0): ("green",), (0, 140, 200): ("cyan",),
            (200, 80, 0): ("amber", "orange"), (100, 100, 100): ("white",),
            (120, 0, 180): ("purple",), (200, 0, 200): ("magenta",),
        }
        legacy = {"idle", "error", "calibrating"}
        rows = {}
        for line in self.ref.splitlines():
            m = re.match(r"\|(.*)\|\s*`([a-z_]+)`\s*\|\s*$", line)
            if m:
                rows[m.group(2)] = m.group(1).lower()
        for name, (r, g, b, mode) in status_led.PALETTE_DEFAULT.items():
            if name in legacy:
                continue
            self.assertIn(name, rows, name)
            self.assertTrue(any(w in rows[name] for w in words[(r, g, b)]),
                            "{}: {}".format(name, rows[name]))

    def test_led_patterns_are_described(self):
        rows = {}
        for line in self.ref.splitlines():
            m = re.match(r"\|(.*)\|\s*`([a-z_]+)`\s*\|\s*$", line)
            if m:
                rows[m.group(2)] = m.group(1).lower()
        pattern_words = {
            "blink_slow": "one blink per second", "blink_double": "two quick blinks",
            "blink_fast": "fast blink", "solid": "steady", "pulse_3s": "every 3 s",
            "stream_usb": "cyan flash every 5 s", "breathe_slow": "slow breathing",
            "breathe": "breathing", "pulse_1500": "pulse",
        }
        for name, (_r, _g, _b, mode) in status_led.PALETTE_DEFAULT.items():
            if name in ("idle", "error", "calibrating", "ap_provisioning", "low_battery"):
                continue
            self.assertIn(pattern_words[mode], rows[name], "{} ({})".format(name, mode))
        # the two blue-and-amber 1 Hz states are the "blink_slow" ones
        self.assertIn("blinking", rows["ap_provisioning"])
        self.assertIn("one blink per second", rows["low_battery"])

    def test_led_priority_order_matches_the_resolver(self):
        order = ["crash recovery", "button preview", "join failed", "just joined", "joining",
                 "recording", "paused", "charge mode", "low battery", "Wi-Fi lost", "streaming",
                 "waiting"]
        block = self.ref.split("### Which one shows when several are true")[1].split("Setup mode (blue blink)")[0]
        items = re.findall(r"^\d+\.\s+(.*)$", block, re.M)
        self.assertEqual(len(items), 12)
        src = inspect.getsource(flexgrid._resolve_led_state)
        pos = [src.index(k) for k in ('"fault"', 'hold_preview', '"join_failed"', '"joined"',
                                      '"joining"', 'mode == "record"', 'mode == "standby"',
                                      'mode == "charge"', '"low_battery"', '"wifi_lost"',
                                      'has_active_subs', 'idle_left')]
        self.assertEqual(pos, sorted(pos))
        self.assertEqual(len(order), len(items))

    def test_mode_banners_are_quoted_exactly(self):
        for mode in ("standby", "charge", "record"):
            for line in screens.mode_lines(mode):
                self.assertIn(line, self.ref, "{}: {}".format(mode, line))

    def test_setup_and_failure_wording(self):
        for frame in screens.setup_header_frames("wrong_password"):
            self.assertIn(frame, self.ref, frame)
        for reason in ("wrong_password", "no_ap_found", "timeout"):
            self.assertIn(" ".join(screens.reason_lines(reason)), self.ref, reason)
        for line in screens.join_failed_lines("timeout"):
            self.assertIn(line, self.ref)
        self.assertEqual(screens.boot_lines("flexgrid-d7af0b", "Net")[0], "OpenMuscle V4")
        self.assertIn("OpenMuscle V4", self.ref)
        self.assertIn("Joining Wi-Fi...", self.ref)

    def test_status_page_wording(self):
        info = {"wifi": "ok", "ip": "10.0.0.5", "device_id": "flexgrid-d7af0b", "role": "left",
                "vbat": 3.95, "pct": 78, "low": False, "mode": "stream", "subs": 1, "ssid": "Net"}
        for page in screens.status_pages(info):
            for line in page:
                token = line.split(" ")[0]
                if token in ("WIFI", "BAT", "MODE", "HUBS", "NET", "role:"):
                    self.assertIn(token, self.ref, line)
        for wifi, want in (("joining", "JOINING WI-FI"), ("lost", "WIFI LOST"), ("off", "WIFI OFF")):
            info["wifi"] = wifi
            self.assertEqual(screens.status_pages(info)[0][0], want)
            self.assertIn(want, self.ref, want)
        self.assertIn("retrying...", self.ref)
        self.assertIn("no role set", self.ref)

    def test_notices_exist_in_the_firmware_and_the_reference(self):
        src = inspect.getsource(flexgrid)
        for lines in (["NO SD CARD", "Record needs one", "Wi-Fi stays on"],
                      ["RECORD OFF", "disabled in", "settings"]):
            for line in lines:
                self.assertIn('"{}"'.format(line), src, line)
                self.assertIn(line, self.ref, line)
        self.assertEqual(screens.low_battery_lines(3.42, 12)[0], "LOW BATTERY")
        self.assertIn("LOW BATTERY", self.ref)
        self.assertIn("charge soon", self.ref)

    def test_button_timings(self):
        sig = inspect.signature(flexgrid.power_mode_loop)
        self.assertEqual(sig.parameters["charge_hold_s"].default, 2)
        src = inspect.getsource(flexgrid.power_mode_loop)
        self.assertIn("(3 * 1000) // tick_ms", src)
        for text in ("Hold MENU 2 s", "Hold SELECT 2 s", "Hold MENU and SELECT 3 s", "Tap MENU", "Tap SELECT"):
            self.assertIn(text, self.ref, text)
            self.assertIn(text, doc("README.md"), text)

    def test_defaults_quoted_in_the_reference_and_readme(self):
        d = SettingsManager.DEFAULTS
        self.assertEqual(d["auto_charge_idle_s"], 300)
        self.assertEqual(d["low_battery_v"], 3.55)
        self.assertEqual(d["sta_join_timeout_s"], 20)
        self.assertEqual(d["display_height"], 32)
        self.assertTrue(d["record_enabled"])
        self.assertEqual(d["scan_interval_ms"], 20)
        self.assertIn("(default 300)", self.ref)
        self.assertIn("3.55 V", self.ref)
        self.assertIn("20 s", self.ref)
        for key in ("auto_charge_idle_s", "low_battery_v", "record_enabled", "display_height",
                    "sta_join_timeout_s", "vbus_stat_pin", "device_label"):
            self.assertIn("`{}`".format(key), doc("README.md"), key)
            self.assertIn(key, d)

    def test_battery_numbers(self):
        from power_manager import PowerManager
        self.assertEqual(PowerManager.LIPO_EMPTY, 3.50)
        self.assertEqual(PowerManager.LIPO_FULL, 4.20)
        self.assertEqual(PowerManager.VBUS_PRESENT_ENTER_V, 4.05)
        self.assertEqual(PowerManager.VBUS_PRESENT_EXIT_V, 3.95)
        self.assertEqual(PowerManager.BATTERY_FULL_V, 4.18)
        self.assertEqual(flexgrid.LOW_BATTERY_CLEAR_V, 0.10)
        for text in ("3.50 V", "4.20 V", "4.05 V", "3.95 V", "4.18 V", "3.65 V"):
            self.assertIn(text, self.ref, text)

    def test_status_and_timing_numbers(self):
        self.assertEqual(flexgrid.STATUS_HOLD_MS, 15000)
        self.assertEqual(flexgrid.JOINED_LED_MS, 2000)
        self.assertEqual(flexgrid.LOW_BATTERY_NOTICE_EVERY_MS, 300000)
        self.assertEqual(screens.SETUP_FRAME_S, 3)
        for text in ("15 s", "2 s", "every 3 s", "every 5 minutes"):
            self.assertIn(text, self.ref, text)

    def test_setup_network_facts(self):
        self.assertEqual(provisioning.AP_IP, "192.168.4.1")
        self.assertEqual(provisioning.AP_CHANNEL, 1)
        self.assertEqual(provisioning.AP_MAX_CLIENTS, 4)
        self.assertEqual(provisioning.PSK_LENGTH, 10)
        self.assertEqual(provisioning.ssid_for("flexgrid", "flexgrid-d7af0b"), "OM-flexgrid-d7af0b")
        self.assertIn("`{}`".format(provisioning.PSK_CHARSET), self.ref)
        self.assertIn("http://192.168.4.1", self.ref)
        self.assertIn("http://192.168.4.1", doc("README.md"))

    def test_readme_setup_screen_matches_the_firmware(self):
        lines = screens.setup_screen_lines("OM-flexgrid-d7af0b", "kd3mpq72xy", tick=0)
        self.assertEqual(lines, ["WIFI SETUP", "OM-flexgrid-", "d7af0b", "PW kd3mpq72xy"])
        readme = doc("README.md")
        for line in lines[1:]:
            self.assertIn(line, readme)
        self.assertIn(lines[0], readme)
        bring = doc("BRINGUP.md")
        for line in lines:
            self.assertIn(line, bring)

    def test_every_open_hardware_question_is_listed(self):
        block = self.ref.split("## Facts still to confirm on a physical band")[1].split("## Where each fact")[0]
        items = re.findall(r"^\d+\.\s+\*\*(.*?)\*\*", block, re.M)
        for needed in ("Power button.", "Switches fitted.", "Screen.", "CHRG light.", "Charging.",
                       "SD socket."):
            self.assertIn(needed, items, needed)


class TestQuickCard(unittest.TestCase):
    def card(self):
        text = doc("device-reference")
        return text.split("## Quick card (print this section)")[1].split("\n---\n")[0]

    def test_fits_one_page(self):
        card = self.card()
        lines = [ln for ln in card.splitlines() if ln.strip()]
        self.assertLessEqual(len(lines), 62, len(lines))
        self.assertLessEqual(len(card), 5200, len(card))

    def test_has_every_part_the_brief_asks_for(self):
        card = self.card()
        for part in ("Switch on and charge", "WIFI SETUP", "Buttons", "Lights", "If it will not connect",
                     "CHRG", "Hold MENU and SELECT 3 s", "BOOT", "2.4 GHz", "http://192.168.4.1"):
            self.assertIn(part, card, part)

    def test_setup_light_is_the_blue_blink_not_magenta(self):
        card = self.card()
        row = [ln for ln in card.splitlines() if ln.startswith("| Blue, blinking once a second")]
        self.assertEqual(len(row), 1)
        self.assertIn("Setup", row[0])
        magenta = [ln for ln in card.splitlines() if "agenta" in ln]
        self.assertEqual(len(magenta), 1)
        self.assertIn("Both buttons held", magenta[0])

    def test_card_buttons_match_the_firmware_actions(self):
        card = self.card()
        for text in ("Tap MENU", "Tap SELECT", "Hold MENU 2 s", "Hold SELECT 2 s",
                     "Hold MENU and SELECT 3 s", "Restart the band"):
            self.assertIn(text, card, text)


if __name__ == "__main__":
    unittest.main()
