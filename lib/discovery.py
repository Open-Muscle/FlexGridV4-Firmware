# lib/discovery.py
# Device discovery.
#
# Two parallel mechanisms per spec section 4:
#   1. mDNS service registration (`_openmuscle._udp.local`). Hubs that scan
#      via mDNS find us in their normal way. Some MicroPython builds expose
#      this via the `network` interface's hostname + `mdns_service`; others
#      do not. We use a best-effort wrapper that no-ops gracefully when the
#      underlying API is absent.
#   2. UDP broadcast beacon to 255.255.255.255:3140. This is the fallback
#      for networks that block multicast (corporate Wi-Fi, captive portals,
#      consumer routers with multicast disabled). The beacon carries the
#      full announce JSON as the packet payload.
#
# Port: 3140 in PROTOCOL.md v1.0 (announce strictly separated from data on
# 3141). Pre-spec firmware broadcast on 3141; hubs dual-listen on both
# ports during the migration window per PROTOCOL.md section 9.3.
#
# Beacon cadence: ~1 Hz while no hub is subscribed. Once at least one hub
# has subscribed we stop broadcasting to keep the channel quiet, and resume
# the moment subscribers.has_any() goes back to False.

import asyncio
import socket
import ujson
import time
import logger


BROADCAST_PORT_DEFAULT = 3140


class Discovery:
    def __init__(self, settings, subscribers, sta, services, caps, matrix_dims):
        """
        settings: the loaded settings dict (gives device_id, mdns_service, etc.)
        subscribers: lib.subscribers.Subscribers instance
        sta: network.WLAN(STA_IF), used to read the current IP for mDNS
        services: dict mapping capability name -> port, e.g. {"sensor": 3141, "cmd": 8001}
        caps: list of capability strings advertised in the announce
        matrix_dims: tuple (cols, rows) for the sensor matrix
        """
        self.settings = settings
        self.subscribers = subscribers
        self.sta = sta
        self.services = services
        self.caps = caps
        self.matrix_dims = matrix_dims

        self.device_id = settings["device_id"]
        self.device_type = settings["device_type"]
        self.fw_version = settings.get("fw_version", "v4.0.0")
        self.service_type = settings.get("mdns_service", "_openmuscle._udp")
        # Announce port is separate from the data port in v1.0 (PROTOCOL.md
        # section 2): 3140 for announce broadcast, 3141 for data unicast.
        # Old settings.json files may not have udp_announce_port; the
        # BROADCAST_PORT_DEFAULT fallback (now 3140) covers that.
        self.beacon_port = settings.get("udp_announce_port", BROADCAST_PORT_DEFAULT)
        self.announce_interval_s = settings.get("announce_interval_s", 1)

        # Sender socket for the broadcast beacon. Created once, reused.
        self._beacon_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._beacon_sock.setblocking(False)
        try:
            self._beacon_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except Exception as e:
            logger.warn("SO_BROADCAST setsockopt failed: {}".format(e))

        # mDNS state. Whether we registered, and how, is best-effort; the
        # broadcast beacon is the reliable path.
        self._mdns_registered = False

    def _announce_payload(self):
        """Build the announce JSON. The IP is NOT included here: the hub takes
        it from the packet source address (UDP broadcast) or from the mDNS A
        record. This is intentional per spec section 4.1: 'The IP is never in
        the announce; the listener takes it from the packet/mDNS source'."""
        return {
            "v":         "1.0",
            "type":      "announce",
            "id":        self.device_id,
            "role":      "source",
            "dev":       self.device_type,
            "fw":        self.fw_version,
            "transports": ["wifi"],  # phase 4 appends "ble"
            "caps":      list(self.caps),
            "matrix":    list(self.matrix_dims),
            "services":  dict(self.services),
            "ts":        time.ticks_ms(),
        }

    def register_mdns(self):
        """Best-effort mDNS registration. Tries the network.mDNS / mdns_service
        paths some MicroPython builds expose. On builds without an mDNS API
        this no-ops and we rely entirely on the broadcast beacon.

        We do NOT raise on failure; the broadcast beacon makes mDNS optional.
        """
        if self._mdns_registered:
            return

        try:
            # Some MicroPython builds expose `network.WLAN.config('hostname', ...)`
            # which underneath registers an mDNS hostname record. Set our id as
            # the hostname so `flexgrid-<6hex>.local` resolves on the LAN.
            self.sta.config(hostname=self.device_id)
        except Exception as e:
            logger.warn("mDNS hostname set failed (ok, beacon will cover it): {}".format(e))

        # If the build has the `mdns_service` C module, we would call it here
        # to register `_openmuscle._udp.local` with a TXT record. That module
        # is not in stock MicroPython; we leave a hook here for users on a
        # custom firmware build to wire it up.
        # try:
        #     import mdns_service
        #     txt = {k: str(v) for k, v in self._announce_txt_record().items()}
        #     mdns_service.register(self.service_type, self.device_id, txt,
        #                           self.services.get("sensor", self.beacon_port))
        # except Exception as e:
        #     logger.warn("mdns_service registration failed: {}".format(e))

        self._mdns_registered = True
        logger.info("mDNS hostname/service best-effort registered as {}".format(self.device_id))

    def _announce_txt_record(self):
        """Same fields as the broadcast payload, formatted for mDNS TXT keys.
        Each value gets stringified because TXT keys are bytes. Lists are
        compacted with commas; the services map is compacted as `cap:port,cap:port`.
        """
        p = self._announce_payload()
        return {
            "v":          p["v"],
            "id":         p["id"],
            "role":       p["role"],
            "dev":        p["dev"],
            "fw":         p["fw"],
            "transports": ",".join(p["transports"]),
            "caps":       ",".join(p["caps"]),
            "matrix":     ",".join(str(x) for x in p["matrix"]),
            "services":   ",".join("{}:{}".format(k, v) for k, v in p["services"].items()),
        }

    async def announce_loop(self):
        """Periodic broadcast beacon. Cadence drops to silent once a hub is
        subscribed; resumes the moment the subscriber list empties.
        """
        # Try the mDNS registration once (no-ops if the build lacks support).
        try:
            self.register_mdns()
        except Exception:
            pass

        while True:
            try:
                if not self.subscribers.has_any():
                    self._send_beacon()
            except Exception as e:
                logger.warn("announce_loop iter failed: {}".format(e))
            await asyncio.sleep(self.announce_interval_s)

    def _send_beacon(self):
        """Send one UDP broadcast announce to 255.255.255.255:beacon_port."""
        try:
            payload = ujson.dumps(self._announce_payload()).encode("utf-8")
            self._beacon_sock.sendto(payload, ("255.255.255.255", self.beacon_port))
        except OSError as e:
            # Errno 12 (ENOMEM) means the lwip pbuf pool is exhausted; not fatal,
            # next tick will try again. Errno 11 (EAGAIN) means the send buffer
            # is full; same story. Anything else gets a louder log.
            errno = getattr(e, "errno", None) or (e.args[0] if e.args else None)
            if errno in (11, 12):
                pass
            else:
                logger.warn("Beacon send failed: errno={} {}".format(errno, e))
        except Exception as e:
            logger.warn("Beacon send unexpected error: {}".format(e))
