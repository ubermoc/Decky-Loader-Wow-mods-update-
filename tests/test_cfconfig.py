# Copyright (c) 2026 ubermoc. All rights reserved. See LICENSE. WoW Addon Updater, https://github.com/ubermoc/Decky-Loader-Wow-mods-update-
# Provenance: wow-addon-updater/2f44d0b2-b3c2-4726-a93d-480b19a8008e
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "py_modules"))

from wow_addon_tools import cfconfig  # noqa: E402

ORIGINAL = {"session-counter": "0", "ftue-shown-prod": "true", "addons-settings": '{"updateFrequency":2}'}


def make_storage(tmp, data=None):
    path = Path(tmp) / ".config" / "CurseForge" / "storage.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(ORIGINAL if data is None else data))
    os.chmod(path, 0o640)
    return path


class DevModeTests(unittest.TestCase):
    def test_storage_path_uses_the_users_home(self):
        self.assertEqual(cfconfig.storage_path(Path("/home/deck")), Path("/home/deck/.config/CurseForge/storage.json"))

    def test_enable_then_restore_gives_back_the_original_file_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_storage(tmp)
            undo = cfconfig.enable_dev_mode(path)
            self.assertEqual(json.loads(path.read_text())["dev-mode"], "true")
            self.assertEqual(json.loads(path.read_text())["ftue-shown-prod"], "true")  # other keys kept
            cfconfig.restore_dev_mode(path, undo)
            self.assertEqual(json.loads(path.read_text()), ORIGINAL)
            cfconfig.restore_dev_mode(path, undo)  # second call is harmless
            self.assertEqual(json.loads(path.read_text()), ORIGINAL)

    def test_existing_dev_mode_value_is_put_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_storage(tmp, {**ORIGINAL, "dev-mode": "false"})
            undo = cfconfig.enable_dev_mode(path)
            self.assertEqual(json.loads(path.read_text())["dev-mode"], "true")
            cfconfig.restore_dev_mode(path, undo)
            self.assertEqual(json.loads(path.read_text())["dev-mode"], "false")

    def test_file_mode_is_kept_and_a_backup_is_made_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_storage(tmp)
            cfconfig.enable_dev_mode(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
            backup = path.with_name("storage.json.wowupdater-backup")
            self.assertEqual(json.loads(backup.read_text()), ORIGINAL)
            cfconfig.enable_dev_mode(path)  # does not overwrite the first backup
            self.assertEqual(json.loads(backup.read_text()), ORIGINAL)

    def test_missing_or_broken_file_gives_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(cfconfig.ConfigError) as ctx:
                cfconfig.enable_dev_mode(Path(tmp) / "nope.json")
            self.assertIn("Open it once", str(ctx.exception))
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not json")
            with self.assertRaises(cfconfig.ConfigError):
                cfconfig.enable_dev_mode(bad)
            self.assertEqual(bad.read_text(), "{not json")  # untouched

    def test_restore_without_undo_does_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_storage(tmp)
            cfconfig.restore_dev_mode(path, None)
            cfconfig.restore_dev_mode(path, {})
            self.assertEqual(json.loads(path.read_text()), ORIGINAL)


class RunningTests(unittest.TestCase):
    def proc(self, tmp, pid, comm, cmdline):
        d = Path(tmp) / str(pid)
        d.mkdir()
        (d / "comm").write_text(comm + "\n")
        (d / "cmdline").write_bytes(cmdline.replace(" ", "\x00").encode())

    def test_detects_curseforge_and_its_helpers(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.proc(tmp, 1, "bash", "/bin/bash -c ls")
            self.assertFalse(cfconfig.curseforge_running(Path(tmp)))
            self.proc(tmp, 2, "curseforge", "/opt/CurseForge/curseforge --no-sandbox")
            self.assertTrue(cfconfig.curseforge_running(Path(tmp)))

    def test_detects_appimage_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.proc(tmp, 3, "AppRun", "/home/deck/Apps/CurseForge-1.325.AppImage --no-sandbox")
            self.assertTrue(cfconfig.curseforge_running(Path(tmp)))


if __name__ == "__main__":
    unittest.main()
