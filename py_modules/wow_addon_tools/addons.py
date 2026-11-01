# Copyright (c) 2026 ubermoc. WoW Addon Updater, https://github.com/ubermoc/Decky-Loader-Wow-mods-update-
# Provenance: wow-addon-updater/2f44d0b2-b3c2-4726-a93d-480b19a8008e
"""Update installed WoW addons straight from CurseForge, the way wowforge-cli does.

wowforge-cli (https://github.com/m-triassi/wowforge-cli) talks to CurseForge's website API
(no API key): list a project's files, pick the newest one that matches the game flavor, download
the zip and unpack it into Interface/AddOns. This module does the same, with two differences:

* Projects are discovered automatically from the `## X-Curse-Project-ID:` line that CurseForge
  packages into addon .toc files, so nothing has to be added by hand.
* Zips are validated and swapped in folder by folder with a rollback, and new files are given
  the same owner as the AddOns folder (the plugin runs as root; the user's Wine prefix must
  stay writable by the user).

These are undocumented website endpoints and can change without notice.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from .updater import USER_AGENT, ssl_context

API = "https://www.curseforge.com/api/v1"
ALLOWED_DOWNLOAD_HOSTS = ("forgecdn.net", "curseforge.com")
MAX_ADDON_ZIP_BYTES = 300 * 1024 * 1024
MAX_UNPACKED_BYTES = 1024 * 1024 * 1024
PAGE_SIZE = 50
MAX_PAGES = 3

RELEASE = 1  # releaseType: 1 release, 2 beta, 3 alpha
STATUS_APPROVED = 4

# CurseForge "game version type" ids for World of Warcraft (from wowforge-cli).
TYPE_RETAIL = 517
TYPE_CLASSIC_ERA = 67408
TYPE_FOREVER = 88568  # "WoW: Forever" (client 1.60.x, product wowf, folder _forever_)

# Folders that are always Retail / Classic Era. Retail is matched by type id (CurseForge tags
# retail files broadly). Everything else is matched by the client version, see FlavorTarget.
_FIXED_FLAVORS = {
    "_forever_": TYPE_FOREVER,
    "_retail_": TYPE_RETAIL,
    "_ptr_": TYPE_RETAIL,
    "_xptr_": TYPE_RETAIL,
    "_beta_": TYPE_RETAIL,
}
# Folders whose product is described in the WoW root's .build.info.
_PRODUCT_FOR_FLAVOR = {
    "_classic_": "wow_classic",
    "_classic_ptr_": "wow_classic_ptr",
    "_classic_beta_": "wow_classic_beta",
    "_classic_era_": "wow_classic_era",
    "_classic_era_ptr_": "wow_classic_era_ptr",
}

_PROJECT_ID = re.compile(r"^\s*##\s*X-Curse-Project-ID\s*:\s*(\d+)", re.IGNORECASE | re.MULTILINE)
_TOC_VERSION = re.compile(r"^\s*##\s*Version\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_WOW_PROCESS = re.compile(r"(^|[\\/ ])wow[a-z]*\.exe", re.IGNORECASE)


class AddonError(Exception):
    """A failure to show to the user for one addon or one run."""


# ---------------------------------------------------------------- flavor / version

def parse_build_info(text: str) -> dict[str, str]:
    """Map product (wow, wow_classic, ...) to its version from Blizzard's .build.info."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return {}
    header = [col.split("!")[0].strip().lower() for col in lines[0].split("|")]
    try:
        product_i, version_i = header.index("product"), header.index("version")
    except ValueError:
        return {}
    out: dict[str, str] = {}
    for line in lines[1:]:
        cols = line.split("|")
        if len(cols) > max(product_i, version_i):
            out[cols[product_i].strip()] = cols[version_i].strip()
    return out


@dataclass(frozen=True)
class FlavorTarget:
    """What a compatible CurseForge file looks like for one WoW folder.

    version_prefix: "major.minor" of the installed client (e.g. "5.5"). CurseForge lists the
        client versions a file supports in `gameVersions` (e.g. "5.5.4"), which is far more
        reliable for the Classic flavors than the shared type ids. Used when known.
    type_id: fallback match on `gameVersionTypeIds` (Retail, or no .build.info).
    """

    type_id: Optional[int] = None
    version_prefix: Optional[str] = None

    def matches(self, file: dict[str, Any]) -> bool:
        if self.version_prefix:
            prefix = self.version_prefix + "."
            return any(str(v).startswith(prefix) or str(v) == self.version_prefix for v in file.get("gameVersions") or [])
        return self.type_id is not None and self.type_id in (file.get("gameVersionTypeIds") or [])


def target_for(flavor: str, wow_root: Path) -> Optional[FlavorTarget]:
    if flavor in _FIXED_FLAVORS:
        return FlavorTarget(type_id=_FIXED_FLAVORS[flavor])
    product = _PRODUCT_FOR_FLAVOR.get(flavor)
    if not product:
        return None
    try:
        version = parse_build_info((wow_root / ".build.info").read_text(errors="replace")).get(product)
    except OSError:
        version = None
    parts = (version or "").split(".")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        if int(parts[0]) >= 10:  # modern retail-line client
            return FlavorTarget(type_id=TYPE_RETAIL)
        return FlavorTarget(version_prefix=f"{int(parts[0])}.{int(parts[1])}")
    if flavor.startswith("_classic_era"):
        return FlavorTarget(type_id=TYPE_CLASSIC_ERA)
    return None


# ---------------------------------------------------------------- discovery

@dataclass
class TrackedAddon:
    project_id: int
    folders: list[str]
    versions: list[str]  # `## Version:` values from the installed .toc files


@dataclass
class Discovery:
    tracked: list[TrackedAddon]
    untracked: list[str]  # folders without a CurseForge id (hand-installed, Blizzard_*, ...)


def discover(addons_dir: Path) -> Discovery:
    """Group the AddOns folders by their CurseForge project id."""
    grouped: dict[int, TrackedAddon] = {}
    untracked: list[str] = []
    try:
        entries = sorted(p for p in addons_dir.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return Discovery([], [])
    for entry in entries:
        project_id: Optional[int] = None
        versions: list[str] = []
        try:
            for toc in sorted(entry.glob("*.toc")):
                with open(toc, "rb") as fh:
                    text = fh.read(65536).decode("utf-8", "replace")
                if project_id is None:
                    match = _PROJECT_ID.search(text)
                    if match:
                        project_id = int(match.group(1))
                version = _TOC_VERSION.search(text)
                if version:
                    versions.append(version.group(1))
        except OSError:
            continue
        if project_id is None:
            untracked.append(entry.name)
            continue
        addon = grouped.setdefault(project_id, TrackedAddon(project_id, [], []))
        addon.folders.append(entry.name)
        addon.versions.extend(versions)
    return Discovery(list(grouped.values()), untracked)


# ---------------------------------------------------------------- choosing a file

def _created(file: dict[str, Any]) -> float:
    try:
        return datetime.fromisoformat(str(file["dateCreated"]).replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        return 0.0


def pick_file(files: list[dict[str, Any]], target: FlavorTarget) -> Optional[dict[str, Any]]:
    """Newest approved *release* file built for this game flavor."""
    best: Optional[dict[str, Any]] = None
    for f in files:
        if f.get("releaseType") != RELEASE:
            continue
        if f.get("status", STATUS_APPROVED) != STATUS_APPROVED:
            continue
        if not target.matches(f):
            continue
        if best is None or _created(f) > _created(best):
            best = f
    return best


def _version_token(version: str) -> str:
    return version.strip().lstrip("#vV").strip()


def needs_update(addon: TrackedAddon, file: dict[str, Any], installed_file_id: Optional[int]) -> bool:
    """A recorded file id wins. Otherwise call it current only if its `## Version:` shows up in
    the release's name (e.g. 'Details.20261015.15339.172'); in doubt, download."""
    if installed_file_id is not None:
        return installed_file_id != file["id"]
    names = f"{file.get('fileName', '')} {file.get('displayName', '')}".lower()
    for version in addon.versions:
        token = _version_token(version).lower()
        if len(token) >= 4 and "@" not in token and token in names:
            return False
    return True


# ---------------------------------------------------------------- network

class CurseForgeClient:
    def __init__(self, timeout: float = 30.0, pause: float = 0.15):
        self.timeout = timeout
        self.pause = pause

    def _open(self, url: str):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        return urllib.request.urlopen(req, timeout=self.timeout, context=ssl_context())

    def files_page(self, project_id: int, page: int) -> list[dict[str, Any]]:
        time.sleep(self.pause)
        url = f"{API}/mods/{int(project_id)}/files?pageSize={PAGE_SIZE}&pageIndex={int(page)}"
        try:
            with self._open(url) as resp:
                data = json.load(resp)
        except Exception as exc:
            raise AddonError(f"Could not list files for project {project_id}: {exc}") from exc
        files = data.get("data") if isinstance(data, dict) else None
        if not isinstance(files, list):
            raise AddonError(f"Unexpected reply listing project {project_id}.")
        return files

    def download(self, project_id: int, file_id: int, dest: Path) -> None:
        url = f"{API}/mods/{int(project_id)}/files/{int(file_id)}/download"
        total = 0
        try:
            with self._open(url) as resp:
                host = urlparse(resp.geturl()).hostname or ""
                if not any(host == h or host.endswith("." + h) for h in ALLOWED_DOWNLOAD_HOSTS):
                    raise AddonError(f"Refusing to download from unexpected host {host!r}.")
                with open(dest, "wb") as out:
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_ADDON_ZIP_BYTES:
                            raise AddonError("Addon download is larger than expected; aborted.")
                        out.write(chunk)
        except AddonError:
            raise
        except Exception as exc:
            raise AddonError(f"Download failed for project {project_id}: {exc}") from exc


def newest_file(client: Any, project_id: int, target: FlavorTarget) -> Optional[dict[str, Any]]:
    """Walk the (newest-first) file pages until one holds a matching release."""
    for page in range(MAX_PAGES):
        files = client.files_page(project_id, page)
        found = pick_file(files, target)
        if found:
            return found
        if len(files) < PAGE_SIZE:
            break
    return None


# ---------------------------------------------------------------- install

def _validate(zf: zipfile.ZipFile) -> list[str]:
    """Return the top-level folder names, or raise if the archive is unsafe."""
    folders: set[str] = set()
    unpacked = 0
    for info in zf.infolist():
        name = info.filename
        parts = Path(name.replace("\\", "/")).parts
        if name.startswith(("/", "\\")) or ".." in parts or (parts and ":" in parts[0]):
            raise AddonError("Addon archive contains an unsafe path.")
        if ((info.external_attr >> 16) & 0o170000) == 0o120000:
            raise AddonError("Addon archive contains a symbolic link.")
        unpacked += info.file_size
        if unpacked > MAX_UNPACKED_BYTES:
            raise AddonError("Addon archive unpacks to more than expected.")
        if len(parts) > 1 or name.endswith("/"):
            folders.add(parts[0])
    if not folders:
        raise AddonError("Addon archive has no addon folder in it.")
    return sorted(folders)


def _chown_tree(root: Path, uid: int, gid: int) -> None:
    os.chown(root, uid, gid)
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            os.chown(os.path.join(dirpath, name), uid, gid, follow_symlinks=False)


def install_zip(zip_path: Path, addons_dir: Path) -> list[str]:
    """Validate, unpack to a staging folder next to AddOns, then swap folders in with rollback."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise AddonError("Downloaded file is not a valid zip.") from exc

    stat = addons_dir.stat()
    with zf, tempfile.TemporaryDirectory(dir=addons_dir, prefix=".wowupdater-") as tmp_str:
        folders = _validate(zf)
        tmp = Path(tmp_str)
        staged, backup = tmp / "staged", tmp / "backup"
        staged.mkdir()
        backup.mkdir()
        zf.extractall(staged)
        if os.geteuid() == 0:
            _chown_tree(staged, stat.st_uid, stat.st_gid)

        moved_in: list[Path] = []
        backed_up: list[tuple[Path, Path]] = []
        try:
            for name in folders:
                source = staged / name
                if not source.is_dir():
                    continue
                target = addons_dir / name
                if target.exists() or target.is_symlink():
                    saved = backup / name
                    shutil.move(str(target), str(saved))
                    backed_up.append((saved, target))
                shutil.move(str(source), str(target))
                moved_in.append(target)
        except Exception as exc:
            for target in moved_in:
                shutil.rmtree(target, ignore_errors=True)
            for saved, target in backed_up:
                if saved.exists():
                    shutil.move(str(saved), str(target))
            raise AddonError(f"Install failed and was rolled back: {exc}") from exc
    return folders


# ---------------------------------------------------------------- running

def wow_running(proc: Path = Path("/proc")) -> bool:
    """True if a WoW client process is running (don't swap addons under it)."""
    try:
        pids = [p for p in proc.iterdir() if p.name.isdigit()]
    except OSError:
        return False
    for p in pids:
        try:
            cmd = (p / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if _WOW_PROCESS.search(cmd):
            return True
    return False


def update_install(
    flavor: str,
    wow_root: Path,
    addons_dir: Path,
    client: Any,
    state: dict[str, int],
) -> dict[str, Any]:
    """Update every CurseForge addon in one AddOns folder.

    `state` maps project id (as str) -> the CurseForge file id this plugin last installed; it is
    updated in place and the caller persists it.
    """
    result: dict[str, Any] = {
        "flavor": flavor,
        "tracked": 0,
        "untracked": 0,
        "updated": [],
        "current": 0,
        "incompatible": 0,  # tracked, but CurseForge has no release for this WoW version
        "failed": [],
        "error": None,
    }
    target = target_for(flavor, wow_root)
    if target is None:
        result["error"] = f"Can't tell which WoW version {flavor} is (no .build.info), so it was skipped."
        return result

    found = discover(addons_dir)
    result["tracked"] = len(found.tracked)
    result["untracked"] = len([n for n in found.untracked if not n.startswith("Blizzard_")])
    for addon in found.tracked:
        key = str(addon.project_id)
        label = addon.folders[0]
        try:
            file = newest_file(client, addon.project_id, target)
            if file is None:
                result["incompatible"] += 1
                continue
            if not needs_update(addon, file, state.get(key)):
                state[key] = file["id"]
                result["current"] += 1
                continue
            with tempfile.TemporaryDirectory() as tmp:
                zip_path = Path(tmp) / "addon.zip"
                client.download(addon.project_id, file["id"], zip_path)
                install_zip(zip_path, addons_dir)
            state[key] = file["id"]
            result["updated"].append(label)
        except AddonError as exc:
            result["failed"].append({"addon": label, "error": str(exc)})
        except Exception as exc:  # one broken addon must not stop the rest
            result["failed"].append({"addon": label, "error": f"Unexpected error: {exc}"})
    return result
