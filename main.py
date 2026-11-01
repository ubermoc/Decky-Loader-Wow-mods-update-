# Copyright (c) 2026 ubermoc. All rights reserved. See LICENSE. WoW Addon Updater, https://github.com/ubermoc/Decky-Loader-Wow-mods-update-
# Provenance: wow-addon-updater/2f44d0b2-b3c2-4726-a93d-480b19a8008e
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
import json  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from wow_addon_tools import PROVENANCE, addons, cdp, cfconfig, updater  # noqa: E402
from wow_addon_tools.shortcuts import find_all_shortcuts, find_curseforge  # noqa: E402
from wow_addon_tools.wow import find_installs  # noqa: E402

_PLUGIN_DIR = Path(os.path.dirname(os.path.abspath(__file__)))


def _home() -> Path:
    # DECKY_USER_HOME is the real user's home even if the backend runs elevated.
    return Path(getattr(decky, "DECKY_USER_HOME", None) or os.path.expanduser("~"))


def _settings_dir() -> Path:
    path = Path(getattr(decky, "DECKY_PLUGIN_SETTINGS_DIR", None) or (_PLUGIN_DIR / "settings"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_json(name: str, default):
    try:
        data = json.loads((_settings_dir() / name).read_text())
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def _save_json(name: str, data) -> None:
    path = _settings_dir() / name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


AUTO_FIRST_DELAY = 120  # seconds after the plugin loads
AUTO_INTERVAL = 6 * 60 * 60
AUTO_RETRY_WHEN_WOW_RUNNING = 30 * 60

_run_lock = threading.Lock()
_last_run: dict = {}


def _run_addon_updates(only_path: str = "") -> dict:
    """Blocking: update every tracked CurseForge addon (or one AddOns folder)."""
    if not _run_lock.acquire(blocking=False):
        return {"ok": False, "error": "An addon update is already running.", "results": []}
    try:
        if addons.wow_running():
            return {"ok": False, "error": "WoW is running. Close it first so addons aren't swapped under it.", "results": []}
        state = _load_json("state.json", {})
        client = addons.CurseForgeClient()
        results = []
        for install in find_installs(_home()):
            if only_path and install.addons_path != only_path:
                continue
            addons_dir = Path(install.addons_path)
            if not addons_dir.is_dir():
                continue
            per_install = state.setdefault(install.addons_path, {})
            result = addons.update_install(install.flavor, Path(install.root), addons_dir, client, per_install)
            result["label"] = install.label
            result["addons_path"] = install.addons_path
            results.append(result)
            _save_json("state.json", state)
        run = {"ok": True, "error": None, "results": results, "finished": time.time()}
        _last_run.clear()
        _last_run.update(run)
        if not results:
            decky.logger.info("addon update: no WoW installs found")
        for r in results:
            decky.logger.info(
                f"addon update {r['label']} ({r['flavor']}): tracked={r['tracked']} updated={len(r['updated'])} "
                f"current={r['current']} no_release={r['incompatible']} no_curse_id={r['untracked']} "
                f"failed={len(r['failed'])} error={r['error']}"
            )
            for failure in r["failed"][:10]:
                decky.logger.warning(f"addon update {r['label']}: {failure['addon']}: {failure['error']}")
        return run
    except Exception as exc:
        decky.logger.exception("addon update crashed")
        return {"ok": False, "error": f"Unexpected error: {exc}", "results": []}
    finally:
        _run_lock.release()


def _restore_pending_dev_mode() -> None:
    """Undo a dev-mode change left behind (crash, plugin reload) once CurseForge is closed."""
    pending = _load_json("devmode_undo.json", {})
    if not pending or cfconfig.curseforge_running():
        return
    try:
        cfconfig.restore_dev_mode(cfconfig.storage_path(_home()), pending)
        _save_json("devmode_undo.json", {})
        decky.logger.info("restored CurseForge's dev-mode setting")
    except Exception:
        decky.logger.exception("could not restore CurseForge's dev-mode setting")


def _prepare_curseforge_app() -> dict:
    """Turn on CurseForge's dev-mode so its window is reachable over the debugging port."""
    if addons.wow_running():
        return {"ok": False, "error": "WoW is running. Close it first so addons aren't swapped under it."}
    if cfconfig.curseforge_running():
        return {
            "ok": False,
            "error": "CurseForge is already open. Close it first: it has to start fresh to allow remote control.",
        }
    try:
        undo = cfconfig.enable_dev_mode(cfconfig.storage_path(_home()))
    except cfconfig.ConfigError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        decky.logger.exception("could not prepare CurseForge")
        return {"ok": False, "error": f"Unexpected error: {exc}"}
    _save_json("devmode_undo.json", undo)
    return {"ok": True, "error": None}


def _drive_curseforge_app() -> dict:
    """Blocking: press "Update all" in the already-launched CurseForge app, then close it."""
    try:
        result = cdp.update_all_through_app()
    except cdp.CdpError as exc:
        decky.logger.warning(f"curseforge app: {exc}")
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        decky.logger.exception("curseforge app drive crashed")
        return {"ok": False, "error": f"Unexpected error: {exc}"}
    finally:
        # Let the app finish closing, then put its settings file back as it was.
        for _ in range(15):
            if not cfconfig.curseforge_running():
                break
            time.sleep(1)
        _restore_pending_dev_mode()
    decky.logger.info(f"curseforge app: {result}")
    return {"ok": True, "error": None, "clicked": result["clicked"], "message": result["message"]}


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
                {
                    "app_id": curseforge.app_id,
                    "name": curseforge.name,
                    "exe": curseforge.exe,
                    "launch_options": curseforge.launch_options,
                }
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

    async def update_addons(self, addons_path: str = "") -> dict:
        """Update CurseForge addons now: all WoW versions, or just one AddOns folder."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _run_addon_updates, addons_path)

    async def prepare_curseforge_app(self) -> dict:
        """Call before launching CurseForge for update_via_curseforge_app."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _prepare_curseforge_app)

    async def update_via_curseforge_app(self) -> dict:
        """Call after launching CurseForge (with --remote-debugging-port=9222 in its launch options)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _drive_curseforge_app)

    async def get_addon_settings(self) -> dict:
        settings = _load_json("settings.json", {})
        return {"auto_update": bool(settings.get("auto_update", True)), "last_run": dict(_last_run) or None}

    async def set_auto_update(self, enabled: bool) -> dict:
        settings = _load_json("settings.json", {})
        settings["auto_update"] = bool(enabled)
        _save_json("settings.json", settings)
        return {"auto_update": bool(enabled)}

    async def _auto_loop(self):
        await asyncio.sleep(AUTO_FIRST_DELAY)
        while True:
            delay = AUTO_INTERVAL
            try:
                if _load_json("settings.json", {}).get("auto_update", True):
                    loop = asyncio.get_running_loop()
                    run = await loop.run_in_executor(None, _run_addon_updates, "")
                    if not run["ok"] and "WoW is running" in (run["error"] or ""):
                        delay = AUTO_RETRY_WHEN_WOW_RUNNING
            except asyncio.CancelledError:
                raise
            except Exception:
                decky.logger.exception("auto update loop failed")
            await asyncio.sleep(delay)

    async def _main(self):
        _restore_pending_dev_mode()
        self._auto_task = asyncio.create_task(self._auto_loop())
        decky.logger.info(f"WoW Addon Updater loaded ({PROVENANCE})")

    async def _unload(self):
        task = getattr(self, "_auto_task", None)
        if task:
            task.cancel()
        decky.logger.info("WoW Addon Updater unloaded")
