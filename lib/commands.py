# lib/commands.py
# Command channel server.
#
# Hubs (phone / PC) connect to this device's cmd_port (default 8001) and send
# JSON command messages, one per line (newline-delimited). The server replies
# with an ack (also one JSON line) for each command.
#
# Why TCP newline-delimited JSON instead of WebSocket: WebSocket adds framing
# overhead, header handling, and a much larger module footprint that V4
# firmware does not need. The hubs are not browsers; they are native Android
# and Python code that can open a raw TCP socket trivially. WebSocket can be
# added later in parallel if a browser hub needs it; for now keep it simple.
#
# Supported command verbs (spec section 6.1):
#   subscribe    -> add (host,port,transport) to subscriber list
#   unsubscribe  -> remove
#   heartbeat    -> refresh subscriber's last-seen timestamp
#   get_info     -> reply with device capabilities and firmware info
#   set_scan_rate -> change the sensor scan interval
#   start_stream -> resume streaming (if previously stopped)
#   stop_stream  -> pause streaming without losing the subscriber list
#   reboot       -> machine.soft_reset() after a brief ack delay
#
# The dispatch table is built at init time from a handlers dict so flexgrid.py
# wires the actual behavior (calling into sensor_matrix, subscribers, etc.)
# without this module knowing about MicroPython hardware.

import asyncio
import ujson
import logger


_OK  = "ok"
_ERR = "error"

# Supervisor backoff after the listener task dies. Keep it short; the listener
# being down means hubs cannot subscribe, so faster restart wins over backoff.
_SUPERVISOR_RESTART_DELAY_S = 1

# CancelledError is a BaseException subclass in modern asyncio. We MUST let it
# propagate when a task is deliberately cancelled (e.g. shutdown). Older
# MicroPython builds may not expose it; fall back to a sentinel that never
# matches so the BaseException catch still works.
try:
    _CancelledError = asyncio.CancelledError
except AttributeError:
    _CancelledError = type("_NoCancelledError", (BaseException,), {})


class CommandServer:
    def __init__(self, port, handlers):
        """
        handlers: dict mapping verb -> async fn(data: dict, peer: tuple) -> dict
                  Each handler returns a payload dict that becomes the ack's
                  `data` field. Raising any exception turns the ack into an
                  error with the exception text as the message.
        """
        self.port = port
        self.handlers = handlers
        self._server = None

    async def serve_forever(self):
        """Supervised bind + accept loop. Spawn this as a background task from
        flexgrid.py: ``asyncio.create_task(cmd_server.serve_forever())``.

        Why a supervisor:
        Without one, an mpremote-induced KeyboardInterrupt (a BaseException,
        not Exception) lands on whichever asyncio task is currently running.
        If that task is the internal accept task spawned by
        ``asyncio.start_server``, the listener dies silently while the rest of
        the device keeps running. Phone and PC hubs then see a black hole.
        This loop catches BaseException (except CancelledError, which means
        deliberate shutdown), logs, sleeps briefly, and rebinds.
        """
        while True:
            try:
                self._server = await asyncio.start_server(
                    self._handle_client, "0.0.0.0", self.port)
                logger.info("Command server listening on TCP {}".format(self.port))
                # Stay alive so the supervisor catches anything that propagates
                # up from the asyncio internals. Sleep in a long loop rather
                # than one huge sleep so a cancel surfaces promptly.
                while True:
                    await asyncio.sleep(60)
            except (_CancelledError, SystemExit):
                logger.info("Command server cancelled; exiting supervisor")
                raise
            except BaseException as e:
                logger.warn("Command server died: {} ({}); restarting in {}s".format(
                    type(e).__name__, e, _SUPERVISOR_RESTART_DELAY_S))
                try:
                    if self._server is not None:
                        self._server.close()
                except Exception:
                    pass
                self._server = None
                await asyncio.sleep(_SUPERVISOR_RESTART_DELAY_S)

    async def _handle_client(self, reader, writer):
        peer = writer.get_extra_info("peername") or ("?", 0)
        logger.info("Command client connected: {}".format(peer))
        try:
            while True:
                # readline() returns bytes; empty means peer closed.
                line = await reader.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                ack = await self._handle_one(line, peer)
                writer.write(ujson.dumps(ack).encode("utf-8") + b"\n")
                await writer.drain()
        except (_CancelledError, SystemExit):
            # Deliberate shutdown of this client task; clean up and re-raise.
            raise
        except BaseException as e:
            # Catch BaseException (not Exception) so a KeyboardInterrupt
            # from a dev tool doesn't propagate past this boundary and take
            # down the listener task with it. The supervisor in serve_forever
            # is the safety net; this is the first line of defense.
            logger.warn("Command client {} errored: {} ({})".format(
                peer, type(e).__name__, e))
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            logger.info("Command client disconnected: {}".format(peer))

    async def _handle_one(self, raw_line, peer):
        """Parse one command line, dispatch, build an ack dict."""
        msg_id = None
        verb = None
        try:
            pkt = ujson.loads(raw_line.decode("utf-8"))
        except Exception as e:
            return {
                "v": "1.0", "type": "ack", "status": _ERR,
                "msg_id": None,
                "data": {"message": "invalid_json: {}".format(e)},
            }

        msg_id = pkt.get("msg_id")
        data = pkt.get("data") or {}
        verb = data.get("verb")
        if not verb:
            return {
                "v": "1.0", "type": "ack", "status": _ERR,
                "msg_id": msg_id,
                "data": {"message": "missing verb in data"},
            }
        handler = self.handlers.get(verb)
        if handler is None:
            return {
                "v": "1.0", "type": "ack", "status": _ERR,
                "msg_id": msg_id,
                "data": {"message": "unknown_verb: " + str(verb)},
            }

        try:
            ack_data = await handler(data, peer)
        except (_CancelledError, SystemExit):
            # Let deliberate cancel / soft_reset propagate past the dispatch
            # so the asyncio runtime can act on them (e.g. machine.soft_reset
            # raises SystemExit; we must not catch it here, mirrors the
            # SystemExit fix in 71e0eae for the loop boundaries).
            raise
        except BaseException as e:
            logger.warn("Command verb={} from {} raised: {} ({})".format(
                verb, peer, type(e).__name__, e))
            return {
                "v": "1.0", "type": "ack", "status": _ERR,
                "msg_id": msg_id,
                "data": {"verb": verb,
                         "message": "{}: {}".format(type(e).__name__, e)},
            }
        return {
            "v": "1.0", "type": "ack", "status": _OK,
            "msg_id": msg_id,
            "data": dict({"verb": verb}, **(ack_data or {})),
        }


def build_handlers(device_state):
    """Factory that returns the handlers dict, closing over device_state.

    device_state is expected to be an object exposing:
      .subscribers        (lib.subscribers.Subscribers)
      .device_id          (str)
      .device_type        (str)
      .fw_version         (str)
      .matrix_dims        (tuple, e.g. (15, 4))
      .caps               (list of str)
      .set_scan_interval(ms: int)   # mutate the live sensor loop's interval
      .start_stream() / .stop_stream()   # toggle sensor streaming
      .request_reboot()   # set a flag the main loop polls
    """

    async def subscribe(data, peer):
        host = data.get("host") or peer[0]
        port = int(data["port"])
        transport = data.get("transport", "wifi")
        hub_id = data.get("hub_id")
        ok = device_state.subscribers.add(host, port, transport, hub_id)
        return {
            "accepted":         ok,
            "subscriber_count": device_state.subscribers.count(),
            "max_subscribers":  device_state.subscribers.max_subscribers,
        }

    async def unsubscribe(data, peer):
        host = data.get("host") or peer[0]
        port = int(data["port"])
        transport = data.get("transport", "wifi")
        removed = device_state.subscribers.remove(host, port, transport)
        return {"removed": removed, "subscriber_count": device_state.subscribers.count()}

    async def heartbeat(data, peer):
        host = data.get("host") or peer[0]
        port = int(data["port"])
        transport = data.get("transport", "wifi")
        refreshed = device_state.subscribers.heartbeat(host, port, transport)
        return {"refreshed": refreshed}

    async def get_info(data, peer):
        reply = {
            "id":         device_state.device_id,
            "dev":        device_state.device_type,
            "fw":         device_state.fw_version,
            "matrix":     list(device_state.matrix_dims),
            "caps":       list(device_state.caps),
            "subscribers": device_state.subscribers.snapshot(),
        }
        # Optional imu chip + per-LSB scale so hubs can normalize data.imu
        # raw counts across mixed-fleet sources (overseer #0217). Key
        # absent when no IMU is present, so legacy hubs see no change.
        imu_scale = getattr(device_state, "imu_scale", None)
        if imu_scale:
            reply["imu"] = dict(imu_scale)
        # Optional role tag (board #0220). Omitted from reply when the
        # device has no role assigned so a phone can detect "unassigned"
        # by the field's absence and prompt the user to tag it.
        role = getattr(device_state, "role", "") or ""
        if role:
            reply["role"] = role
        # Optional power_mode tag so the phone can show the same
        # STREAM/STANDBY/CHARGE state in its UI. Always present when
        # the device implements the power-mode state machine.
        pm = getattr(device_state, "power_mode", None)
        if pm:
            reply["power_mode"] = pm
        # LAN address of the band, so a hub can show it (and add the band by
        # IP) without a network scan. Omitted until the band has joined.
        ip = getattr(device_state, "ip", "") or ""
        if ip:
            reply["ip"] = ip
        return reply

    async def set_scan_rate(data, peer):
        ms = int(data["interval_ms"])
        if ms < 5 or ms > 2000:
            raise ValueError("interval_ms out of range (5..2000): {}".format(ms))
        device_state.set_scan_interval(ms)
        return {"interval_ms": ms}

    async def start_stream(data, peer):
        device_state.start_stream()
        return {"streaming": True}

    async def stop_stream(data, peer):
        device_state.stop_stream()
        return {"streaming": False}

    async def reboot(data, peer):
        device_state.request_reboot()
        return {"rebooting": True}

    async def set_role(data, peer):
        """Hub-pushed role assignment (board #0220). Stored in
        device_state + persisted to settings so the role survives
        reboot. Drives the idle-LED hue (BLUE left, RED right,
        GREEN labeler, WHITE unassigned).

        Valid values: 'left' / 'right' / 'labeler' / '' (clear).
        Unknown values get clamped to '' inside DeviceState.set_role
        so a buggy hub cannot wedge the device into an undefined LED
        state. Persistence is best-effort; a flash write failure is
        logged but does not error the verb."""
        role = data.get("role", "")
        device_state.set_role(role)
        # Persist via the SettingsManager-attached save callback if the
        # device_state exposes one. The flexgrid wiring sets this in
        # main() before serve_forever spins up.
        save = getattr(device_state, "save_settings", None)
        if save is not None:
            try:
                save("role", device_state.role)
            except Exception as e:
                logger.warn("set_role persist failed: {}".format(e))
        return {"role": device_state.role}

    async def set_power_mode(data, peer):
        """Network-commanded power mode (board #0323 follow-up): lets a
        hub put the band into CHARGE (Wi-Fi off, purple LED) without
        the MENU button, which is unsoldered on as-built V4 units.

        mode: "stream" | "standby" | "charge". The request is queued on
        device_state and applied by power_mode_loop within ~100 ms, so
        this ack reaches the hub BEFORE the radio drops. Exiting charge
        afterwards requires a physical action (BOOT hold 2-6 s or USB
        unplug) since the device is off the network by design."""
        mode = data.get("mode", "")
        if mode not in ("stream", "standby", "charge", "record"):
            raise ValueError("mode must be stream|standby|charge|record: {}".format(mode))
        device_state.requested_power_mode = mode
        note = "charge/record turn Wi-Fi off; exit via SELECT tap or MENU/SELECT hold on the device"
        return {"power_mode": mode, "applies_in_ms": 100, "note": note}

    return {
        "subscribe":     subscribe,
        "unsubscribe":   unsubscribe,
        "heartbeat":     heartbeat,
        "get_info":      get_info,
        "set_scan_rate": set_scan_rate,
        "start_stream":  start_stream,
        "stop_stream":   stop_stream,
        "reboot":        reboot,
        "set_role":      set_role,
        "set_power_mode": set_power_mode,
    }
