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
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                raise CdpError("CurseForge didn't answer in time (its window may still be starting).") from None
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


# ---------------------------------------------------------------- rows (per-addon actions)
#
# The real page markup has not been seen (only the toolbar button's class name, read from the
# app's code, and a photo of the screen). So rows are found by what is visible on screen: each
# row has a text button reading "Update" or "Up to date". A row is the largest ancestor that
# contains exactly one such button; it belongs to an addon when its text contains one of the
# addon's names (folder name or .toc title, letters and digits only, case-insensitive).

_ROW_HELPERS = r"""
const norm = s => (s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
const ACTIONS = ['update', 'up to date', 'updating', 'updating...', 'installing', 'installed', 'updated'];
const textOf = e => (e.textContent || '').replace(/\s+/g, ' ').trim();
const isAction = e => ACTIONS.includes(textOf(e).toLowerCase());
const actions = [...document.querySelectorAll('button, a, [role=button], div, span, td')]
  .filter(e => isAction(e) && ![...e.children].some(isAction));
const rowOf = el => {
  let row = el;
  while (row.parentElement) {
    const p = row.parentElement;
    if (actions.filter(a => p.contains(a)).length > 1) break;
    row = p;
  }
  return row;
};
"""


def row_find_js(names: list[str], click: bool) -> str:
    """Find the row for an addon by name; optionally press its Update button."""
    return (
        "(() => { /*ROW_FIND*/ " + _ROW_HELPERS
        + f"const NAMES = {json.dumps(names)}.map(norm).filter(n => n.length >= 3);"
        + f"const CLICK = {'true' if click else 'false'};"
        + r"""
  const hits = [];
  for (const a of actions) {
    const row = rowOf(a);
    const rn = norm(textOf(row));
    const best = Math.max(0, ...NAMES.map(n => (rn.includes(n) ? n.length : 0)));
    if (best) hits.push({a, row, best});
  }
  if (!hits.length) return {found: false, rows: actions.length};
  hits.sort((x, y) => y.best - x.best);
  const h = hits[0];
  const action = textOf(h.a).toLowerCase();
  let clicked = false;
  if (CLICK && action === 'update') { (h.a.closest('button, [role=button], a') || h.a).click(); clicked = true; }
  return {found: true, matches: hits.length, action: action, rowText: textOf(h.row).slice(0, 160), clicked: clicked};
})()"""
    )


ROWS_COUNT_JS = "(() => { /*ROW_COUNT*/ " + _ROW_HELPERS + " return actions.length; })()"

DUMP_JS = (
    "(() => { /*ROW_DUMP*/ "
    + _ROW_HELPERS
    + r"""
  const trim = (s, n) => (s || '').slice(0, n);
  const btn = document.querySelector('.update-all');
  return {
    hash: location.hash,
    title: document.title,
    updateAll: btn ? {tag: btn.tagName, cls: btn.className, label: btn.getAttribute('aria-label'), text: textOf(btn),
                      html: trim(btn.parentElement ? btn.parentElement.outerHTML : btn.outerHTML, 1500)} : null,
    actionCount: actions.length,
    rows: actions.slice(0, 12).map(a => {
      const row = rowOf(a);
      return {action: textOf(a), tag: a.tagName, cls: a.className, rowTag: row.tagName, rowCls: row.className,
              rowText: trim(textOf(row), 200), rowHtml: trim(row.outerHTML, 1400)};
    }),
    flavorPicker: trim(textOf(document.body).slice(0, 600), 600)
  };
})()"""
)


class _Page:
    """The app's main window. If a call times out (the window is busy, e.g. still starting) the
    connection is dropped and re-opened, because a half-read reply can't be resumed safely."""

    def __init__(self, port: int, sleep: Callable[[float], None], timeout: float):
        self.port, self.sleep, self.timeout = port, sleep, timeout
        self.session: Optional[Cdp] = None
        self._reconnect()

    def _reconnect(self) -> None:
        self.close()
        self.session = _open_page(self.port, self.sleep, self.timeout)

    def evaluate(self, expression: str, attempts: int = 3) -> Any:
        last: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                assert self.session is not None
                return self.session.evaluate(expression)
            except (CdpError, OSError, AssertionError) as exc:
                last = exc
                if attempt + 1 < attempts:
                    self.sleep(2.0)
                    self._reconnect()
        raise CdpError(str(last) if isinstance(last, CdpError) else "CurseForge's window stopped answering.")

    def close(self) -> None:
        if self.session is not None:
            self.session.close()
            self.session = None


def _open_page(port: int, sleep: Callable[[float], None], timeout: float) -> Cdp:
    """Connect to the app's main window and go to WoW's My Addons."""
    deadline = time.monotonic() + timeout
    while True:
        for target in http_json("/json", port):
            if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                try:
                    session = Cdp(target["webSocketDebuggerUrl"])
                    session.evaluate(NAVIGATE_JS)
                    return session
                except (CdpError, OSError, ValueError):
                    continue
        if time.monotonic() >= deadline:
            raise CdpError(
                "CurseForge started but its window isn't reachable. Close CurseForge completely and try again."
            )
        sleep(2.0)


def _wait_for_rows(session: _Page, timeout: float, poll: float, sleep: Callable[[float], None]) -> int:
    deadline = time.monotonic() + timeout
    while True:
        count = session.evaluate(ROWS_COUNT_JS) or 0
        if count:
            return int(count)
        if time.monotonic() >= deadline:
            raise CdpError(
                "CurseForge opened but its addon list never appeared. Make sure World of Warcraft is added in "
                "CurseForge (Add a Game / Scan Now) and has addons, then try again."
            )
        sleep(poll)


def dump_my_addons(
    port: int = DEFAULT_PORT,
    start_timeout: float = 90.0,
    poll: float = 3.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Read the structure of the My Addons screen (for development), then close the app."""
    wait_for_port(port, start_timeout, sleep)
    try:
        session = _Page(port, sleep, start_timeout)
        try:
            _wait_for_rows(session, start_timeout, poll, sleep)
            sleep(2.0)  # let the list finish rendering
            return session.evaluate(DUMP_JS) or {}
        finally:
            session.close()
    finally:
        close_app(port)


def update_row_through_app(
    names: list[str],
    port: int = DEFAULT_PORT,
    start_timeout: float = 90.0,
    finish_timeout: float = 10 * 60.0,
    poll: float = 3.0,
    sleep: Callable[[float], None] = time.sleep,
    find_timeout: float = 15.0,
) -> dict[str, Any]:
    """Press the Update button on one addon's row in the running app, wait, then close the app."""
    wait_for_port(port, start_timeout, sleep)
    try:
        session = _Page(port, sleep, start_timeout)
        try:
            _wait_for_rows(session, start_timeout, poll, sleep)
            deadline = time.monotonic() + find_timeout
            state = session.evaluate(row_find_js(names, False)) or {}
            while not state.get("found") and time.monotonic() < deadline:
                sleep(poll)
                state = session.evaluate(row_find_js(names, False)) or {}
            if not state.get("found"):
                raise CdpError(
                    f"Couldn't find {names[0]} in CurseForge's My Addons list ({state.get('rows', 0)} rows seen). "
                    "CurseForge shows the WoW version you used last; switch it to the right one inside CurseForge."
                )
            if state.get("action") != "update":
                return {"ok": True, "clicked": False, "message": f"CurseForge says {names[0]} is already up to date."}
            if not (session.evaluate(row_find_js(names, True)) or {}).get("clicked"):
                raise CdpError(f"Couldn't press Update for {names[0]}.")
            quiet = 0
            deadline = time.monotonic() + finish_timeout
            while quiet < 2:
                if time.monotonic() >= deadline:
                    raise CdpError(f"CurseForge was still updating {names[0]} after 10 minutes; closed it.")
                sleep(poll)
                now = session.evaluate(row_find_js(names, False)) or {}
                done = (not now.get("found")) or now.get("action") not in ("update", "updating", "updating...", "installing")
                quiet = quiet + 1 if done else 0
            return {"ok": True, "clicked": True, "message": f"Updated {names[0]} through CurseForge."}
        finally:
            session.close()
    finally:
        close_app(port)
