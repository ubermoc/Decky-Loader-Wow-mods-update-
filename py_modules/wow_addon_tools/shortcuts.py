# Copyright (c) 2026 ubermoc. WoW Addon Updater, https://github.com/ubermoc/Decky-Loader-Wow-mods-update-
# Provenance: wow-addon-updater/2f44d0b2-b3c2-4726-a93d-480b19a8008e
"""Read Steam's non-Steam shortcuts (shortcuts.vdf) without ever writing to it.

shortcuts.vdf is Valve's binary KeyValues format. Tokens:

    0x00  nested map   : key\\0 ... entries ... 0x08
    0x01  string       : key\\0 value\\0
    0x02  int32 (LE)   : key\\0 4 bytes
    0x08  end of map

This module only parses. Editing the file is deliberately not supported: a
malformed shortcuts.vdf can wipe a user's shortcuts.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

_MAP, _STR, _INT, _END = 0x00, 0x01, 0x02, 0x08


class VdfError(ValueError):
    """Raised when the binary VDF data is malformed."""


@dataclass(frozen=True)
class Shortcut:
    app_id: int  # unsigned 32-bit id as stored by Steam
    name: str
    exe: str


def _read_cstr(data: bytes, pos: int) -> tuple[str, int]:
    end = data.find(b"\x00", pos)
    if end == -1:
        raise VdfError("unterminated string")
    return data[pos:end].decode("utf-8", errors="replace"), end + 1


def _parse_map(data: bytes, pos: int) -> tuple[dict[str, Any], int]:
    out: dict[str, Any] = {}
    while True:
        if pos >= len(data):
            raise VdfError("unexpected end of data")
        kind = data[pos]
        pos += 1
        if kind == _END:
            return out, pos
        key, pos = _read_cstr(data, pos)
        if kind == _MAP:
            out[key], pos = _parse_map(data, pos)
        elif kind == _STR:
            out[key], pos = _read_cstr(data, pos)
        elif kind == _INT:
            if pos + 4 > len(data):
                raise VdfError("truncated int")
            out[key] = struct.unpack_from("<I", data, pos)[0]
            pos += 4
        else:
            raise VdfError(f"unknown token 0x{kind:02x}")


def parse_binary_vdf(data: bytes) -> dict[str, Any]:
    """Parse a whole binary VDF document into nested dicts."""
    if not data:
        return {}
    root, _ = _parse_map(data + b"\x08" if data[-1:] != b"\x08" else data, 0)
    return root


def _ci_get(mapping: dict[str, Any], name: str, default: Any = None) -> Any:
    """Case-insensitive key lookup (Steam has used both AppName and appname)."""
    lowered = name.lower()
    for key, value in mapping.items():
        if key.lower() == lowered:
            return value
    return default


def iter_shortcuts(data: bytes) -> Iterator[Shortcut]:
    doc = parse_binary_vdf(data)
    shortcuts = _ci_get(doc, "shortcuts", {})
    if not isinstance(shortcuts, dict):
        return
    for entry in shortcuts.values():
        if not isinstance(entry, dict):
            continue
        yield Shortcut(
            app_id=int(_ci_get(entry, "appid", 0) or 0),
            name=str(_ci_get(entry, "AppName", "") or ""),
            exe=str(_ci_get(entry, "Exe", "") or ""),
        )


def load_shortcuts(path: Path) -> list[Shortcut]:
    """Read one shortcuts.vdf. Returns [] if it is missing or unreadable."""
    try:
        return list(iter_shortcuts(path.read_bytes()))
    except (OSError, VdfError):
        return []


def steam_roots(home: Path) -> list[Path]:
    """Likely Steam install roots (native, legacy symlink, Flatpak)."""
    candidates = [
        home / ".local/share/Steam",
        home / ".steam/steam",
        home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
    ]
    seen: list[Path] = []
    resolved: set[Path] = set()
    for cand in candidates:
        try:
            real = cand.resolve()
        except OSError:
            continue
        if cand.is_dir() and real not in resolved:
            resolved.add(real)
            seen.append(cand)
    return seen


def find_all_shortcuts(home: Path) -> list[Shortcut]:
    found: list[Shortcut] = []
    for root in steam_roots(home):
        for vdf in sorted((root / "userdata").glob("*/config/shortcuts.vdf")):
            found.extend(load_shortcuts(vdf))
    return found


def find_curseforge(shortcuts: list[Shortcut]) -> Shortcut | None:
    """First shortcut whose name or executable mentions CurseForge."""
    for sc in shortcuts:
        if "curseforge" in sc.name.lower() or "curseforge" in sc.exe.lower():
            return sc
    return None
