# Copyright (c) 2026 ubermoc. All rights reserved. See LICENSE. WoW Addon Updater, https://github.com/ubermoc/Decky-Loader-Wow-mods-update-
# Provenance: wow-addon-updater/2f44d0b2-b3c2-4726-a93d-480b19a8008e
"""Drive the CurseForge desktop app through Chromium's remote debugging port.

The CurseForge app is an Electron app. Started with `--remote-debugging-port=9222` it serves
the Chrome DevTools Protocol on localhost, which lets us run a little JavaScript in its window,
e.g. press its own "Update all" button, then close it.

Only the standard library is used (Decky's Python has no websocket package), so this file
contains a minimal WebSocket client. It only ever talks to 127.0.0.1.

Facts about the app (CurseForge 1.325.0, Electron 42). Confirmed by running it: the debugging port
answers, but only exposes the window when the app's `dev-mode` setting is on (see cfconfig.py);
my WebSocket client works against it; clicking WoW on the home screen goes to
`#/game/1/my-library/my-addons`. Read from its code but NOT yet seen live (the test machine had no
WoW install): the My Addons screen renders a button with class `update-all`, aria-label
"Update all (N)", which gets class `disabled` when there is nothing to update.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import time
import urllib.request
from typing import Any, Callable, Optional

HOST = "127.0.0.1"
DEFAULT_PORT = 9222
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

FIND_JS = """(() => {
  const el = document.querySelector('.update-all');
  if (!el) return {found: false};
  const label = (el.getAttribute('aria-label') || el.textContent || '').trim();
  const disabled = el.classList.contains('disabled') || el.disabled === true
    || el.getAttribute('aria-disabled') === 'true';
  return {found: true, disabled: disabled, label: label};
})()"""

CLICK_JS = """(() => {
  const el = document.querySelector('.update-all');
  if (!el) return false;
  el.click();
  return true;
})()"""


# Clicking "World of Warcraft" on the home screen lands on this route (checked in the real app).
NAVIGATE_JS = """(() => {
  if (!location.hash.includes('/my-library/my-addons')) location.hash = '#/game/1/my-library/my-addons';
  return location.hash;
})()"""


class CdpError(Exception):
    """Anything that should be shown to the user as a failed attempt to drive the app."""


# ---------------------------------------------------------------- websocket

def _accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()


def encode_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """A masked client frame (clients must mask)."""
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 65536:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    mask = os.urandom(4)
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


class WebSocket:
    def __init__(self, url: str, timeout: float = 10.0):
        if not url.startswith("ws://" + HOST) and not url.startswith("ws://localhost"):
            raise CdpError("Refusing to connect to anything but localhost.")
        rest = url[len("ws://"):]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            (
                f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CdpError("The app closed the debugging connection.")
            header += chunk
        head, _, self._buf = header.partition(b"\r\n\r\n")
        text = head.decode("latin-1")
        if " 101 " not in text.split("\r\n")[0]:
            raise CdpError(f"Debugging connection refused: {text.splitlines()[0]}")
        if _accept_key(key).lower() not in text.lower():
            raise CdpError("Debugging connection failed its handshake check.")

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise CdpError("The app closed the debugging connection.")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send_text(self, text: str) -> None:
        self.sock.sendall(encode_frame(text.encode()))

    def recv_text(self) -> str:
        message = b""
        while True:
            b1, b2 = self._read(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            if n > 64 * 1024 * 1024:
                raise CdpError("Unexpectedly large reply from the app.")
            payload = self._read(n)  # server frames are not masked
            if opcode == 0x8:
                raise CdpError("The app closed the debugging connection.")
            if opcode == 0x9:
                self.sock.sendall(encode_frame(payload, 0xA))
                continue
            if opcode == 0xA:
                continue
            message += payload
            if fin:
                return message.decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class Cdp:
    """One DevTools session: send a command, wait for its reply, ignore events."""

    def __init__(self, ws_url: str, timeout: float = 15.0):
        self.ws = WebSocket(ws_url, timeout)
        self._id = 0

    def call(self, method: str, params: Optional[dict] = None) -> dict[str, Any]:
        self._id += 1
        self.ws.send_text(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv_text())
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise CdpError(f"{method}: {msg['error'].get('message', msg['error'])}")
                return msg.get("result", {})

    def evaluate(self, expression: str) -> Any:
        res = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        if "exceptionDetails" in res:
            raise CdpError("The page raised an error while running our script.")
        return res.get("result", {}).get("value")

    def close(self) -> None:
        self.ws.close()


# ---------------------------------------------------------------- the app

def http_json(path: str, port: int, timeout: float = 3.0) -> Any:
    with urllib.request.urlopen(f"http://{HOST}:{port}{path}", timeout=timeout) as resp:
        return json.load(resp)


def wait_for_port(port: int, timeout: float, sleep: Callable[[float], None] = time.sleep) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            http_json("/json/version", port)
            return
        except Exception:
            if time.monotonic() >= deadline:
                raise CdpError(
                    "Couldn't reach CurseForge's debugging port. The plugin passes --remote-debugging-port=9222 "
                    "when it launches CurseForge; if Steam ignored that, add it to the CurseForge shortcut's "
                    "launch options yourself (keep any existing options), then try again."
                ) from None
            sleep(1.0)


def find_update_button(port: int) -> Optional[tuple[Cdp, dict[str, Any]]]:
    """Open each page of the app and return (session, button state) for the one that has it."""
    for target in http_json("/json", port):
        if target.get("type") != "page" or not target.get("webSocketDebuggerUrl"):
            continue
        try:
            cdp = Cdp(target["webSocketDebuggerUrl"])
            cdp.evaluate(NAVIGATE_JS)
            state = cdp.evaluate(FIND_JS)
        except (CdpError, OSError, ValueError):
            continue
        if state and state.get("found"):
            return cdp, state
        cdp.close()
    return None


def _has_count(label: str) -> bool:
    return any(ch.isdigit() and ch != "0" for ch in label)


def close_app(port: int = DEFAULT_PORT) -> bool:
    """Ask the app to quit through DevTools. True if the request was delivered."""
    try:
        version = http_json("/json/version", port)
        browser = Cdp(version["webSocketDebuggerUrl"])
    except Exception:
        return False
    try:
        browser.call("Browser.close")
    except (CdpError, OSError):
        pass  # the app often drops the connection while closing
    browser.close()
    return True


def update_all_through_app(
    port: int = DEFAULT_PORT,
    start_timeout: float = 90.0,
    finish_timeout: float = 20 * 60.0,
    poll: float = 3.0,
    sleep: Callable[[float], None] = time.sleep,
    close_when_done: bool = True,
) -> dict[str, Any]:
    """Press "Update all" in the running CurseForge app, wait for it to finish, close the app.

    The app is closed on failure too, so it doesn't linger in the background.
    """
    wait_for_port(port, start_timeout, sleep)  # nothing to close if it never came up
    try:
        result = _press_update_all(port, start_timeout, finish_timeout, poll, sleep)
    except BaseException:
        if close_when_done:
            close_app(port)
        raise
    if close_when_done and not close_app(port):
        result["message"] += " (Couldn't close CurseForge by itself.)"
    return result


def _press_update_all(port: int, start_timeout: float, finish_timeout: float, poll: float, sleep: Callable[[float], None]) -> dict[str, Any]:
    deadline = time.monotonic() + start_timeout
    found = find_update_button(port)
    while found is None:
        if time.monotonic() >= deadline:
            raise CdpError(
                "CurseForge opened but its \"Update all\" button wasn't on screen. Make sure World of Warcraft is "
                "added in CurseForge (Add a Game / Scan Now) and has addons, then try again."
            )
        sleep(poll)
        found = find_update_button(port)

    cdp, state = found
    result: dict[str, Any] = {"ok": True, "clicked": False, "label": state.get("label", ""), "message": ""}
    try:
        if state.get("disabled") or not _has_count(state.get("label", "")):
            result["message"] = "CurseForge says nothing needs updating."
        else:
            if not cdp.evaluate(CLICK_JS):
                raise CdpError("Couldn't press \"Update all\".")
            result["clicked"] = True
            # Done when the button is disabled / has no count for two checks in a row.
            quiet = 0
            deadline = time.monotonic() + finish_timeout
            while quiet < 2:
                if time.monotonic() >= deadline:
                    raise CdpError("CurseForge was still updating after 20 minutes; closed it.")
                sleep(poll)
                now = cdp.evaluate(FIND_JS) or {}
                finished = (not now.get("found")) or now.get("disabled") or not _has_count(now.get("label", ""))
                quiet = quiet + 1 if finished else 0
            result["message"] = f"Pressed \"{result['label']}\" and CurseForge finished."
    finally:
        cdp.close()
    return result
