# lib/subscribers.py
# Subscriber list for the source role.
#
# A hub (phone, PC) discovers this device, opens the command channel, and sends
# a `subscribe` message with its own (host, port, transport) so we know where
# to unicast sensor frames. Hubs send heartbeats ~1 Hz; entries older than the
# configured timeout (default 5 s) are dropped.
#
# Spec section 5.1 caps the subscriber list at ~4 to bound UDP fan-out cost on
# the device. We respect the cap and reject further subscriptions with a clear
# error reply (handled by commands.py, which calls add() and gets a bool back).
#
# This module is intentionally framework-agnostic: no asyncio import, no socket
# import. It is just data + book-keeping. The network module sends; this module
# decides who to send to.

import time
import logger


class Subscribers:
    def __init__(self, max_subscribers=4, heartbeat_timeout_s=5):
        self.max_subscribers = max_subscribers
        # ms; convert from seconds at construction so the prune hot path stays
        # cheap (one ticks_diff per entry per prune call).
        self.heartbeat_timeout_ms = int(heartbeat_timeout_s * 1000)
        # Each entry: dict with host, port, transport, hub_id (optional),
        # last_heartbeat_ms (set at subscribe + each heartbeat).
        self._entries = []

    def add(self, host, port, transport="wifi", hub_id=None):
        """Add or refresh a subscriber. Returns True if accepted, False if the
        list is full. If (host, port) already exists, refreshes its heartbeat
        timestamp without consuming a new slot.

        transport: "wifi" or "ble". A wifi subscriber's (host, port) is its
        UDP address. A ble subscriber's (host, port) is reserved for future
        BLE notify routing; (host, port) values are not meaningful there.
        """
        now = time.ticks_ms()
        for e in self._entries:
            if e["host"] == host and e["port"] == port and e["transport"] == transport:
                e["last_heartbeat_ms"] = now
                if hub_id:
                    e["hub_id"] = hub_id
                logger.info("Subscriber refreshed: {} {}:{} (hub_id={})".format(
                    transport, host, port, hub_id))
                return True
        if len(self._entries) >= self.max_subscribers:
            logger.warn("Subscriber list full ({}/{}); rejecting {}:{}".format(
                len(self._entries), self.max_subscribers, host, port))
            return False
        self._entries.append({
            "host":              host,
            "port":              port,
            "transport":         transport,
            "hub_id":            hub_id,
            "last_heartbeat_ms": now,
        })
        logger.info("Subscriber added: {} {}:{} (hub_id={}, {}/{})".format(
            transport, host, port, hub_id, len(self._entries), self.max_subscribers))
        return True

    def remove(self, host, port, transport="wifi"):
        """Explicit unsubscribe. Returns True if found+removed, False if not."""
        for i, e in enumerate(self._entries):
            if e["host"] == host and e["port"] == port and e["transport"] == transport:
                self._entries.pop(i)
                logger.info("Subscriber removed: {} {}:{}".format(transport, host, port))
                return True
        return False

    def heartbeat(self, host, port, transport="wifi"):
        """Refresh a subscriber's heartbeat. Idempotent; if the (host,port)
        isn't currently subscribed this is a no-op + warning. Hubs sometimes
        race and send a heartbeat after a server-side expiry; that is the
        signal for them to re-subscribe."""
        now = time.ticks_ms()
        for e in self._entries:
            if e["host"] == host and e["port"] == port and e["transport"] == transport:
                e["last_heartbeat_ms"] = now
                return True
        logger.warn("Heartbeat from unknown subscriber {}:{} ({})".format(host, port, transport))
        return False

    def prune_stale(self):
        """Drop subscribers whose last heartbeat is older than the timeout.
        Called by a periodic loop in flexgrid.py. Returns the number of
        entries dropped, useful for surfacing 'hub disconnected' to the UI.
        """
        now = time.ticks_ms()
        kept = []
        dropped = 0
        for e in self._entries:
            age_ms = time.ticks_diff(now, e["last_heartbeat_ms"])
            if age_ms > self.heartbeat_timeout_ms:
                logger.info("Subscriber stale, dropping: {}:{} age={}ms".format(
                    e["host"], e["port"], age_ms))
                dropped += 1
            else:
                kept.append(e)
        self._entries = kept
        return dropped

    def wifi_targets(self):
        """Iterable of (host, port) for every active Wi-Fi subscriber.
        network_manager iterates this to fan out each sensor frame."""
        return [(e["host"], e["port"]) for e in self._entries if e["transport"] == "wifi"]

    def ble_targets(self):
        """Iterable of hub identifiers for every active BLE subscriber.
        Phase 4 only; today this is always empty."""
        return [e.get("hub_id") for e in self._entries if e["transport"] == "ble"]

    def count(self):
        """Total subscribers across both transports."""
        return len(self._entries)

    def has_any(self):
        """True if at least one hub is subscribed (used to gate the
        broadcast-beacon-while-unsubscribed behavior in discovery.py)."""
        return len(self._entries) > 0

    def snapshot(self):
        """List of subscriber summaries for diagnostics / UI."""
        now = time.ticks_ms()
        return [
            {
                "host":      e["host"],
                "port":      e["port"],
                "transport": e["transport"],
                "hub_id":    e.get("hub_id"),
                "age_ms":    time.ticks_diff(now, e["last_heartbeat_ms"]),
            }
            for e in self._entries
        ]
