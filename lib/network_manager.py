# lib/network_manager.py
# Wi-Fi setup + v1.0 packet construction + subscriber fan-out.
#
# Major differences from V3:
#   * No hardcoded destination IP. The V3 firmware sent every frame to a
#     single baked-in (host, port). V4 sends to every entry in the live
#     subscriber list, which is populated by hubs talking to commands.py.
#   * No "send if nobody's listening" no-op. We only allocate the JSON if
#     at least one subscriber is present. Saves CPU + heap when the band
#     is idle (no hub on the network).
#   * Wi-Fi reconnect handling carried forward from V3 v0.2.1 (force-recreate
#     the socket on link-up to defeat the silent-wedge bug).
#
# This module is transport-only; it does not know anything about the matrix
# itself, just receives the 2D array and wraps it in the v1.0 envelope.

import network
import socket
import asyncio
import ujson
import time
import logger


PROTOCOL_VERSION = "1.0"

# Default seconds to wait for the first STA join before giving up. Settings key
# "sta_join_timeout_s" overrides it; clamped so a typo cannot hang boot or
# make the fallback fire before the radio has had a fair try.
DEFAULT_JOIN_TIMEOUT_S = 20
MIN_JOIN_TIMEOUT_S = 5
MAX_JOIN_TIMEOUT_S = 60


# ---------------------------------------------------------------------------
# Join-result bookkeeping (PROVISIONING.md section 2: fall back to setup mode
# when the very first Wi-Fi join fails). Kept as plain functions over the
# settings dict so the rules are testable without a radio.
#
# Settings keys involved:
#   wifi_joined_ok   True once this band has joined its network at least once
#                    since the last /provision or Wi-Fi wipe. While False, a
#                    failed join sends the band back to setup mode; once True
#                    the creds are never discarded (the router may simply be
#                    off, and the radio keeps retrying on its own).
#   last_wifi_error  wrong_password | no_ap_found | timeout, or "" (shown on
#                    the OLED and reported by GET /info in setup mode).
#   last_ssid        the network that failed, kept for display / prefill.
# ---------------------------------------------------------------------------

def _stat(name, default):
    """network.STAT_* constant, tolerant of builds that lack one."""
    try:
        return getattr(network, name)
    except AttributeError:
        return default


def classify_join_status(code):
    """Map a network.WLAN.status() code to a failure reason, or None if the
    code is not a failure (idle, connecting, got-ip, unknown).

    A WPA2 handshake timeout is what a wrong password looks like on some
    access points, so it counts as wrong_password too."""
    if code is None:
        return None
    if code in (_stat("STAT_WRONG_PASSWORD", 202), _stat("STAT_HANDSHAKE_TIMEOUT", 204)):
        return "wrong_password"
    if code == _stat("STAT_NO_AP_FOUND", 201):
        return "no_ap_found"
    return None


def record_join_failure(settings, reason):
    """Apply a failed first join to the settings dict: remember why, keep the
    SSID for display, and clear the credentials so the next boot lands in AP
    (setup) mode. The caller saves and resets."""
    settings["last_wifi_error"] = reason or "timeout"
    settings["last_ssid"] = settings.get("wifi_ssid") or settings.get("last_ssid") or ""
    settings["wifi_ssid"] = ""
    settings["wifi_password"] = ""
    settings["wifi_joined_ok"] = False


def record_join_success(settings):
    """Mark the network as proven good. Returns True when something changed
    (so the caller only writes flash when it has to)."""
    changed = False
    for key, value in (("wifi_joined_ok", True), ("last_wifi_error", ""), ("last_ssid", "")):
        if settings.get(key) != value:
            settings[key] = value
            changed = True
    return changed


class NetworkManager:
    def __init__(self, settings, subscribers):
        self.settings = settings
        self.subscribers = subscribers

        self.device_id = settings["device_id"]
        self.device_type = settings["device_type"]
        # Not stripped: an SSID or password may legitimately start or end with
        # a space, and provisioning stores them exactly as typed.
        self.ssid = settings.get("wifi_ssid") or ""
        self.password = settings.get("wifi_password") or ""
        # Why the most recent connect() failed ("" when it did not).
        self.last_error = ""

        self.sta = network.WLAN(network.STA_IF)

        # One UDP send socket, reused for all subscribers. Recreated whenever
        # the Wi-Fi link comes back up (the V3 v0.2.1 wedge defense).
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self._was_connected = False

        # Sequence number for sensor frames. Hub uses it to detect drops on
        # lossy Wi-Fi. Wraps at 65535 (uint16); hubs handle the wrap.
        self._seq = 0

    async def connect(self):
        """Bring the STA interface up and join the configured AP. Returns True
        when joined, False when it did not join within sta_join_timeout_s
        (self.last_error then says why: wrong_password / no_ap_found /
        timeout). A False return is non-fatal here: the link layer keeps
        trying in the background and send loops pick up once it is up. The
        caller decides whether a failure on a never-joined network should
        send the band back to setup mode (see record_join_failure)."""
        self.last_error = ""
        if not self.ssid:
            logger.warn("No Wi-Fi SSID configured; skipping connection")
            return False

        if not self.sta.active():
            self.sta.active(True)

        if not self.sta.isconnected():
            timeout_s = self.settings.get("sta_join_timeout_s", DEFAULT_JOIN_TIMEOUT_S)
            try:
                timeout_s = int(timeout_s)
            except (TypeError, ValueError):
                timeout_s = DEFAULT_JOIN_TIMEOUT_S
            timeout_s = max(MIN_JOIN_TIMEOUT_S, min(MAX_JOIN_TIMEOUT_S, timeout_s))

            logger.info("Connecting to Wi-Fi SSID='{}'".format(self.ssid))
            self.sta.connect(self.ssid, self.password)
            # The driver retries on its own, so status() flips between a
            # failure code and "connecting"; remember the latest failure seen
            # rather than sampling once at the deadline.
            reason = None
            for _ in range(timeout_s):
                if self.sta.isconnected():
                    break
                try:
                    seen = classify_join_status(self.sta.status())
                except Exception:
                    seen = None
                if seen:
                    reason = seen
                await asyncio.sleep(1)
            if not self.sta.isconnected():
                self.last_error = reason or "timeout"
                logger.warn("Wi-Fi did not join within {}s ({})".format(
                    timeout_s, self.last_error))
                return False

        logger.info("Wi-Fi connected, IP: " + self.sta.ifconfig()[0])
        return True

    def rssi(self):
        try:
            return self.sta.status('rssi')
        except Exception:
            return None

    def is_connected(self):
        return self.sta.isconnected()

    def local_ip(self):
        """Returns the device's current IP, or None if not connected."""
        try:
            if self.sta.isconnected():
                return self.sta.ifconfig()[0]
        except Exception:
            pass
        return None

    def _ensure_socket_fresh_if_reconnected(self):
        """V3 v0.2.1 wedge defense: detect WiFi disconnect -> reconnect and
        force-recreate the UDP send socket. Cheap (one isconnected() + a bool
        compare per send). Returns False if WiFi is currently down (caller
        should skip the send), True otherwise."""
        connected = self.sta.isconnected()
        if not connected:
            self._was_connected = False
            return False
        if not self._was_connected:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setblocking(False)
            self._was_connected = True
            logger.info("Wi-Fi connected; UDP socket (re)created -> " + self.sta.ifconfig()[0])
        return True

    def _build_sensor_packet(self, matrix, meta=None, imu=None):
        """Wrap a 2D sensor matrix in the v1.0 envelope. Adds the rolling
        seq number so the hub can detect dropped frames. Optional `imu`
        (a dict carrying accel + gyro keys) rides in `data.imu` so hubs
        get high-rate IMU on the sensor channel; see PROTOCOL.md 7.1."""
        self._seq = (self._seq + 1) & 0xFFFF
        data = {"matrix": matrix}
        if imu is not None:
            data["imu"] = imu
        pkt = {
            "v":    PROTOCOL_VERSION,
            "type": self.device_type,
            "id":   self.device_id,
            "ts":   time.ticks_ms(),
            "seq":  self._seq,
            "data": data,
        }
        if meta:
            pkt["meta"] = meta
        return pkt

    def _build_status_packet(self, status):
        """Standalone status packet (no matrix). Used by the status loop."""
        return {
            "v":    PROTOCOL_VERSION,
            "type": self.device_type,
            "id":   self.device_id,
            "ts":   time.ticks_ms(),
            "data": {},
            "meta": status,
        }

    async def send_sensor(self, matrix, meta=None, imu=None):
        """Send a sensor frame to every Wi-Fi subscriber.

        If no subscribers are registered we skip the JSON encode entirely.
        This matters: an idle band can be scanning at 50 Hz indefinitely
        and we do not want to spend CPU on packet construction with nobody
        to receive it.

        `imu`, when given, rides in `data.imu` per PROTOCOL.md 7.1 so hubs
        receive high-rate IMU samples without parsing the slower status
        meta path.
        """
        targets = self.subscribers.wifi_targets()
        if not targets:
            return
        if not self._ensure_socket_fresh_if_reconnected():
            return
        pkt = self._build_sensor_packet(matrix, meta=meta, imu=imu)
        payload = ujson.dumps(pkt).encode("utf-8")
        for host, port in targets:
            try:
                self.sock.sendto(payload, (host, port))
            except OSError as e:
                errno = getattr(e, "errno", None) or (e.args[0] if e.args else None)
                # ENOMEM (12) and EAGAIN (11) are non-fatal; next iter retries.
                if errno not in (11, 12):
                    logger.warn("UDP send to {}:{} failed errno={} {}".format(host, port, errno, e))
                    # Force socket recreate next iter just in case.
                    self._was_connected = False
            except Exception as e:
                logger.warn("UDP send to {}:{} unexpected: {}".format(host, port, e))
                self._was_connected = False

    async def send_status(self, status):
        """Send a status packet to every Wi-Fi subscriber. ~1 Hz."""
        targets = self.subscribers.wifi_targets()
        if not targets:
            return
        if not self._ensure_socket_fresh_if_reconnected():
            return
        pkt = self._build_status_packet(status)
        payload = ujson.dumps(pkt).encode("utf-8")
        for host, port in targets:
            try:
                self.sock.sendto(payload, (host, port))
            except Exception as e:
                logger.warn("UDP status to {}:{} failed: {}".format(host, port, e))
