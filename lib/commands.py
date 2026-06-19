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

    async def start(self):
        """Bind cmd_port and accept connections. Returns once the server is
        listening; the actual accept loop runs as a background asyncio task.
        """
        self._server = await asyncio.start_server(self._handle_client, "0.0.0.0", self.port)
        logger.info("Command server listening on TCP {}".format(self.port))

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
        except Exception as e:
            logger.warn("Command client {} errored: {}".format(peer, e))
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
        except Exception as e:
            logger.warn("Command verb={} from {} raised: {}".format(verb, peer, e))
            return {
                "v": "1.0", "type": "ack", "status": _ERR,
                "msg_id": msg_id,
                "data": {"verb": verb, "message": str(e)},
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
        return {
            "id":         device_state.device_id,
            "dev":        device_state.device_type,
            "fw":         device_state.fw_version,
            "matrix":     list(device_state.matrix_dims),
            "caps":       list(device_state.caps),
            "subscribers": device_state.subscribers.snapshot(),
        }

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

    return {
        "subscribe":     subscribe,
        "unsubscribe":   unsubscribe,
        "heartbeat":     heartbeat,
        "get_info":      get_info,
        "set_scan_rate": set_scan_rate,
        "start_stream":  start_stream,
        "stop_stream":   stop_stream,
        "reboot":        reboot,
    }
