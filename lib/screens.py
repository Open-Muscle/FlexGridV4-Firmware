# lib/screens.py
# Text layouts for the 16-column SSD1306 OLED (8 lines on the 128x64 panel).
#
# Pure functions, no hardware imports, so the wording and the line budget can
# be unit-tested on a desktop Python (see tests/). display_manager.text_screen
# truncates every line to 16 characters, so anything longer is silently lost;
# these helpers keep each line within COLS.

COLS = 16
MAX_LINES = 8


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


def setup_screen_lines(ap_ssid, psk, last_error="", last_ssid=""):
    """The WIFI SETUP screen shown while the band is in AP mode.

    First four lines are the part a user needs to join the band's own
    network (title, full AP name over two lines, password), so even a
    32 px (4 line) panel shows them. When the previous attempt to join a
    home network failed, the reason and the network that was tried follow.
    """
    lines = ["WIFI SETUP"]
    lines.extend(split_ssid(ap_ssid))
    lines.append("PSK " + str(psk))
    if last_error:
        lines.append("LAST TRY FAILED")
        lines.extend(reason_lines(last_error))
        if last_ssid:
            lines.append(str(last_ssid))
    return [ln[:COLS] for ln in lines[:MAX_LINES]]


def join_failed_lines(reason, ssid=""):
    """Brief screen shown right before the band reboots into setup mode."""
    lines = ["WIFI FAILED"]
    lines.extend(reason_lines(reason))
    if ssid:
        lines.append(str(ssid))
    lines.append("going back to")
    lines.append("WIFI SETUP")
    return [ln[:COLS] for ln in lines[:MAX_LINES]]


# Persistent banner for each non-streaming power mode. While one of these is
# active the live heatmap is replaced by this text, so the user can tell why
# the band stopped streaming and how to get it back.
_MODE_LINES = {
    "standby": ("PAUSED",
                "Wi-Fi is on",
                "Not streaming",
                "",
                "Tap SELECT to",
                "resume"),
    "charge":  ("CHARGE MODE",
                "Wi-Fi is OFF",
                "Not streaming",
                "",
                "Tap SELECT or",
                "hold MENU 2s to",
                "reboot and",
                "reconnect"),
    "record":  ("RECORDING",
                "to SD card",
                "Wi-Fi is OFF",
                "",
                "Tap SELECT to",
                "stop, reboot and",
                "reconnect"),
}


def mode_lines(mode):
    """Banner lines for a power mode, or None when the live heatmap should
    be shown instead (stream)."""
    lines = _MODE_LINES.get(mode)
    if lines is None:
        return None
    return list(lines)
