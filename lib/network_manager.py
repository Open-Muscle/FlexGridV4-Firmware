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


class NetworkManager:
    def __init__(self, settings, subscribers):
        self.settings = settings
        self.subscribers = subscribers

        self.device_id = settings["device_id"]
        self.device_type = settings["device_type"]
        self.ssid = settings.get("wifi_ssid", "").strip()
        self.password = settings.get("wifi_password", "").strip()

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
        """Bring the STA interface up and join the configured AP. Non-fatal
        if it does not join within the timeout; the link layer will keep
        trying in the background and send loops will pick up once it's up."""
        if not self.ssid:
            logger.warn("No Wi-Fi SSID configured; skipping connection")
            return

        if not self.sta.active():
            self.sta.active(True)

        if not self.sta.isconnected():
            logger.info("Connecting to Wi-Fi SSID='{}'".format(self.ssid))
            self.sta.connect(self.ssid, self.password)
            for _ in range(20):
                if self.sta.isconnected():
                    break
                await asyncio.sleep(1)
            if not self.sta.isconnected():
                logger.warn("Wi-Fi did not join within 20s; will keep trying")
                return

        logger.info("Wi-Fi connected, IP: " + self.sta.ifconfig()[0])

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

    def _build_sensor_packet(self, matrix, meta=None):
        """Wrap a 2D sensor matrix in the v1.0 envelope. Adds the rolling
        seq number so the hub can detect dropped frames."""
        self._seq = (self._seq + 1) & 0xFFFF
        pkt = {
            "v":    PROTOCOL_VERSION,
            "type": self.device_type,
            "id":   self.device_id,
            "ts":   time.ticks_ms(),
            "seq":  self._seq,
            "data": {"matrix": matrix},
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

    async def send_sensor(self, matrix, meta=None):
        """Send a sensor frame to every Wi-Fi subscriber.

        If no subscribers are registered we skip the JSON encode entirely.
        This matters: an idle band can be scanning at 50 Hz indefinitely
        and we do not want to spend CPU on packet construction with nobody
        to receive it.
        """
        targets = self.subscribers.wifi_targets()
        if not targets:
            return
        if not self._ensure_socket_fresh_if_reconnected():
            return
        pkt = self._build_sensor_packet(matrix, meta=meta)
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
