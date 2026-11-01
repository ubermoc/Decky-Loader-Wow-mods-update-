# Copyright (c) 2026 ubermoc. WoW Addon Updater, https://github.com/ubermoc/Decky-Loader-Wow-mods-update-
# Provenance: wow-addon-updater/2f44d0b2-b3c2-4726-a93d-480b19a8008e
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "py_modules"))

from wow_addon_tools import shortcuts, wow  # noqa: E402


def _s(key: str, value: str) -> bytes:
    return b"\x01" + key.encode() + b"\x00" + value.encode() + b"\x00"


def _i(key: str, value: int) -> bytes:
    return b"\x02" + key.encode() + b"\x00" + struct.pack("<I", value)


def build_vdf(entries: list[tuple[int, str, str]]) -> bytes:
    body = b""
    for idx, (appid, name, exe) in enumerate(entries):
        body += b"\x00" + str(idx).encode() + b"\x00"
        body += _i("appid", appid) + _s("AppName", name) + _s("Exe", exe) + b"\x08"
    return b"\x00shortcuts\x00" + body + b"\x08" + b"\x08"


class ShortcutTests(unittest.TestCase):
    def test_parses_entries(self):
        data = build_vdf([(3000000001, "CurseForge", '"/home/deck/Apps/CurseForge.AppImage"'), (5, "Other", "/bin/x")])
        found = list(shortcuts.iter_shortcuts(data))
        self.assertEqual([s.name for s in found], ["CurseForge", "Other"])
        self.assertEqual(found[0].app_id, 3000000001)  # unsigned

    def test_finds_curseforge_by_name_or_exe(self):
        by_name = shortcuts.Shortcut(1, "CurseForge", "/x")
        by_exe = shortcuts.Shortcut(2, "WoW Addons", "/home/deck/curseforge-1.0.AppImage")
        other = shortcuts.Shortcut(3, "Other", "/bin/x")
        self.assertIs(shortcuts.find_curseforge([other, by_name]), by_name)
        self.assertIs(shortcuts.find_curseforge([other, by_exe]), by_exe)
        self.assertIsNone(shortcuts.find_curseforge([other]))

    def test_lowercase_keys(self):
        entry = b"\x00shortcuts\x00\x000\x00" + _i("appid", 7) + _s("appname", "curseforge") + _s("exe", "e") + b"\x08\x08\x08"
        self.assertEqual(shortcuts.find_curseforge(list(shortcuts.iter_shortcuts(entry))).app_id, 7)

    def test_bad_data_does_not_crash_loader(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "shortcuts.vdf"
            bad.write_bytes(b"\x00shortcuts\x00\x07garbage")
            self.assertEqual(shortcuts.load_shortcuts(bad), [])
            self.assertEqual(shortcuts.load_shortcuts(Path(tmp) / "missing.vdf"), [])

    def test_empty_file(self):
        self.assertEqual(list(shortcuts.iter_shortcuts(b"")), [])

    def test_never_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shortcuts.vdf"
            path.write_bytes(build_vdf([(1, "CurseForge", "x")]))
            before = path.read_bytes()
            shortcuts.load_shortcuts(path)
            self.assertEqual(path.read_bytes(), before)


class WowScanTests(unittest.TestCase):
    def make_home(self, tmp: str) -> Path:
        home = Path(tmp)
        steam = home / ".local/share/Steam"
        (steam / "steamapps").mkdir(parents=True)
        (steam / "userdata/123/config").mkdir(parents=True)
        return home

    def make_wow(self, library: Path, prefix_id: str, flavors: dict[str, list[str]]) -> Path:
        root = library / "steamapps/compatdata" / prefix_id / "pfx/drive_c/Program Files (x86)/World of Warcraft"
        for flavor, addons in flavors.items():
            addons_dir = root / flavor / "Interface" / "AddOns"
            addons_dir.mkdir(parents=True)
            for addon in addons:
                (addons_dir / addon).mkdir()
        return root

    def test_lists_every_flavor_with_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self.make_home(tmp)
            steam = home / ".local/share/Steam"
            self.make_wow(steam, "3123456789", {"_retail_": ["DBM", "Details"], "_classic_era_": ["Questie"], "_classic_": []})
            installs = {i.flavor: i for i in wow.find_installs(home)}
            self.assertEqual(set(installs), {"_retail_", "_classic_era_", "_classic_"})
            self.assertEqual(installs["_retail_"].label, "Retail")
            self.assertEqual(installs["_retail_"].addon_count, 2)
            self.assertEqual(installs["_classic_era_"].label, "Classic Era")
            self.assertEqual(installs["_classic_"].addon_count, 0)

    def test_in_place_update_moves_modified_time(self):
        import os

        with tempfile.TemporaryDirectory() as tmp:
            home = self.make_home(tmp)
            steam = home / ".local/share/Steam"
            root = self.make_wow(steam, "1", {"_retail_": ["DBM"]})
            addons = root / "_retail_/Interface/AddOns"
            toc = addons / "DBM" / "DBM.toc"
            toc.write_text("## Version: 1")
            old = 1_000_000_000
            for p in (toc, addons / "DBM", addons):
                os.utime(p, (old, old))
            before = wow.find_installs(home)[0].addons_modified
            self.assertEqual(before, old)
            # An update rewrites the .toc without touching the AddOns folder itself.
            os.utime(toc, (old + 500, old + 500))
            after = wow.find_installs(home)[0].addons_modified
            self.assertEqual(after, old + 500)

    def test_finds_wow_on_second_library_from_libraryfolders(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self.make_home(tmp)
            sd = home / "sdcard"
            sd.mkdir()
            (home / ".local/share/Steam/steamapps/libraryfolders.vdf").write_text(
                '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n}\n' % sd
            )
            self.make_wow(sd, "42", {"_retail_": ["Bartender4"]})
            installs = wow.find_installs(home)
            self.assertEqual([i.flavor for i in installs], ["_retail_"])

    def test_ignores_non_flavor_dirs_and_missing_wow(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self.make_home(tmp)
            steam = home / ".local/share/Steam"
            root = self.make_wow(steam, "1", {"_retail_": []})
            (root / "Logs").mkdir()
            (root / "_unrelated_").mkdir()  # matches the pattern but is not a game folder
            (steam / "steamapps/compatdata/2/pfx/drive_c").mkdir(parents=True)
            self.assertEqual([i.flavor for i in wow.find_installs(home)], ["_retail_"])

    def test_no_steam_at_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(wow.find_installs(Path(tmp)), [])

    def test_unknown_flavor_gets_generated_label(self):
        self.assertEqual(wow._label_for("_cataclysm_"), "Cataclysm")


if __name__ == "__main__":
    unittest.main()
