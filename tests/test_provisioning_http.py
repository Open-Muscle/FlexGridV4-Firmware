"""Content-Length aware provisioning HTTP handler (lib/provisioning.py).

The handler used to do one reader.read(2048), so a client that sent the
headers and the body in separate TCP segments had its body dropped and the
SSID was never saved. These tests drive the real handler over real loopback
sockets with several send patterns.

To run the identical checks against the LASK5 copy of the module, point
OM_PROVISIONING_PATH at its om_provisioning.py.
"""
import asyncio
import http.client
import json
import os
import socket
import time
import unittest

import mp_stubs

PATHS = [("v4", os.path.join(mp_stubs.ROOT, "lib", "provisioning.py"))]
if os.environ.get("OM_PROVISIONING_PATH"):
    PATHS.append(("lask5", os.environ["OM_PROVISIONING_PATH"]))


class FakeSettingsManager:
    def __init__(self):
        self.saves = []

    def save(self, settings):
        self.saves.append(dict(settings))
        return True


def make_state(prov, **extra):
    settings = {"device_id": "flexgrid-abc123", "device_type": "flexgrid",
                "fw_version": "v4.0.0", "wifi_ssid": "", "wifi_password": ""}
    settings.update(extra)
    sm = FakeSettingsManager()
    return prov._ProvisioningState(settings, sm, {"caps": ["sensor"], "matrix": [15, 4]}), sm


def head(path, length, method="POST"):
    return ("{} {} HTTP/1.1\r\nHost: 192.168.4.1\r\n"
            "Content-Type: application/json\r\nContent-Length: {}\r\n\r\n"
            .format(method, path, length)).encode()


def read_response(sock):
    sock.settimeout(5)
    data = b""
    while True:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        data += chunk
    status = int(data.split(b" ", 2)[1]) if data else 0
    body = data.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in data else b""
    return status, (json.loads(body) if body[:1] == b"{" else body)


class ProvisioningHttpMixin:
    prov = None   # set per subclass

    def serve(self, state):
        """Start the handler on an ephemeral loopback port; returns
        (loop, server, port)."""
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        server = loop.run_until_complete(asyncio.start_server(
            lambda r, w: self.prov._handle(r, w, state), "127.0.0.1", 0))
        self.addCleanup(lambda: loop.run_until_complete(self._close(server)))
        return loop, server, server.sockets[0].getsockname()[1]

    @staticmethod
    async def _close(server):
        server.close()
        await server.wait_closed()

    def drive(self, state, client_fn):
        """Run client_fn(port) in a thread while the loop serves."""
        loop, _server, port = self.serve(state)
        return loop.run_until_complete(loop.run_in_executor(None, client_fn, port))

    # -- send patterns ------------------------------------------------------

    def post_split(self, port, body, gap, path="/provision"):
        s = socket.create_connection(("127.0.0.1", port))
        try:
            s.sendall(head(path, len(body)))
            if gap:
                time.sleep(gap)
            s.sendall(body)
            return read_response(s)
        finally:
            s.close()

    def post_single(self, port, body, path="/provision"):
        s = socket.create_connection(("127.0.0.1", port))
        try:
            s.sendall(head(path, len(body)) + body)
            return read_response(s)
        finally:
            s.close()

    def post_http_client(self, port, body):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            c.request("POST", "/provision", body=body,
                      headers={"Content-Type": "application/json"})
            r = c.getresponse()
            r.read()
            return r.status
        finally:
            c.close()

    # -- tests --------------------------------------------------------------

    def test_http_client_20_of_20(self):
        body = json.dumps({"ssid": "HomeNet", "password": "correct-horse-1"}).encode()
        state, sm = make_state(self.prov)

        def client(port):
            return [self.post_http_client(port, body) for _ in range(20)]

        statuses = self.drive(state, client)
        self.assertEqual(statuses, [200] * 20)
        self.assertEqual(len(sm.saves), 20)
        self.assertTrue(all(s["wifi_ssid"] == "HomeNet" for s in sm.saves))

    def test_split_segments_20_of_20(self):
        body = json.dumps({"ssid": "HomeNet", "password": "correct-horse-1"}).encode()
        gaps = [0, 0.001, 0.01, 0.05] * 5
        state, sm = make_state(self.prov)

        def client(port):
            return [self.post_split(port, body, g)[0] for g in gaps]

        self.assertEqual(self.drive(state, client), [200] * 20)
        self.assertEqual(len(sm.saves), 20)
        self.assertTrue(all(s["wifi_ssid"] == "HomeNet" for s in sm.saves))

    def test_split_with_500ms_gap_persists_ssid(self):
        body = json.dumps({"ssid": "SlowNet", "password": "correct-horse-1"}).encode()
        state, sm = make_state(self.prov)
        status, _ = self.drive(state, lambda port: self.post_split(port, body, 0.5))
        self.assertEqual(status, 200)
        self.assertEqual(state.settings["wifi_ssid"], "SlowNet")

    def test_single_segment_still_works(self):
        body = json.dumps({"ssid": "OneShot", "password": "correct-horse-1"}).encode()
        state, _ = make_state(self.prov)
        status, resp = self.drive(state, lambda port: self.post_single(port, body))
        self.assertEqual(status, 200)
        self.assertEqual(resp["status"], "ok")
        self.assertTrue(state.reboot_pending)

    def test_byte_at_a_time(self):
        body = json.dumps({"ssid": "Dribble", "password": ""}).encode()
        raw = head("/provision", len(body)) + body

        def client(port):
            s = socket.create_connection(("127.0.0.1", port))
            try:
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                for i in range(len(raw)):
                    s.sendall(raw[i:i + 1])
                return read_response(s)
            finally:
                s.close()

        state, _ = make_state(self.prov)
        status, _resp = self.drive(state, client)
        self.assertEqual(status, 200)
        self.assertEqual(state.settings["wifi_ssid"], "Dribble")

    def test_truncated_body_times_out_with_400(self):
        self.prov.READ_TIMEOUT_S, saved = 0.4, self.prov.READ_TIMEOUT_S
        self.addCleanup(setattr, self.prov, "READ_TIMEOUT_S", saved)
        state, sm = make_state(self.prov)

        def client(port):
            s = socket.create_connection(("127.0.0.1", port))
            try:
                s.sendall(head("/provision", 100) + b'{"ssid": "Half')
                return read_response(s)
            finally:
                s.close()

        status, resp = self.drive(state, client)
        self.assertEqual(status, 400)
        self.assertEqual(resp["data"]["message"], "incomplete_body")
        self.assertEqual(sm.saves, [])

    def test_peer_closes_mid_body(self):
        state, sm = make_state(self.prov)

        def client(port):
            s = socket.create_connection(("127.0.0.1", port))
            try:
                s.sendall(head("/provision", 100) + b'{"ssid"')
                s.shutdown(socket.SHUT_WR)
                return read_response(s)
            finally:
                s.close()

        status, resp = self.drive(state, client)
        self.assertEqual(status, 400)
        self.assertEqual(resp["data"]["message"], "incomplete_body")
        self.assertEqual(sm.saves, [])

    def test_oversized_declared_body_gets_413(self):
        state, sm = make_state(self.prov)

        def client(port):
            s = socket.create_connection(("127.0.0.1", port))
            try:
                s.sendall(head("/provision", self.prov.MAX_BODY_BYTES + 1))
                return read_response(s)
            finally:
                s.close()

        status, resp = self.drive(state, client)
        self.assertEqual(status, 413)
        self.assertEqual(resp["data"]["message"], "request_too_large")
        self.assertEqual(sm.saves, [])

    def test_oversized_headers_get_413(self):
        state, _ = make_state(self.prov)

        def client(port):
            s = socket.create_connection(("127.0.0.1", port))
            try:
                s.sendall(b"GET /info HTTP/1.1\r\nX-Pad: " + b"a" * (self.prov.MAX_HEAD_BYTES + 600))
                return read_response(s)
            finally:
                s.close()

        status, _resp = self.drive(state, client)
        self.assertEqual(status, 413)

    def test_bad_content_length(self):
        state, _ = make_state(self.prov)

        def client(port):
            s = socket.create_connection(("127.0.0.1", port))
            try:
                s.sendall(b"POST /provision HTTP/1.1\r\nContent-Length: abc\r\n\r\n")
                return read_response(s)
            finally:
                s.close()

        status, resp = self.drive(state, client)
        self.assertEqual(status, 400)
        self.assertEqual(resp["data"]["message"], "bad_content_length")

    def test_get_info_after_split_headers(self):
        state, _ = make_state(self.prov)

        def client(port):
            s = socket.create_connection(("127.0.0.1", port))
            try:
                s.sendall(b"GET /info HTTP/1.1\r\nHost: x\r\n")
                time.sleep(0.05)
                s.sendall(b"\r\n")
                return read_response(s)
            finally:
                s.close()

        status, resp = self.drive(state, client)
        self.assertEqual(status, 200)
        self.assertEqual(resp["type"], "info")
        self.assertEqual(resp["state"], "unprovisioned")

    # -- credential validation ---------------------------------------------

    def provision(self, payload, **settings):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        state, sm = make_state(self.prov, **settings)
        status, resp = self.drive(state, lambda port: self.post_single(port, body))
        return status, resp, state, sm

    def test_ssid_is_not_stripped(self):
        status, _resp, state, _ = self.provision({"ssid": " Cafe Net ", "password": "correct-horse-1"})
        self.assertEqual(status, 200)
        self.assertEqual(state.settings["wifi_ssid"], " Cafe Net ")

    def test_ssid_byte_length_limit(self):
        ok = "a" * 32
        self.assertEqual(self.provision({"ssid": ok, "password": ""})[0], 200)
        # 17 two-byte characters: 17 characters but 34 bytes.
        status, resp, _s, sm = self.provision({"ssid": "é" * 17, "password": ""})
        self.assertEqual(status, 400)
        self.assertTrue(resp["data"]["message"].startswith("ssid_length_invalid"))
        self.assertEqual(sm.saves, [])
        # 16 two-byte characters: 32 bytes, accepted.
        self.assertEqual(self.provision({"ssid": "é" * 16, "password": ""})[0], 200)

    def test_empty_or_blank_ssid_rejected(self):
        for ssid in ("", "   ", None, 5):
            status, resp, _s, _sm = self.provision({"ssid": ssid, "password": ""})
            self.assertEqual(status, 400, ssid)
            self.assertTrue(resp["data"]["message"].startswith("ssid_length_invalid"))

    def test_password_byte_length_limits(self):
        self.assertEqual(self.provision({"ssid": "n", "password": "x" * 8})[0], 200)
        self.assertEqual(self.provision({"ssid": "n", "password": "x" * 63})[0], 200)
        for pw in ("x" * 7, "x" * 64, "€" * 22):   # 22 euro signs = 66 bytes
            status, resp, _s, sm = self.provision({"ssid": "n", "password": pw})
            self.assertEqual(status, 400)
            self.assertTrue(resp["data"]["message"].startswith("password_length_invalid"))
            self.assertEqual(sm.saves, [])
        # 20 euro signs = 20 chars but 60 bytes: within the limit.
        self.assertEqual(self.provision({"ssid": "n", "password": "€" * 20})[0], 200)

    def test_open_network_empty_password_allowed(self):
        status, _r, state, _ = self.provision({"ssid": "OpenCafe", "password": ""})
        self.assertEqual(status, 200)
        self.assertEqual(state.settings["wifi_password"], "")

    def test_non_object_json_rejected(self):
        body = b'["ssid"]'
        state, sm = make_state(self.prov)
        status, resp = self.drive(state, lambda port: self.post_single(port, body))
        self.assertEqual(status, 400)
        self.assertTrue(resp["data"]["message"].startswith("invalid_json"))
        self.assertEqual(sm.saves, [])

    def test_invalid_json_rejected(self):
        body = b'{"ssid": '
        state, _ = make_state(self.prov)
        status, resp = self.drive(state, lambda port: self.post_single(port, body))
        self.assertEqual(status, 400)
        self.assertTrue(resp["data"]["message"].startswith("invalid_json"))

    # -- fallback bookkeeping exposed through /info and /provision ----------

    def test_provision_resets_join_state(self):
        status, _r, state, _ = self.provision(
            {"ssid": "HomeNet", "password": "correct-horse-1"},
            wifi_joined_ok=True, last_wifi_error="wrong_password", last_ssid="Old")
        self.assertEqual(status, 200)
        self.assertFalse(state.settings["wifi_joined_ok"])
        self.assertEqual(state.settings["last_wifi_error"], "")
        self.assertEqual(state.settings["last_ssid"], "")

    def test_info_reports_last_wifi_error(self):
        state, _ = make_state(self.prov, last_wifi_error="wrong_password", last_ssid="HomeNet")

        def client(port):
            s = socket.create_connection(("127.0.0.1", port))
            try:
                s.sendall(b"GET /info HTTP/1.1\r\nHost: x\r\n\r\n")
                return read_response(s)
            finally:
                s.close()

        status, resp = self.drive(state, client)
        self.assertEqual(status, 200)
        self.assertEqual(resp["last_wifi_error"], "wrong_password")
        self.assertEqual(resp["last_ssid"], "HomeNet")

    def test_info_without_failure_has_empty_error(self):
        self.assertEqual(make_state(self.prov)[0].info_payload()["last_wifi_error"], "")


def _make_case(label, path):
    cls = type("ProvisioningHttp_" + label, (ProvisioningHttpMixin, unittest.TestCase), {})

    def setUp(self):
        self.prov = mp_stubs.load_by_path("prov_" + label, path)

    cls.setUp = setUp
    return cls


for _label, _path in PATHS:
    globals()["ProvisioningHttp_" + _label] = _make_case(_label, _path)
del _label, _path


if __name__ == "__main__":
    unittest.main()
