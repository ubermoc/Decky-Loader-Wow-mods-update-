# Copyright (c) 2026 ubermoc. All rights reserved. See LICENSE. WoW Addon Updater, https://github.com/ubermoc/Decky-Loader-Wow-mods-update-
# Provenance: wow-addon-updater/2f44d0b2-b3c2-4726-a93d-480b19a8008e
import base64
import hashlib
import json
import socket
import struct
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "py_modules"))

from wow_addon_tools import cdp  # noqa: E402


def _read_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError
        buf += chunk
    return buf


def _read_client_frame(conn):
    b1, b2 = _read_exact(conn, 2)
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack(">H", _read_exact(conn, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", _read_exact(conn, 8))[0]
    assert b2 & 0x80, "client frames must be masked"
    mask = _read_exact(conn, 4)
    data = bytes(b ^ mask[i % 4] for i, b in enumerate(_read_exact(conn, n)))
    return b1 & 0x0F, data


def _server_frame(text: str) -> bytes:
    data = text.encode()
    if len(data) < 126:
        return bytes([0x81, len(data)]) + data
    if len(data) < 65536:
        return bytes([0x81, 126]) + struct.pack(">H", len(data)) + data
    return bytes([0x81, 127]) + struct.pack(">Q", len(data)) + data


class FakeApp:
    """Speaks just enough of Chromium's DevTools HTTP + WebSocket protocol."""

    def __init__(self, button_states):
        self.states = list(button_states)  # successive replies to the FIND script
        self.clicks = 0
        self.closed = False
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def stop(self):
        self.srv.close()

    def _serve(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            req = b""
            while b"\r\n\r\n" not in req:
                req += conn.recv(4096)
            first = req.split(b"\r\n")[0].decode()
            path = first.split(" ")[1]
            if b"Upgrade: websocket" in req:
                key = [ln for ln in req.decode().split("\r\n") if ln.lower().startswith("sec-websocket-key")][0].split(": ")[1]
                accept = base64.b64encode(hashlib.sha1((key + cdp._GUID).encode()).digest()).decode()
                conn.sendall(
                    f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode()
                )
                self._ws(conn, path)
                return
            if path == "/json/version":
                body = json.dumps({"webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/browser/x"})
            elif path == "/json":
                body = json.dumps(
                    [{"type": "page", "title": "CurseForge", "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/page/1"}]
                )
            else:
                conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                return
            conn.sendall(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n{body}".encode())
        except Exception:
            pass
        finally:
            conn.close()

    def _ws(self, conn, path):
        while True:
            opcode, data = _read_client_frame(conn)
            if opcode == 0x8:
                return
            msg = json.loads(data)
            method, params = msg["method"], msg.get("params", {})
            result = {}
            if method == "Runtime.evaluate":
                expr = params["expression"]
                if expr == cdp.FIND_JS:
                    state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
                    result = {"result": {"type": "object", "value": state}}
                elif expr == cdp.CLICK_JS:
                    self.clicks += 1
                    result = {"result": {"type": "boolean", "value": True}}
            elif method == "Browser.close":
                self.closed = True
            # an unrelated event first, like Chromium sends, then the real reply
            conn.sendall(_server_frame(json.dumps({"method": "Page.something", "params": {}})))
            conn.sendall(_server_frame(json.dumps({"id": msg["id"], "result": result})))


NOSLEEP = lambda _s: None  # noqa: E731


class FrameTests(unittest.TestCase):
    def test_accept_key_matches_rfc_example(self):
        self.assertEqual(cdp._accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_client_frames_are_masked_and_sized(self):
        for size in (5, 200, 70000):
            frame = cdp.encode_frame(b"x" * size)
            self.assertTrue(frame[1] & 0x80)
        self.assertEqual(cdp.encode_frame(b"hi")[0], 0x81)

    def test_refuses_non_local_addresses(self):
        with self.assertRaises(cdp.CdpError):
            cdp.WebSocket("ws://example.com:9222/devtools/page/1")


class DriveAppTests(unittest.TestCase):
    def run_flow(self, states, **kw):
        app = FakeApp(states)
        self.addCleanup(app.stop)
        result = cdp.update_all_through_app(port=app.port, sleep=NOSLEEP, start_timeout=3, finish_timeout=5, **kw)
        return app, result

    def test_presses_update_all_waits_and_closes(self):
        pending = {"found": True, "disabled": False, "label": "Update all (3)"}
        done = {"found": True, "disabled": True, "label": "Update all"}
        app, result = self.run_flow([pending, pending, done, done])
        self.assertTrue(result["clicked"])
        self.assertEqual(app.clicks, 1)
        self.assertTrue(app.closed)
        self.assertIn("Update all (3)", result["message"])

    def test_nothing_to_update_does_not_click(self):
        app, result = self.run_flow([{"found": True, "disabled": True, "label": "Update all"}])
        self.assertFalse(result["clicked"])
        self.assertEqual(app.clicks, 0)
        self.assertTrue(app.closed)

    def test_missing_button_explains_what_to_do(self):
        app = FakeApp([{"found": False}])
        self.addCleanup(app.stop)
        with self.assertRaises(cdp.CdpError) as ctx:
            cdp.update_all_through_app(port=app.port, sleep=NOSLEEP, start_timeout=0.2)
        self.assertIn("My Addons", str(ctx.exception))

    def test_port_closed_explains_launch_option(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with self.assertRaises(cdp.CdpError) as ctx:
            cdp.update_all_through_app(port=port, sleep=NOSLEEP, start_timeout=0.2)
        self.assertIn("--remote-debugging-port=9222", str(ctx.exception))
        self.assertIn("launch options", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
