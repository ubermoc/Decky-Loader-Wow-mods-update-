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

from wow_addon_tools import PROVENANCE, addons, cdp, cfconfig, overrides, updater  # noqa: E402
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


_run_lock = threading.Lock()


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


def _run_app_task(task, what: str) -> dict:
    """Run a CurseForge-app task, then always let the app finish closing and restore its settings."""
    try:
        return task()
    except cdp.CdpError as exc:
        decky.logger.warning(f"curseforge app ({what}): {exc}")
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        decky.logger.exception(f"curseforge app ({what}) crashed")
        return {"ok": False, "error": f"Unexpected error: {exc}"}
    finally:
        for _ in range(15):
            if not cfconfig.curseforge_running():
                break
            time.sleep(1)
        _restore_pending_dev_mode()


def _diagnose_curseforge_app() -> dict:
    """Read the structure of CurseForge's My Addons screen and save it for debugging."""

    def task() -> dict:
        dump = cdp.dump_my_addons()
        _save_json("curseforge_dump.json", dump)
        path = str(_settings_dir() / "curseforge_dump.json")
        decky.logger.info(f"curseforge dump saved to {path}: {json.dumps(dump)[:3000]}")
        lines = [f"Route {dump.get('hash')}", f"{dump.get('actionCount', 0)} rows found"]
        ua = dump.get("updateAll")
        lines.append(f"Update all: {ua['tag']}.{ua['cls']} \"{ua['label'] or ua['text']}\"" if ua else "Update all button: not found")
        for row in (dump.get("rows") or [])[:6]:
            lines.append(f"[{row.get('action')}] {row.get('rowTag')}.{row.get('rowCls', '')[:30]} {row.get('rowText', '')[:70]}")
        return {"ok": True, "error": None, "summary": "\n".join(lines), "path": path}

    return _run_app_task(task, "diagnose")


def _update_addon_through_app(addons_path: str, folder: str) -> dict:
    install = _install_for(addons_path)
    if install is None:
        return {"ok": False, "error": "That WoW version wasn't found any more. Press Rescan."}
    if not folder or "/" in folder or "\\" in folder or folder.startswith("."):
        return {"ok": False, "error": "Invalid addon name."}
    addons_dir = Path(install.addons_path)
    if not (addons_dir / folder).is_dir():
        return {"ok": False, "error": f"{folder} isn't installed any more. Press Rescan."}
    names = addons.names_for(addons_dir, folder)

    def task() -> dict:
        result = cdp.update_row_through_app(names)
        decky.logger.info(f"curseforge app row update {names}: {result}")
        return {"ok": True, "error": None, "clicked": result["clicked"], "message": result["message"]}

    return _run_app_task(task, f"update {folder}")


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


def _install_for(addons_path: str):
    """Only paths the scan itself found are accepted, never an arbitrary path from the UI."""
    for install in find_installs(_home()):
        if install.addons_path == addons_path and Path(install.addons_path).is_dir():
            return install
    return None


def _manual_ids(install) -> dict:
    return overrides.get(_settings_dir(), install.addons_path)


def _list_addons(addons_path: str) -> dict:
    install = _install_for(addons_path)
    if install is None:
        return {"ok": False, "error": "That WoW version wasn't found any more. Press Rescan.", "addons": [], "untracked": []}
    found = addons.discover(Path(install.addons_path), _manual_ids(install))
    state = _load_json("state.json", {}).get(install.addons_path, {})
    rows = []
    for addon in found.tracked:
        rows.append(
            {
                "project_id": addon.project_id,
                "name": addon.folders[0],
                "folders": addon.folders,
                "version": addons._version_token(addon.versions[0]) if addon.versions else "",
                "installed_file_known": str(addon.project_id) in state,
                # set when the ID was typed in (not read from the .toc): the UI offers edit / remove
                "manual_folder": addon.manual_folders[0] if addon.manual_folders else None,
            }
        )
    return {
        "ok": True,
        "error": None,
        "label": install.label,
        "addons": rows,
        "untracked": [n for n in found.untracked if not n.startswith("Blizzard_")],
    }


def _check_addons(addons_path: str) -> dict:
    install = _install_for(addons_path)
    if install is None:
        return {"ok": False, "error": "That WoW version wasn't found any more. Press Rescan.", "results": {}}
    target = addons.target_for(install.flavor, Path(install.root))
    if target is None:
        return {"ok": False, "error": f"Can't tell which WoW version {install.flavor} is.", "results": {}}
    state = _load_json("state.json", {}).get(install.addons_path, {})
    client = addons.CurseForgeClient()
    results = {}
    for addon in addons.discover(Path(install.addons_path), _manual_ids(install)).tracked:
        results[str(addon.project_id)] = addons.check_addon(addon, target, client, state)
    return {"ok": True, "error": None, "results": results}


def _update_one_addon(addons_path: str, project_id: int) -> dict:
    if not _run_lock.acquire(blocking=False):
        return {"ok": False, "error": "An addon update is already running."}
    try:
        if addons.wow_running():
            return {"ok": False, "error": "WoW is running. Close it first so addons aren't swapped under it."}
        install = _install_for(addons_path)
        if install is None:
            return {"ok": False, "error": "That WoW version wasn't found any more. Press Rescan."}
        target = addons.target_for(install.flavor, Path(install.root))
        if target is None:
            return {"ok": False, "error": f"Can't tell which WoW version {install.flavor} is."}
        addons_dir = Path(install.addons_path)
        addon = next(
            (a for a in addons.discover(addons_dir, _manual_ids(install)).tracked if a.project_id == int(project_id)),
            None,
        )
        if addon is None:
            return {"ok": False, "error": "That addon has no CurseForge ID, so it can't be updated here."}
        state = _load_json("state.json", {})
        per_install = state.setdefault(install.addons_path, {})
        outcome = addons.update_addon(addon, target, addons_dir, addons.CurseForgeClient(), per_install, force=True)
        _save_json("state.json", state)
        decky.logger.info(f"update {addon.folders[0]} ({install.flavor}): {outcome}")
        if outcome["status"] == "failed":
            return {"ok": False, "error": outcome["error"]}
        if outcome["status"] == "incompatible":
            return {"ok": False, "error": f"CurseForge has no release of {addon.folders[0]} for {install.label}."}
        return {"ok": True, "error": None, "latest": outcome["latest"]}
    except Exception as exc:
        decky.logger.exception("single addon update crashed")
        return {"ok": False, "error": f"Unexpected error: {exc}"}
    finally:
        _run_lock.release()


def _check_project_id(addons_path: str, text: str) -> dict:
    """Does this project ID have a release for this WoW version? Shows its newest file, saves nothing."""
    try:
        project_id = overrides.parse_project_id(text)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    install = _install_for(addons_path)
    if install is None:
        return {"ok": False, "error": "That WoW version wasn't found any more. Press Rescan."}
    target = addons.target_for(install.flavor, Path(install.root))
    if target is None:
        return {"ok": False, "error": f"Can't tell which WoW version {install.flavor} is."}
    try:
        file = addons.newest_file(addons.CurseForgeClient(), project_id, target)
    except addons.AddonError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        decky.logger.exception("project id check crashed")
        return {"ok": False, "error": f"Unexpected error: {exc}"}
    if file is None:
        return {
            "ok": False,
            "error": f"CurseForge has no release for project {project_id} for {install.label}. Check the number.",
        }
    return {"ok": True, "error": None, "project_id": project_id, "latest": file.get("fileName") or file.get("displayName")}


def _set_project_id(addons_path: str, folder: str, text: str) -> dict:
    try:
        project_id = overrides.parse_project_id(text)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    install = _install_for(addons_path)
    if install is None:
        return {"ok": False, "error": "That WoW version wasn't found any more. Press Rescan."}
    if not folder or "/" in folder or "\\" in folder or folder.startswith("."):
        return {"ok": False, "error": "Invalid addon name."}
    addons_dir = Path(install.addons_path)
    if not (addons_dir / folder).is_dir():
        return {"ok": False, "error": f"{folder} isn't installed any more. Press Rescan."}
    # Only addons whose .toc has no ID of its own may be given one (the .toc's own ID always wins).
    if folder not in {n for n in addons.discover(addons_dir, _manual_ids(install)).untracked} and folder not in _manual_ids(install):
        return {"ok": False, "error": f"{folder} already has a CurseForge ID in its files."}
    overrides.set_id(_settings_dir(), install.addons_path, folder, project_id)
    decky.logger.info(f"project id for {folder} ({install.flavor}) set to {project_id}")
    return {"ok": True, "error": None, "project_id": project_id}


def _clear_project_id(addons_path: str, folder: str) -> dict:
    install = _install_for(addons_path)
    if install is None:
        return {"ok": False, "error": "That WoW version wasn't found any more. Press Rescan."}
    removed = overrides.clear_id(_settings_dir(), install.addons_path, folder)
    decky.logger.info(f"project id for {folder} ({install.flavor}) removed: {removed}")
    return {"ok": True, "error": None, "removed": removed}


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

    async def list_addons(self, addons_path: str) -> dict:
        """Addons in one WoW version's AddOns folder (local only, fast)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _list_addons, addons_path)

    async def check_addons(self, addons_path: str) -> dict:
        """Ask CurseForge what the newest release of each addon is (network, a few seconds)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _check_addons, addons_path)

    async def update_addon(self, addons_path: str, project_id: int) -> dict:
        """Install the newest release of ONE addon."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _update_one_addon, addons_path, project_id)

    async def check_project_id(self, addons_path: str, project_id: str) -> dict:
        """Look the typed ID up on CurseForge for this WoW version (saves nothing)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _check_project_id, addons_path, project_id)

    async def set_project_id(self, addons_path: str, folder: str, project_id: str) -> dict:
        """Save (or replace) the project ID for an addon that has none in its .toc."""
        return _set_project_id(addons_path, folder, project_id)

    async def clear_project_id(self, addons_path: str, folder: str) -> dict:
        return _clear_project_id(addons_path, folder)

    async def diagnose_curseforge_app(self) -> dict:
        """Call after prepare_curseforge_app and launching CurseForge."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _diagnose_curseforge_app)

    async def update_addon_through_app(self, addons_path: str, folder: str) -> dict:
        """Call after prepare_curseforge_app and launching CurseForge."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _update_addon_through_app, addons_path, folder)

    async def prepare_curseforge_app(self) -> dict:
        """Call before launching CurseForge for update_via_curseforge_app."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _prepare_curseforge_app)

    async def update_via_curseforge_app(self) -> dict:
        """Call after launching CurseForge (with --remote-debugging-port=9222 in its launch options)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _drive_curseforge_app)

    async def _main(self):
        _restore_pending_dev_mode()
        decky.logger.info(f"WoW Addon Updater loaded ({PROVENANCE})")

    async def _unload(self):
        decky.logger.info("WoW Addon Updater unloaded")
