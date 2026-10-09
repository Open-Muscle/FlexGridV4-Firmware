# lib/screens.py
# Text layouts for the 16-column SSD1306 OLED.
#
# The shipped panel is 128x32, which is 4 lines of 16 characters (8 px per
# character row). A 128x64 panel gives 8 lines. Every helper takes a `rows`
# argument (default 4) and returns at most that many lines, so the screens are
# written for the small panel first and get extra detail only when there is
# room.
#
# Pure functions, no hardware imports, so the wording and the line budget can
# be unit-tested on a desktop Python (see tests/). display_manager.text_screen
# truncates every line to 16 characters, so anything longer is silently lost;
# these helpers keep each line within COLS.

COLS = 16
DEFAULT_ROWS = 4     # 128x32 panel
MAX_LINES = 8        # 128x64 panel

# How long each frame of a rotating line stays up (see setup_screen_lines).
SETUP_FRAME_S = 3


def _fit(lines, rows):
    """Clip every line to COLS and the list to `rows` lines."""
    return [str(ln)[:COLS] for ln in lines[:max(1, rows)]]


# Why the last Wi-Fi join failed -> the OLED lines that explain it. The keys
# are the values stored in settings "last_wifi_error" (see
# network_manager.classify_join_status).
_JOIN_REASON_LINES = {
    "wrong_password": ("check password",),
    "no_ap_found":    ("network not", "found or 5 GHz"),
    "timeout":        ("join timed out",),
}


def reason_lines(reason):
    """OLED lines (each <= COLS) explaining a join failure reason."""
    return list(_JOIN_REASON_LINES.get(reason, ("join failed",)))


def split_ssid(ssid):
    """Break an SSID over as many lines as it needs so nothing is cut off.
    'OM-flexgrid-d7af0b' (18 chars) becomes ['OM-flexgrid-', 'd7af0b'];
    anything else that is too long is chunked every COLS characters."""
    ssid = str(ssid)
    if len(ssid) <= COLS:
        return [ssid]
    head, sep, tail = ssid.rpartition("-")
    if sep and head and len(head) + 1 <= COLS and 0 < len(tail) <= COLS:
        return [head + "-", tail]
    return [ssid[i:i + COLS] for i in range(0, len(ssid), COLS)]


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------

def boot_lines(device_id, ssid="", rows=DEFAULT_ROWS):
    """Shown while the band joins the saved Wi-Fi network."""
    lines = ["OpenMuscle V4", str(device_id), "Joining Wi-Fi..."]
    if ssid:
        lines.append(str(ssid))
    return _fit(lines, rows)


# ---------------------------------------------------------------------------
# Setup mode (the band is its own Wi-Fi network)
# ---------------------------------------------------------------------------

def setup_header_frames(last_error=""):
    """The lines that take turns in the top row of the setup screen on a
    4-line panel. The network name and password below it never move, so they
    are always readable; only this one row rotates."""
    frames = ["WIFI SETUP", "see guide"]
    if last_error:
        frames.append("LAST TRY FAILED")
        frames.extend(reason_lines(last_error))
    return frames


def setup_screen_lines(ap_ssid, psk, last_error="", last_ssid="",
                       rows=DEFAULT_ROWS, tick=0):
    """The WIFI SETUP screen shown while the band is in AP mode.

    The part a user needs to join the band's own network is the full network
    name (over two lines when it is longer than the screen) and the password
    line `PW <psk>`. On a 4-line panel that leaves one row, so the top row
    rotates every SETUP_FRAME_S seconds (pass an increasing `tick`) through
    WIFI SETUP, `see guide` and, after a failed join, the reason. With 8 lines
    there is room for everything at once: title, name, password, `see guide`,
    then the failure details.
    """
    creds = split_ssid(ap_ssid)
    creds.append("PW " + str(psk))
    if rows >= len(creds) + 2:
        lines = ["WIFI SETUP"] + creds + ["see guide"]
        if last_error:
            lines.append("LAST TRY FAILED")
            lines.extend(reason_lines(last_error))
            if last_ssid:
                lines.append(str(last_ssid))
        return _fit(lines, rows)
    frames = setup_header_frames(last_error)
    return _fit([frames[tick % len(frames)]] + creds, rows)


def join_failed_lines(reason, ssid="", rows=DEFAULT_ROWS):
    """Brief screen shown right before the band reboots into setup mode."""
    lines = ["WIFI FAILED"] + reason_lines(reason)
    if ssid and rows >= MAX_LINES:     # the tried network only fits on a tall panel
        lines.append(str(ssid))
    lines.append("back to setup")
    return _fit(lines, rows)


# ---------------------------------------------------------------------------
# Status pages (after the band has joined; MENU tap, or 15 s after joining)
# ---------------------------------------------------------------------------

# Power-mode word as shown on the status page. Matches the banners below.
_MODE_WORD = {
    "stream":  "STREAM",
    "standby": "PAUSED",
    "charge":  "CHARGE",
    "record":  "RECORD",
}


def battery_line(vbat, pct, low=False):
    """'BAT 87% 3.95V', or 'LOW 12% 3.42V' once the low-battery flag is up."""
    if vbat is None:
        return "BAT --"
    p = "--" if pct is None else "{}%".format(pct)
    return "{} {} {:.2f}V".format("LOW" if low else "BAT", p, vbat)


def status_pages(info, rows=DEFAULT_ROWS):
    """Pages for the on-demand status screen, as a list of line lists.

    `info` keys: wifi ("ok" | "lost" | "joining" | "off"), ip, device_id,
    role, vbat, pct, low (bool), mode (power mode), subs (hub count), ssid.

    4-line panel: page 0 is the network (WIFI OK, IP address, device id,
    role), page 1 is the device (battery, mode, hubs, network name). With 8
    lines both fit on one page.
    """
    wifi = info.get("wifi", "lost")
    role = info.get("role") or ""
    role_line = "role: " + role if role else "no role set"
    dev = str(info.get("device_id") or "")
    mode = info.get("mode", "stream")
    if wifi == "ok":
        net = ["WIFI OK", str(info.get("ip") or "no IP yet"), dev, role_line]
    elif wifi == "joining":
        net = ["JOINING WI-FI", str(info.get("ssid") or ""), dev, role_line]
    elif wifi == "off":
        net = ["WIFI OFF", _MODE_WORD.get(mode, mode) + " mode", dev, role_line]
    else:
        net = ["WIFI LOST", "retrying...", dev, role_line]
    dev_page = [
        battery_line(info.get("vbat"), info.get("pct"), info.get("low", False)),
        "MODE " + _MODE_WORD.get(mode, str(mode).upper()),
        "HUBS {}".format(info.get("subs", 0)),
        "NET " + str(info.get("ssid") or "-"),
    ]
    if rows >= len(net) + len(dev_page):
        return [_fit(net + dev_page, rows)]
    return [_fit(net, rows), _fit(dev_page, rows)]


def low_battery_lines(vbat, pct, rows=DEFAULT_ROWS):
    """Notice shown when the battery drops under the low-battery threshold."""
    return _fit(["LOW BATTERY", battery_line(vbat, pct, False), "charge soon"], rows)


# ---------------------------------------------------------------------------
# Power-mode banners
# ---------------------------------------------------------------------------

# Persistent banner for each non-streaming power mode. While one of these is
# active the live heatmap is replaced by this text, so the user can tell why
# the band stopped streaming and how to get it back. Four lines each, so the
# whole message fits the 128x32 panel.
_MODE_LINES = {
    "standby": ("PAUSED",
                "Wi-Fi is on",
                "not streaming",
                "SELECT to resume"),
    "charge":  ("CHARGING",
                "Wi-Fi off",
                "unplug to resume",
                "or tap SELECT"),
    "record":  ("RECORDING",
                "to SD, Wi-Fi OFF",
                "tap SELECT to",
                "stop & reconnect"),
}


def mode_lines(mode, rows=DEFAULT_ROWS):
    """Banner lines for a power mode, or None when the live heatmap should
    be shown instead (stream)."""
    lines = _MODE_LINES.get(mode)
    if lines is None:
        return None
    return _fit(lines, rows)
