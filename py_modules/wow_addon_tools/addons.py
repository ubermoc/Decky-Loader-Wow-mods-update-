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
TYPE_TBC = 73246
TYPE_CATA = 77522
TYPE_MOP = 79434

_FIXED_FLAVORS = {
    "_retail_": TYPE_RETAIL,
    "_ptr_": TYPE_RETAIL,
    "_xptr_": TYPE_RETAIL,
    "_beta_": TYPE_RETAIL,
    "_classic_era_": TYPE_CLASSIC_ERA,
    "_classic_era_ptr_": TYPE_CLASSIC_ERA,
}
# Folders whose expansion changes over time: read it from the WoW root's .build.info.
_PRODUCT_FOR_FLAVOR = {
    "_classic_": "wow_classic",
    "_classic_ptr_": "wow_classic_ptr",
    "_classic_beta_": "wow_classic_beta",
}

_PROJECT_ID = re.compile(r"^\s*##\s*X-Curse-Project-ID\s*:\s*(\d+)", re.IGNORECASE | re.MULTILINE)
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


def type_for_version(version: str) -> Optional[int]:
    """CurseForge type id for a client version like '5.5.3.65299' (None if unsupported)."""
    try:
        major = int(version.split(".")[0])
    except (ValueError, IndexError):
        return None
    if major >= 10:
        return TYPE_RETAIL
    return {1: TYPE_CLASSIC_ERA, 2: TYPE_TBC, 4: TYPE_CATA, 5: TYPE_MOP}.get(major)


def type_id_for(flavor: str, wow_root: Path) -> Optional[int]:
    if flavor in _FIXED_FLAVORS:
        return _FIXED_FLAVORS[flavor]
    product = _PRODUCT_FOR_FLAVOR.get(flavor)
    if not product:
        return None
    try:
        info = parse_build_info((wow_root / ".build.info").read_text(errors="replace"))
    except OSError:
        return None
    version = info.get(product)
    return type_for_version(version) if version else None


# ---------------------------------------------------------------- discovery

@dataclass
class TrackedAddon:
    project_id: int
    folders: list[str]
    newest_mtime: float  # newest folder/.toc change across its folders


def discover(addons_dir: Path) -> list[TrackedAddon]:
    """Group the AddOns folders by their CurseForge project id."""
    grouped: dict[int, TrackedAddon] = {}
    try:
        entries = sorted(p for p in addons_dir.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []
    for entry in entries:
        project_id: Optional[int] = None
        newest = 0.0
        try:
            newest = entry.stat().st_mtime
            for toc in sorted(entry.glob("*.toc")):
                newest = max(newest, toc.stat().st_mtime)
                if project_id is None:
                    with open(toc, "rb") as fh:
                        match = _PROJECT_ID.search(fh.read(65536).decode("utf-8", "replace"))
                    if match:
                        project_id = int(match.group(1))
        except OSError:
            continue
        if project_id is None:
            continue
        addon = grouped.setdefault(project_id, TrackedAddon(project_id, [], 0.0))
        addon.folders.append(entry.name)
        addon.newest_mtime = max(addon.newest_mtime, newest)
    return list(grouped.values())


# ---------------------------------------------------------------- choosing a file

def _created(file: dict[str, Any]) -> float:
    try:
        return datetime.fromisoformat(str(file["dateCreated"]).replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        return 0.0


def pick_file(files: list[dict[str, Any]], type_id: int) -> Optional[dict[str, Any]]:
    """Newest approved *release* file built for this game flavor."""
    best: Optional[dict[str, Any]] = None
    for f in files:
        if f.get("releaseType") != RELEASE:
            continue
        if f.get("status", STATUS_APPROVED) != STATUS_APPROVED:
            continue
        if type_id not in (f.get("gameVersionTypeIds") or []):
            continue
        if best is None or _created(f) > _created(best):
            best = f
    return best


def needs_update(addon: TrackedAddon, file: dict[str, Any], installed_file_id: Optional[int]) -> bool:
    """Known file id wins. Otherwise assume it is current if it was released before the
    folders were last written (i.e. it was already the newest when it was installed)."""
    if installed_file_id is not None:
        return installed_file_id != file["id"]
    return _created(file) > addon.newest_mtime


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


def newest_file(client: Any, project_id: int, type_id: int) -> Optional[dict[str, Any]]:
    """Walk the (newest-first) file pages until one holds a matching release."""
    for page in range(MAX_PAGES):
        files = client.files_page(project_id, page)
        found = pick_file(files, type_id)
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
        "updated": [],
        "failed": [],
        "error": None,
    }
    type_id = type_id_for(flavor, wow_root)
    if type_id is None:
        result["error"] = f"Can't tell which WoW version {flavor} is, so it was skipped."
        return result

    addons = discover(addons_dir)
    result["tracked"] = len(addons)
    for addon in addons:
        key = str(addon.project_id)
        label = addon.folders[0]
        try:
            file = newest_file(client, addon.project_id, type_id)
            if file is None or not needs_update(addon, file, state.get(key)):
                if file is not None:
                    state.setdefault(key, file["id"])
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
