import os
import sys
from pathlib import Path

import decky

# Decky normally puts py_modules on sys.path; do it explicitly so a manual
# install behaves the same.
_PY_MODULES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "py_modules")
if _PY_MODULES not in sys.path:
    sys.path.insert(0, _PY_MODULES)

import asyncio  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402

from wow_addon_tools import updater  # noqa: E402
from wow_addon_tools.shortcuts import find_all_shortcuts, find_curseforge  # noqa: E402
from wow_addon_tools.wow import find_installs  # noqa: E402

_PLUGIN_DIR = Path(os.path.dirname(os.path.abspath(__file__)))


def _home() -> Path:
    # DECKY_USER_HOME is the real user's home even if the backend runs elevated.
    return Path(getattr(decky, "DECKY_USER_HOME", None) or os.path.expanduser("~"))


class Plugin:
    async def scan(self) -> dict:
        """Read-only snapshot: is CurseForge in Steam, and which WoW versions exist."""
        home = _home()
        try:
            curseforge = find_curseforge(find_all_shortcuts(home))
            installs = find_installs(home)
        except Exception as exc:  # never let a scan crash the plugin
            decky.logger.exception("scan failed")
            return {"ok": False, "error": str(exc), "curseforge": None, "installs": []}

        return {
            "ok": True,
            "error": None,
            "curseforge": (
                {"app_id": curseforge.app_id, "name": curseforge.name, "exe": curseforge.exe}
                if curseforge
                else None
            ),
            "installs": [i.to_dict() for i in installs],
        }

    async def get_update_info(self) -> dict:
        """Installed vs latest build. Network errors are reported, not raised."""
        installed = updater.read_installed(_PLUGIN_DIR)
        loop = asyncio.get_running_loop()
        try:
            latest = await loop.run_in_executor(None, updater.fetch_latest)
        except updater.UpdateError as exc:
            return {"installed": installed, "latest": None, "update_available": False, "error": str(exc)}
        return {
            "installed": installed,
            "latest": {"version": latest["version"], "build": latest["build"]},
            "update_available": bool(latest["asset_url"])
            and updater.update_available(installed["build"], latest["build"]),
            "error": None,
        }

    async def install_update(self) -> dict:
        """Download the latest release zip and swap it into this plugin's folder."""
        loop = asyncio.get_running_loop()

        def work() -> dict:
            latest = updater.fetch_latest()
            if not latest["asset_url"]:
                raise updater.UpdateError("The latest release has no downloadable zip.")
            with tempfile.TemporaryDirectory() as tmp:
                zip_path = Path(tmp) / "update.zip"
                updater.download(latest["asset_url"], zip_path)
                updater.install_zip(zip_path, _PLUGIN_DIR)
            return {"version": latest["version"], "build": latest["build"]}

        try:
            installed = await loop.run_in_executor(None, work)
        except updater.UpdateError as exc:
            decky.logger.error(f"update failed: {exc}")
            return {"ok": False, "error": str(exc), "installed": None}
        except Exception as exc:
            decky.logger.exception("update crashed")
            return {"ok": False, "error": f"Unexpected error: {exc}", "installed": None}
        decky.logger.info(f"installed update {installed}")
        # Restart Decky shortly so the new backend and frontend are loaded
        # (same approach as the Ubermoc Speech plugin).
        subprocess.Popen(
            ["sh", "-c", "sleep 3; systemctl restart plugin_loader"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return {"ok": True, "error": None, "installed": installed}

    async def _main(self):
        decky.logger.info("WoW Addon Updater loaded")

    async def _unload(self):
        decky.logger.info("WoW Addon Updater unloaded")
