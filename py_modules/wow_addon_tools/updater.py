"""Self-update: compare the installed build with the latest GitHub release and install it.

The release is published by .github/workflows/build.yml with notes like
"Version 0.0.2 build abc1234". The installed plugin knows its own VERSION and
BUILD (git short hash) from files that scripts/package.sh writes into the zip.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Optional

REPO = "ubermoc/Decky-Loader-Wow-mods-update-"
API_URL = f"https://api.github.com/repos/{REPO}/releases/tags/latest"
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"
PLUGIN_FOLDER = "WoW Addon Updater"
USER_AGENT = "wow-addon-updater-decky-plugin"
MAX_ZIP_BYTES = 50 * 1024 * 1024
REQUIRED_FILES = ("plugin.json", "main.py", "dist/index.js")

_NOTES_NEW = re.compile(r"Version\s+(\S+)\s+build\s+([0-9a-f]{7,40})", re.IGNORECASE)
_NOTES_OLD = re.compile(r"Built from\s+([0-9a-f]{7,40})", re.IGNORECASE)


class UpdateError(Exception):
    """Anything that should be shown to the user as a failed update."""


def read_installed(plugin_dir: Path) -> dict[str, str]:
    def read(name: str, default: str) -> str:
        try:
            return (plugin_dir / name).read_text().strip() or default
        except OSError:
            return default

    return {"version": read("VERSION", "unknown"), "build": read("BUILD", "dev")}


def parse_release(data: dict[str, Any]) -> dict[str, Optional[str]]:
    """Pull version, build hash and zip URL out of a GitHub release payload."""
    body = str(data.get("body") or "")
    version: Optional[str] = None
    build: Optional[str] = None
    match = _NOTES_NEW.search(body)
    if match:
        version, build = match.group(1), match.group(2).lower()
    else:
        old = _NOTES_OLD.search(body)
        if old:
            build = old.group(1).lower()

    asset_url: Optional[str] = None
    for asset in data.get("assets") or []:
        url = str(asset.get("browser_download_url") or "")
        if str(asset.get("name", "")).endswith(".zip") and url.startswith(DOWNLOAD_PREFIX):
            asset_url = url
            break
    return {"version": version, "build": build, "asset_url": asset_url}


def update_available(installed_build: str, latest_build: Optional[str]) -> bool:
    if not latest_build:
        return False
    a, b = installed_build.lower(), latest_build.lower()
    n = min(len(a), len(b))
    return a[:n] != b[:n] if n >= 7 else True


def fetch_latest(timeout: float = 15.0) -> dict[str, Optional[str]]:
    req = urllib.request.Request(API_URL, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return parse_release(json.load(resp))
    except Exception as exc:
        raise UpdateError(f"Could not check GitHub for updates: {exc}") from exc


def download(url: str, dest: Path, timeout: float = 60.0) -> None:
    if not url.startswith(DOWNLOAD_PREFIX):
        raise UpdateError("Refusing to download from an unexpected address.")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    total = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as out:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ZIP_BYTES:
                    raise UpdateError("Download is larger than expected; aborted.")
                out.write(chunk)
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError(f"Download failed: {exc}") from exc


def _validate_zip(zf: zipfile.ZipFile) -> None:
    prefix = PLUGIN_FOLDER + "/"
    names = set()
    for info in zf.infolist():
        name = info.filename
        parts = Path(name).parts
        if name.startswith("/") or ".." in parts:
            raise UpdateError("Update archive contains an unsafe path.")
        if not name.startswith(prefix):
            raise UpdateError("Update archive has an unexpected layout.")
        if ((info.external_attr >> 16) & 0o170000) == 0o120000:
            raise UpdateError("Update archive contains a symbolic link.")
        names.add(name)
    for required in REQUIRED_FILES:
        if prefix + required not in names:
            raise UpdateError(f"Update archive is missing {required}.")


def install_zip(zip_path: Path, plugin_dir: Path) -> None:
    """Validate the archive, then swap its files into plugin_dir, rolling back on failure."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise UpdateError("Downloaded file is not a valid zip.") from exc

    with zf, tempfile.TemporaryDirectory() as tmp_str:
        _validate_zip(zf)
        tmp = Path(tmp_str)
        staged = tmp / "staged"
        zf.extractall(staged)
        source = staged / PLUGIN_FOLDER
        backup = tmp / "backup"
        backup.mkdir()

        moved_in: list[Path] = []
        backed_up: list[tuple[Path, Path]] = []
        try:
            for item in sorted(source.iterdir()):
                target = plugin_dir / item.name
                if target.exists() or target.is_symlink():
                    saved = backup / item.name
                    shutil.move(str(target), str(saved))
                    backed_up.append((saved, target))
                shutil.move(str(item), str(target))
                moved_in.append(target)
        except Exception as exc:
            for target in moved_in:
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink(missing_ok=True)
            for saved, target in backed_up:
                if saved.exists():
                    shutil.move(str(saved), str(target))
            if isinstance(exc, PermissionError):
                raise UpdateError(
                    "No permission to write the plugin folder. Make sure plugin.json has the \"root\" flag."
                ) from exc
            raise UpdateError(f"Install failed and was rolled back: {exc}") from exc
