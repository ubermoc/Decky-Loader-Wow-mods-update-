# Copyright (c) 2026 ubermoc. WoW Addon Updater, https://github.com/ubermoc/Decky-Loader-Wow-mods-update-
# Provenance: wow-addon-updater/2f44d0b2-b3c2-4726-a93d-480b19a8008e
"""Find World of Warcraft installs (every game version) inside Steam/Proton prefixes."""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from pathlib import Path

from .shortcuts import steam_roots

# Folder name -> display label. _retail_ and _classic_ are confirmed by Steam
# Deck guides; the rest are Blizzard's usual flavor folder names (not verified
# against a live install). Unknown "_something_" folders get a generated label.
FLAVOR_LABELS = {
    "_retail_": "Retail",
    "_classic_": "Classic",
    "_classic_era_": "Classic Era",
    "_ptr_": "Retail PTR",
    "_xptr_": "Retail PTR 2",
    "_beta_": "Retail Beta",
    "_classic_ptr_": "Classic PTR",
    "_classic_beta_": "Classic Beta",
    "_classic_era_ptr_": "Classic Era PTR",
    "_forever_": "WoW Forever",
}

_FLAVOR_DIR = re.compile(r"^_[a-z0-9_]+_$")
_WOW_SUBPATHS = (
    "pfx/drive_c/Program Files (x86)/World of Warcraft",
    "pfx/drive_c/Program Files/World of Warcraft",
)
_LIBRARY_PATH = re.compile(r'"path"\s+"([^"]+)"')


@dataclass
class WowInstall:
    flavor: str  # folder name, e.g. "_retail_"
    label: str  # display name, e.g. "Retail"
    root: str  # .../World of Warcraft
    addons_path: str  # .../_retail_/Interface/AddOns
    addon_count: int
    addons_modified: float | None  # AddOns dir mtime (epoch seconds)

    def to_dict(self) -> dict:
        return asdict(self)


def _label_for(flavor: str) -> str:
    if flavor in FLAVOR_LABELS:
        return FLAVOR_LABELS[flavor]
    return flavor.strip("_").replace("_", " ").title()


def library_dirs(home: Path) -> list[Path]:
    """Steam library folders (internal drive, SD card, ...), deduplicated."""
    libs: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        try:
            real = path.resolve()
        except OSError:
            return
        if path.is_dir() and real not in seen:
            seen.add(real)
            libs.append(path)

    for root in steam_roots(home):
        add(root)
        vdf = root / "steamapps" / "libraryfolders.vdf"
        try:
            text = vdf.read_text(errors="replace")
        except OSError:
            continue
        for match in _LIBRARY_PATH.findall(text):
            add(Path(match.replace("\\\\", "\\")))
    return libs


def wow_roots(home: Path, extra_paths: list[str] | None = None) -> list[Path]:
    """Every 'World of Warcraft' folder reachable from a Proton prefix."""
    roots: list[Path] = []
    for lib in library_dirs(home):
        compat = lib / "steamapps" / "compatdata"
        if not compat.is_dir():
            continue
        for prefix in sorted(compat.iterdir()):
            for sub in _WOW_SUBPATHS:
                cand = prefix / sub
                if cand.is_dir():
                    roots.append(cand)
    for extra in extra_paths or []:
        cand = Path(extra)
        if cand.is_dir():
            roots.append(cand)
    unique: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        real = root.resolve()
        if real not in seen:
            seen.add(real)
            unique.append(root)
    return unique


def _looks_like_flavor(path: Path) -> bool:
    if (path / "Interface").is_dir():
        return True
    try:
        return any(p.name.lower().startswith("wow") and p.suffix.lower() == ".exe" for p in path.iterdir())
    except OSError:
        return False


def _addons_summary(addons: Path) -> tuple[int, float | None]:
    """Addon folder count and the newest change anywhere that marks an update.

    The AddOns directory's own mtime only moves when a folder is added or removed.
    An in-place update touches the addon's folder or its .toc file instead, so
    take the newest of those too (one level deep, so it stays cheap).
    """
    count = 0
    try:
        newest = addons.stat().st_mtime
        for entry in addons.iterdir():
            if not entry.is_dir():
                continue
            count += 1
            try:
                newest = max(newest, entry.stat().st_mtime)
                for toc in entry.glob("*.toc"):
                    newest = max(newest, toc.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        return count, None
    return count, newest


def scan_root(root: Path) -> list[WowInstall]:
    installs: list[WowInstall] = []
    try:
        children = sorted(root.iterdir())
    except OSError:
        return installs
    for child in children:
        if not (child.is_dir() and _FLAVOR_DIR.match(child.name)):
            continue
        if not _looks_like_flavor(child):
            continue
        addons = child / "Interface" / "AddOns"
        count = 0
        modified: float | None = None
        if addons.is_dir():
            count, modified = _addons_summary(addons)
        installs.append(
            WowInstall(
                flavor=child.name,
                label=_label_for(child.name),
                root=str(root),
                addons_path=str(addons),
                addon_count=count,
                addons_modified=modified,
            )
        )
    return installs


def find_installs(home: Path, extra_paths: list[str] | None = None) -> list[WowInstall]:
    installs: list[WowInstall] = []
    for root in wow_roots(home, extra_paths):
        installs.extend(scan_root(root))
    return installs
