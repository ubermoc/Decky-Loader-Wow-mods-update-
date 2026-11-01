# Copyright (c) 2026 ubermoc. All rights reserved. See LICENSE. WoW Addon Updater, https://github.com/ubermoc/Decky-Loader-Wow-mods-update-
# Provenance: wow-addon-updater/2f44d0b2-b3c2-4726-a93d-480b19a8008e
"""CurseForge project IDs typed in by the user for addons whose .toc has no X-Curse-Project-ID.

Stored in the plugin's own settings folder (never in the addon files) as
{ "<AddOns path>": { "<addon folder>": <project id> } }, so the same folder name in two
WoW versions can have different IDs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

FILE = "project_ids.json"
MAX_ID = 99_999_999


def parse_project_id(text: object) -> int:
    """Turn what was typed into a project id, or raise ValueError with a message for the user."""
    cleaned = str(text).strip()
    if not cleaned:
        raise ValueError("Type the project ID first.")
    if not cleaned.isdigit():
        raise ValueError("A project ID is just a number, like 1234567.")
    value = int(cleaned)
    if value < 1 or value > MAX_ID:
        raise ValueError("That doesn't look like a CurseForge project ID.")
    return value


def _load(settings_dir: Path) -> dict[str, dict[str, int]]:
    try:
        data = json.loads((settings_dir / FILE).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    clean: dict[str, dict[str, int]] = {}
    for path, folders in data.items():
        if isinstance(folders, dict):
            clean[str(path)] = {str(f): int(i) for f, i in folders.items() if isinstance(i, int) and i > 0}
    return clean


def _save(settings_dir: Path, data: dict[str, dict[str, int]]) -> None:
    path = settings_dir / FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def get(settings_dir: Path, addons_path: str) -> dict[str, int]:
    return dict(_load(settings_dir).get(addons_path, {}))


def set_id(settings_dir: Path, addons_path: str, folder: str, project_id: int) -> None:
    data = _load(settings_dir)
    data.setdefault(addons_path, {})[folder] = int(project_id)
    _save(settings_dir, data)


def clear_id(settings_dir: Path, addons_path: str, folder: str) -> bool:
    """Remove one saved ID. True if there was one."""
    data = _load(settings_dir)
    if folder not in data.get(addons_path, {}):
        return False
    del data[addons_path][folder]
    if not data[addons_path]:
        del data[addons_path]
    _save(settings_dir, data)
    return True
