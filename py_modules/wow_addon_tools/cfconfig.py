# Copyright (c) 2026 ubermoc. All rights reserved. See LICENSE. WoW Addon Updater, https://github.com/ubermoc/Decky-Loader-Wow-mods-update-
# Provenance: wow-addon-updater/2f44d0b2-b3c2-4726-a93d-480b19a8008e
"""Temporarily switch on the CurseForge app's own `dev-mode` setting.

Checked against CurseForge 1.325.0 (Electron 42) by running it: with `--remote-debugging-port`
alone it exposes NO windows over the debugging port. Its windows are created with
`webPreferences.devTools = isDevModeEnabled()`, and that is true when the app's storage file
(`~/.config/CurseForge/storage.json`, a plain JSON object of string values) has `"dev-mode": "true"`.
With that key set, the main window shows up as a debuggable page.

So before launching the app we set the key, and afterwards we put the file back exactly as it
was (removing the key if it wasn't there). This is only done while CurseForge is not running,
and the file's owner and mode are kept (the plugin runs as root, the file belongs to the user).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

KEY = "dev-mode"


class ConfigError(Exception):
    """Show to the user: we couldn't prepare CurseForge."""


def storage_path(home: Path) -> Path:
    # The plugin runs as root, so use the real user's home, not root's XDG_CONFIG_HOME.
    return home / ".config" / "CurseForge" / "storage.json"


def curseforge_running(proc: Path = Path("/proc")) -> bool:
    """True if the CurseForge app (or one of its helper processes) is running."""
    try:
        pids = [p for p in proc.iterdir() if p.name.isdigit()]
    except OSError:
        return False
    for p in pids:
        try:
            exe = (p / "comm").read_text().strip().lower()
            cmd = (p / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace").lower()
        except OSError:
            continue
        if exe == "curseforge" or ("curseforge" in cmd and ("appimage" in cmd or "--type=" in cmd)):
            return True
    return False


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError(
            "CurseForge hasn't been run on this device yet. Open it once from Steam, close it, then try again."
        ) from None
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Couldn't read CurseForge's settings file: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("CurseForge's settings file isn't in the expected format.")
    return data


def _write(path: Path, data: dict[str, Any]) -> None:
    st = path.stat()
    tmp = path.with_name(path.name + ".wowupdater-tmp")
    tmp.write_text(json.dumps(data))
    os.chmod(tmp, st.st_mode & 0o7777)
    if os.geteuid() == 0:
        os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp, path)


def enable_dev_mode(path: Path) -> dict[str, Any]:
    """Set dev-mode and return what is needed to undo it: {"had": bool, "previous": str|None}."""
    data = _read(path)
    undo = {"had": KEY in data, "previous": data.get(KEY)}
    backup = path.with_name(path.name + ".wowupdater-backup")
    if not backup.exists():
        backup.write_text(path.read_text())  # one-time copy of the original, in case anything goes wrong
        if os.geteuid() == 0:
            st = path.stat()
            os.chown(backup, st.st_uid, st.st_gid)
    data[KEY] = "true"
    _write(path, data)
    return undo


def restore_dev_mode(path: Path, undo: Optional[dict[str, Any]]) -> None:
    """Put the file back as it was before enable_dev_mode. Safe to call twice."""
    if not undo:
        return
    try:
        data = _read(path)
    except ConfigError:
        return
    if undo.get("had"):
        data[KEY] = undo.get("previous")
    else:
        data.pop(KEY, None)
    _write(path, data)
