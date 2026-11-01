# Copyright (c) 2026 ubermoc. All rights reserved. See LICENSE. WoW Addon Updater, https://github.com/ubermoc/Decky-Loader-Wow-mods-update-
# Provenance: wow-addon-updater/2f44d0b2-b3c2-4726-a93d-480b19a8008e
"""Runs the plugin's real backend functions (main.py) with a stand-in for Decky and a fake CurseForge."""
import logging
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "py_modules"))

decky_stub = types.ModuleType("decky")
decky_stub.logger = logging.getLogger("decky-test")
decky_stub.DECKY_USER_HOME = "/nonexistent"
decky_stub.DECKY_PLUGIN_SETTINGS_DIR = "/nonexistent"
sys.modules["decky"] = decky_stub

import main  # noqa: E402
from wow_addon_tools import addons  # noqa: E402


def cf_file(file_id, name, types=(517,)):
    return {
        "id": file_id, "fileName": name, "dateCreated": "2026-10-01T00:00:00Z",
        "gameVersionTypeIds": list(types), "releaseType": 1, "status": 4,
    }


class FakeClient:
    def __init__(self, files, zips=None):
        self.files, self.zips, self.downloaded = files, zips or {}, []

    def files_page(self, project_id, page):
        return self.files.get(project_id, []) if page == 0 else []

    def download(self, project_id, file_id, dest):
        self.downloaded.append(file_id)
        with zipfile.ZipFile(dest, "w") as zf:
            for n, data in self.zips[file_id].items():
                zf.writestr(n, data)


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name) / "home"
        settings = Path(self.tmp.name) / "settings"
        settings.mkdir()
        wow = home / ".local/share/Steam/steamapps/compatdata/1/pfx/drive_c/Program Files (x86)/World of Warcraft"
        self.addons = wow / "_retail_" / "Interface" / "AddOns"
        self.addons.mkdir(parents=True)
        (wow / ".build.info").write_text("Version!STRING:0|Product!STRING:0\n12.1.5.1|wow\n")
        (home / ".local/share/Steam/userdata/1/config").mkdir(parents=True)
        decky_stub.DECKY_USER_HOME = str(home)
        decky_stub.DECKY_PLUGIN_SETTINGS_DIR = str(settings)
        self.path = str(self.addons)
        self.client = FakeClient({})
        real = addons.CurseForgeClient
        addons.CurseForgeClient = lambda: self.client
        self.addCleanup(lambda: setattr(addons, "CurseForgeClient", real))
        main._run_lock.acquire(blocking=False) and main._run_lock.release()

    def make(self, folder, project_id=None):
        d = self.addons / folder
        d.mkdir()
        lines = [f"## Title: {folder}", "## Version: 1.0.0"]
        if project_id:
            lines.append(f"## X-Curse-Project-ID: {project_id}")
        (d / f"{folder}.toc").write_text("\n".join(lines))

    def test_untracked_addon_is_listed_as_untracked(self):
        self.make("UbermocUI")
        res = main._list_addons(self.path)
        self.assertEqual((res["addons"], res["untracked"]), ([], ["UbermocUI"]))

    def test_check_shows_the_newest_file_and_saves_nothing(self):
        self.client.files = {555: [cf_file(9, "UbermocUI-1.0.99.zip")]}
        res = main._check_project_id(self.path, " 555 ")
        self.assertEqual((res["ok"], res["latest"], res["project_id"]), (True, "UbermocUI-1.0.99.zip", 555))
        self.assertEqual(main._list_addons(self.path)["addons"], [])

    def test_check_rejects_bad_numbers_and_unknown_projects(self):
        self.assertFalse(main._check_project_id(self.path, "abc")["ok"])
        res = main._check_project_id(self.path, "777")  # no files at all
        self.assertFalse(res["ok"])
        self.assertIn("no release for project 777", res["error"])

    def test_save_then_it_behaves_like_the_other_addons(self):
        self.make("UbermocUI")
        self.client.files = {555: [cf_file(9, "UbermocUI-1.0.99.zip")]}
        self.client.zips = {9: {"UbermocUI/UbermocUI.toc": "## Title: UbermocUI\n", "UbermocUI/new.lua": "x"}}
        self.assertTrue(main._set_project_id(self.path, "UbermocUI", "555")["ok"])

        listed = main._list_addons(self.path)
        self.assertEqual(listed["untracked"], [])
        row = listed["addons"][0]
        self.assertEqual((row["project_id"], row["name"], row["manual_folder"]), (555, "UbermocUI", "UbermocUI"))

        checked = main._check_addons(self.path)["results"]["555"]
        self.assertEqual((checked["status"], checked["latest"]), ("update", "UbermocUI-1.0.99.zip"))

        done = main._update_one_addon(self.path, 555)
        self.assertEqual((done["ok"], done["latest"]), (True, "UbermocUI-1.0.99.zip"))
        self.assertTrue((self.addons / "UbermocUI" / "new.lua").exists())

    def test_edit_replaces_the_id_and_remove_puts_it_back_to_untracked(self):
        self.make("UbermocUI")
        main._set_project_id(self.path, "UbermocUI", "555")
        main._set_project_id(self.path, "UbermocUI", "666")  # edit
        self.assertEqual(main._list_addons(self.path)["addons"][0]["project_id"], 666)
        self.assertTrue(main._clear_project_id(self.path, "UbermocUI")["removed"])
        listed = main._list_addons(self.path)
        self.assertEqual((listed["addons"], listed["untracked"]), ([], ["UbermocUI"]))
        self.assertFalse(main._clear_project_id(self.path, "UbermocUI")["removed"])  # already gone

    def test_cannot_override_an_addon_that_has_its_own_id(self):
        self.make("BugSack", 9)
        res = main._set_project_id(self.path, "BugSack", "555")
        self.assertFalse(res["ok"])
        self.assertIn("already has a CurseForge ID", res["error"])

    def test_rejects_paths_and_missing_folders(self):
        for folder in ("../evil", "a/b", ".hidden", ""):
            self.assertFalse(main._set_project_id(self.path, folder, "5")["ok"], folder)
        self.assertFalse(main._set_project_id(self.path, "NotInstalled", "5")["ok"])
        self.assertFalse(main._set_project_id("/some/other/path", "X", "5")["ok"])  # only scanned paths accepted


if __name__ == "__main__":
    unittest.main()
