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

# Cooperative-yield interval for the discover listener loop. Keeps the
# socket polling cheap while still responding to a probe within ~50 ms.
_DISCOVER_POLL_MS = 50


class Discovery:
    def __init__(self, settings, subscribers, sta, services, caps, matrix_dims,
                 imu_scale=None):
        """
        settings: the loaded settings dict (gives device_id, mdns_service, etc.)
        subscribers: lib.subscribers.Subscribers instance
        sta: network.WLAN(STA_IF), used to read the current IP for mDNS
        services: dict mapping capability name -> port, e.g. {"sensor": 3141, "cmd": 8001}
        caps: list of capability strings advertised in the announce
        matrix_dims: tuple (cols, rows) for the sensor matrix
        imu_scale: optional {chip, gyro_dps_per_lsb, accel_g_per_lsb} dict
                   from imu.scale_dict(); folded into the announce payload
                   under the "imu" key so hubs can normalize data.imu raw
                   counts to a common unit without an extra get_info round
                   trip. Omitted (key absent) when no IMU is present.
        """
        self.settings = settings
        self.subscribers = subscribers
        self.sta = sta
        self.services = services
        self.caps = caps
        self.matrix_dims = matrix_dims
        self.imu_scale = imu_scale

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
        # boot_seq is a monotonic restart counter persisted in settings.json
        # and incremented once per boot (in flexgrid.py main() before the
        # Discovery instance is constructed). Hubs include it on every
        # announce so a phone can detect a fresh boot vs same session and
        # flush cached subscription state. Default 0 keeps the field
        # well-defined on settings.json files that pre-date this addition.
        self.boot_seq = int(settings.get("boot_seq", 0))

        # Sender socket for the broadcast beacon. Created once, reused.
        self._beacon_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._beacon_sock.setblocking(False)
        try:
            self._beacon_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except Exception as e:
            logger.warn("SO_BROADCAST setsockopt failed: {}".format(e))

        # Listener socket for {verb:"discover"} probes. Bound on the
        # announce port (3140) so a phone joining a new network can
        # actively pull an announce instead of waiting up to one beacon
        # interval. SO_REUSEADDR is best-effort -- some MicroPython
        # builds do not expose it but the bind still succeeds on ESP32
        # lwIP because no other socket has the port bound.
        self._discover_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._discover_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except Exception:
            pass
        try:
            self._discover_sock.bind(("", self.beacon_port))
            self._discover_sock.setblocking(False)
            self._discover_listen_ok = True
        except OSError as e:
            logger.warn("Discover listener bind failed (errno={}); active discovery disabled".format(
                getattr(e, "errno", None) or (e.args[0] if e.args else None)))
            self._discover_listen_ok = False

        # mDNS state. Whether we registered, and how, is best-effort; the
        # broadcast beacon is the reliable path.
        self._mdns_registered = False

    def _announce_payload(self):
        """Build the announce JSON. The IP is NOT included here: the hub takes
        it from the packet source address (UDP broadcast) or from the mDNS A
        record. This is intentional per spec section 4.1: 'The IP is never in
        the announce; the listener takes it from the packet/mDNS source'."""
        pkt = {
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
            # boot_seq lets a phone detect that the device just rebooted
            # (counter jumped) so it can drop any cached subscription
            # state and re-subscribe cleanly. Monotonic per-device.
            "boot_seq":  self.boot_seq,
            # The band answers JSON commands on its USB serial port
            # (lib/usb_console.py).
            "usb_console": 1,
        }
        if self.imu_scale:
            pkt["imu"] = dict(self.imu_scale)
        return pkt

    def register_mdns(self):
        """Best-effort mDNS registration. Tries the network.mDNS / mdns_service
        paths some MicroPython builds expose. On builds without an mDNS API
        this no-ops and we rely entirely on the broadcast beacon.

        We do NOT raise on failure; the broadcast beacon makes mDNS optional.
        Audit-pass fix (board #0189): only mark _mdns_registered=True on
        actual success so a Wi-Fi-down boot does not poison the flag
        permanently; the announce_loop's Wi-Fi-reconnect path can then
        retry register_mdns() on the next link-up.
        """
        if self._mdns_registered:
            return

        success = False
        try:
            # Some MicroPython builds expose `network.WLAN.config('hostname', ...)`
            # which underneath registers an mDNS hostname record. Set our id as
            # the hostname so `flexgrid-<6hex>.local` resolves on the LAN.
            self.sta.config(hostname=self.device_id)
            success = True
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

        if success:
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

        Catches BaseException (except CancelledError) so an interrupt during
        sendto cannot silently kill the announce task. Same defensive pattern
        as sensor_loop / cmd-server (board #0156 WDT-reset investigation).

        Re-registers mDNS on Wi-Fi reconnect (audit pass, board #0189): the
        sta.config(hostname=...) call only takes effect while STA is up,
        and after a flap the hostname binding can be lost. Tracks the
        connected edge per loop iter and re-fires register_mdns() on the
        rising transition. Cheap (one isconnected() call per second);
        keeps `<device-id>.local` resolution alive across reconnects.
        """
        # Try the mDNS registration once (no-ops if the build lacks support).
        try:
            self.register_mdns()
        except Exception:
            pass

        was_connected = False
        while True:
            try:
                now_connected = self.sta.isconnected()
                if now_connected and not was_connected:
                    # Rising edge: Wi-Fi just came back up. Force a fresh
                    # mDNS registration; the prior one (if any) is stale.
                    self._mdns_registered = False
                    try:
                        self.register_mdns()
                    except Exception as e:
                        logger.warn("mDNS re-register on reconnect failed: {}".format(e))
                was_connected = now_connected
                # Gate on link-up: in CHARGE mode the radio is off and a
                # sendto would raise once per second, and those WRN lines
                # flush to flash (the exact stall class fixed in 65ffa06).
                if now_connected and not self.subscribers.has_any():
                    self._send_beacon()
            except (asyncio.CancelledError, SystemExit):
                raise
            except BaseException as e:
                logger.warn("announce_loop iter failed: {} ({})".format(
                    type(e).__name__, e))
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

    async def discover_listener_loop(self):
        """Listen on the announce port for active discovery probes from
        phones/hubs and respond with an immediate announce.

        Wire shape of an accepted probe (PROTOCOL.md section 5.5):
            {"v":"1.0", "type":"cmd", "data":{"verb":"discover"}}

        Sent to 255.255.255.255:3140 (broadcast) by a phone that just
        joined a new Wi-Fi network and does not want to wait up to one
        beacon interval for the periodic announce. On receipt we:
          1. Broadcast an announce (reaches every hub bound to 3140 on
             the link, same path as the periodic beacon).
          2. Unicast a copy to the prober's (host, port) tuple as a
             belt-and-suspenders backup -- if the phone sent its probe
             from an ephemeral port and is not bound to 3140, the
             broadcast would not reach it but the unicast will.

        Cooperative-yield every _DISCOVER_POLL_MS so the loop is cheap
        even when no probes are arriving. Hardened with the same
        SystemExit/CancelledError re-raise + BaseException catch as the
        rest of the device's loops (board #0166).

        Skips silently if the listen socket failed to bind at init
        (some networks block binding the same port a sender is
        broadcasting from; rare on ESP32 lwIP but possible).
        """
        if not self._discover_listen_ok:
            logger.warn("discover_listener_loop: socket not bound; exiting task")
            return
        while True:
            try:
                while True:
                    try:
                        data, addr = self._discover_sock.recvfrom(1024)
                    except OSError:
                        # No data ready; drop out of the inner drain loop
                        # and yield. EAGAIN/EWOULDBLOCK on a non-blocking
                        # socket is the normal idle case.
                        break
                    self._handle_discover(data, addr)
            except (asyncio.CancelledError, SystemExit):
                raise
            except BaseException as e:
                logger.warn("discover_listener iter failed: {} ({})".format(
                    type(e).__name__, e))
            await asyncio.sleep_ms(_DISCOVER_POLL_MS)

    def _handle_discover(self, data, addr):
        """Parse one received UDP packet on the announce port. If it is a
        discover probe, broadcast + unicast an immediate announce.

        Anything else (the device's own broadcast announce echoed back,
        a malformed packet, a stray UDP datagram from another protocol)
        is silently ignored. Defensive: parsing is wrapped so a malformed
        payload cannot kill the listener."""
        try:
            msg = ujson.loads(data)
        except (ValueError, TypeError):
            return
        if not isinstance(msg, dict):
            return
        if msg.get("type") != "cmd":
            return
        verb = msg.get("data", {}).get("verb") if isinstance(msg.get("data"), dict) else None
        if verb != "discover":
            return

        payload = ujson.dumps(self._announce_payload()).encode("utf-8")
        # Broadcast: reaches every hub already listening on the announce
        # port on this link segment.
        try:
            self._beacon_sock.sendto(payload, ("255.255.255.255", self.beacon_port))
        except OSError:
            pass
        # Unicast: also reaches a prober that bound to an ephemeral port.
        try:
            self._discover_sock.sendto(payload, addr)
        except OSError:
            pass
