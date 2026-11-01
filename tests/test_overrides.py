# Copyright (c) 2026 ubermoc. All rights reserved. See LICENSE. WoW Addon Updater, https://github.com/ubermoc/Decky-Loader-Wow-mods-update-
# Provenance: wow-addon-updater/2f44d0b2-b3c2-4726-a93d-480b19a8008e
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "py_modules"))

from wow_addon_tools import addons, overrides  # noqa: E402


def make_addon(d, folder, project_id=None):
    (d / folder).mkdir(parents=True, exist_ok=True)
    lines = [f"## Title: {folder}"]
    if project_id:
        lines.append(f"## X-Curse-Project-ID: {project_id}")
    (d / folder / f"{folder}.toc").write_text("\n".join(lines))


class ParseTests(unittest.TestCase):
    def test_accepts_numbers_with_whitespace(self):
        self.assertEqual(overrides.parse_project_id(" 1234567 "), 1234567)
        self.assertEqual(overrides.parse_project_id(42), 42)

    def test_rejects_everything_else_with_a_helpful_message(self):
        for bad in ("", "   ", "abc", "12 34", "-5", "1.5", "0", "999999999999", "https://x"):
            with self.assertRaises(ValueError, msg=bad):
                overrides.parse_project_id(bad)
        with self.assertRaises(ValueError) as ctx:
            overrides.parse_project_id("abc")
        self.assertIn("number", str(ctx.exception))


class StoreTests(unittest.TestCase):
    def test_set_get_edit_and_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Path(tmp)
            self.assertEqual(overrides.get(s, "/a"), {})
            overrides.set_id(s, "/a", "UbermocUI", 111)
            overrides.set_id(s, "/b", "UbermocUI", 222)  # same folder name, other WoW version
            self.assertEqual(overrides.get(s, "/a"), {"UbermocUI": 111})
            self.assertEqual(overrides.get(s, "/b"), {"UbermocUI": 222})
            overrides.set_id(s, "/a", "UbermocUI", 333)  # edit
            self.assertEqual(overrides.get(s, "/a"), {"UbermocUI": 333})
            self.assertTrue(overrides.clear_id(s, "/a", "UbermocUI"))
            self.assertFalse(overrides.clear_id(s, "/a", "UbermocUI"))
            self.assertEqual(overrides.get(s, "/a"), {})
            self.assertEqual(overrides.get(s, "/b"), {"UbermocUI": 222})  # untouched

    def test_broken_file_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Path(tmp)
            (s / overrides.FILE).write_text("{not json")
            self.assertEqual(overrides.get(s, "/a"), {})
            overrides.set_id(s, "/a", "X", 5)  # and can be written over
            self.assertEqual(overrides.get(s, "/a"), {"X": 5})

    def test_garbage_entries_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Path(tmp)
            (s / overrides.FILE).write_text('{"/a": {"Good": 7, "Bad": "x", "Neg": -1}, "/b": 5}')
            self.assertEqual(overrides.get(s, "/a"), {"Good": 7})
            self.assertEqual(overrides.get(s, "/b"), {})


class DiscoverWithManualIdsTests(unittest.TestCase):
    def test_typed_id_makes_an_untracked_addon_behave_like_the_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            make_addon(d, "UbermocUI")  # no id in the toc
            make_addon(d, "BugSack", 9)
            before = addons.discover(d)
            self.assertEqual(before.untracked, ["UbermocUI"])
            after = addons.discover(d, {"UbermocUI": 555})
            self.assertEqual(after.untracked, [])
            manual = next(a for a in after.tracked if a.project_id == 555)
            self.assertEqual((manual.folders, manual.manual_folders), (["UbermocUI"], ["UbermocUI"]))
            toc_based = next(a for a in after.tracked if a.project_id == 9)
            self.assertIsNone(toc_based.manual_folders)

    def test_a_tocs_own_id_wins_over_a_typed_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            make_addon(d, "Foo", 9)
            found = addons.discover(d, {"Foo": 555})
            self.assertEqual([a.project_id for a in found.tracked], [9])
            self.assertIsNone(found.tracked[0].manual_folders)

    def test_typed_id_for_another_folder_joins_the_same_project_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            make_addon(d, "Details", 61284)
            make_addon(d, "Details_Extra")
            found = addons.discover(d, {"Details_Extra": 61284})
            self.assertEqual(len(found.tracked), 1)
            self.assertEqual(sorted(found.tracked[0].folders), ["Details", "Details_Extra"])


if __name__ == "__main__":
    unittest.main()
