import os
import sys
from pathlib import Path

import decky

# Decky normally puts py_modules on sys.path; do it explicitly so a manual
# install behaves the same.
_PY_MODULES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "py_modules")
if _PY_MODULES not in sys.path:
    sys.path.insert(0, _PY_MODULES)

from wow_addon_tools.shortcuts import find_all_shortcuts, find_curseforge  # noqa: E402
from wow_addon_tools.wow import find_installs  # noqa: E402


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

    async def _main(self):
        decky.logger.info("WoW Addon Updater loaded")

    async def _unload(self):
        decky.logger.info("WoW Addon Updater unloaded")
