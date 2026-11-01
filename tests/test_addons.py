# Copyright (c) 2026 ubermoc. All rights reserved. See LICENSE. WoW Addon Updater, https://github.com/ubermoc/Decky-Loader-Wow-mods-update-
# Provenance: wow-addon-updater/2f44d0b2-b3c2-4726-a93d-480b19a8008e
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "py_modules"))

from wow_addon_tools import addons  # noqa: E402

BUILD_INFO = (
    "Branch!STRING:0|Active!DEC:1|Version!STRING:0|Product!STRING:0\n"
    "us|1|11.2.0.62213|wow\n"
    "us|1|5.5.0.62655|wow_classic\n"
    "us|1|1.15.7.61582|wow_classic_era\n"
)


def make_addon(addons_dir: Path, folder: str, project_id=None, mtime=None, version=None) -> None:
    d = addons_dir / folder
    d.mkdir(parents=True, exist_ok=True)
    lines = ["## Interface: 110200", f"## Title: {folder}"]
    if project_id is not None:
        lines.append(f"## X-Curse-Project-ID: {project_id}")
    if version is not None:
        lines.append(f"## Version: {version}")
    (d / f"{folder}.toc").write_text("\n".join(lines))
    if mtime is not None:
        for p in (d / f"{folder}.toc", d, addons_dir):
            os.utime(p, (mtime, mtime))


def make_zip(path: Path, entries: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


def cf_file(file_id, created="2026-10-01T00:00:00Z", types=(517,), release=1, status=4):
    return {
        "id": file_id,
        "dateCreated": created,
        "gameVersionTypeIds": list(types),
        "releaseType": release,
        "status": status,
    }


class FakeClient:
    def __init__(self, files_by_project, zips_by_file):
        self.files_by_project = files_by_project
        self.zips_by_file = zips_by_file
        self.downloaded = []

    def files_page(self, project_id, page):
        return self.files_by_project.get(project_id, []) if page == 0 else []

    def download(self, project_id, file_id, dest):
        self.downloaded.append(file_id)
        make_zip(dest, self.zips_by_file[file_id])


class FlavorTests(unittest.TestCase):
    def test_parse_build_info(self):
        info = addons.parse_build_info(BUILD_INFO)
        self.assertEqual(info["wow_classic"], "5.5.0.62655")

    def root_with(self, tmp, text=BUILD_INFO):
        root = Path(tmp)
        (root / ".build.info").write_text(text)
        return root

    def test_targets_by_flavor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.root_with(tmp)
            self.assertEqual(addons.target_for("_retail_", root), addons.FlavorTarget(type_id=517))
            self.assertEqual(addons.target_for("_classic_", root), addons.FlavorTarget(version_prefix="5.5"))
            self.assertEqual(addons.target_for("_classic_era_", root), addons.FlavorTarget(version_prefix="1.15"))
            self.assertIsNone(addons.target_for("_something_new_", root))

    def test_forever_folder_uses_its_own_type_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(addons.target_for("_forever_", Path(tmp)), addons.FlavorTarget(type_id=88568))
            f = {"gameVersions": ["1.60.1"], "gameVersionTypeIds": [88568]}
            self.assertTrue(addons.target_for("_forever_", Path(tmp)).matches(f))

    def test_wrath_and_forever_clients_are_supported(self):
        for version, prefix in (("3.4.3.54261", "3.4"), ("4.4.1.1", "4.4"), ("1.60.1.99", "1.60")):
            info = f"Version!STRING:0|Product!STRING:0\n{version}|wow_classic\n"
            with tempfile.TemporaryDirectory() as tmp:
                root = self.root_with(tmp, info)
                self.assertEqual(addons.target_for("_classic_", root), addons.FlavorTarget(version_prefix=prefix))

    def test_modern_version_in_classic_folder_means_retail_type(self):
        info = "Version!STRING:0|Product!STRING:0\n12.0.1.1|wow_classic\n"
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(addons.target_for("_classic_", self.root_with(tmp, info)), addons.FlavorTarget(type_id=517))

    def test_era_falls_back_to_type_id_without_build_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(addons.target_for("_classic_era_", Path(tmp)), addons.FlavorTarget(type_id=67408))
            self.assertIsNone(addons.target_for("_classic_", Path(tmp)))

    def test_matches_by_client_version_not_shared_type_ids(self):
        wrath = addons.FlavorTarget(version_prefix="3.4")
        forever = addons.FlavorTarget(version_prefix="1.60")
        f = {"gameVersions": ["1.15.9", "3.4.5", "4.4.2", "5.5.4", "1.60.1"], "gameVersionTypeIds": [81212, 88568]}
        self.assertTrue(wrath.matches(f))
        self.assertTrue(forever.matches(f))
        self.assertFalse(addons.FlavorTarget(version_prefix="3.5").matches(f))
        self.assertFalse(addons.FlavorTarget(version_prefix="1.6").matches(f))  # no prefix bleed (1.6 vs 1.60)


class DiscoverTests(unittest.TestCase):
    def test_groups_folders_by_project_and_reports_untracked(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            make_addon(d, "Details", 61284)
            make_addon(d, "Details_Streamer", 61284)
            make_addon(d, "DBM-Core", 3358)
            make_addon(d, "HandWritten")  # no project id
            (d / ".wowupdater-old").mkdir()
            found = addons.discover(d)
            tracked = {a.project_id: a for a in found.tracked}
            self.assertEqual(set(tracked), {61284, 3358})
            self.assertEqual(sorted(tracked[61284].folders), ["Details", "Details_Streamer"])
            self.assertEqual(found.untracked, ["HandWritten"])


class PickFileTests(unittest.TestCase):
    def test_newest_matching_release_only(self):
        files = [
            cf_file(1, "2026-01-01T00:00:00Z"),
            cf_file(2, "2026-03-01T00:00:00Z"),
            cf_file(3, "2026-05-01T00:00:00Z", release=2),  # beta
            cf_file(4, "2026-06-01T00:00:00Z", types=(67408,)),  # other flavor
            cf_file(5, "2026-07-01T00:00:00Z", status=10),  # not approved
        ]
        retail = addons.FlavorTarget(type_id=517)
        self.assertEqual(addons.pick_file(files, retail)["id"], 2)
        self.assertEqual(addons.pick_file(files, addons.FlavorTarget(type_id=67408))["id"], 4)
        self.assertIsNone(addons.pick_file(files, addons.FlavorTarget(type_id=79434)))

    def test_needs_update(self):
        a = addons.TrackedAddon(1, ["X"], versions=["#Details.20261015.15339.172"])
        current = {"id": 10, "fileName": "Details-Details.20261015.15339.172.zip", "displayName": "x"}
        newer = {"id": 11, "fileName": "Details-Details.20261101.1.1.zip", "displayName": "x"}
        self.assertFalse(addons.needs_update(a, current, None))  # version is in the release name
        self.assertTrue(addons.needs_update(a, newer, None))  # unknown -> download
        self.assertFalse(addons.needs_update(a, newer, 11))  # recorded id wins
        self.assertTrue(addons.needs_update(a, current, 9))
        unversioned = addons.TrackedAddon(2, ["Y"], versions=["@project-version@"])
        self.assertTrue(addons.needs_update(unversioned, current, None))


class InstallZipTests(unittest.TestCase):
    def test_replaces_folder_and_removes_stale_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "AddOns"
            make_addon(d, "Foo", 1)
            (d / "Foo" / "stale.lua").write_text("old")
            z = Path(tmp) / "foo.zip"
            make_zip(z, {"Foo/Foo.toc": "## X-Curse-Project-ID: 1\n", "Foo/new.lua": "new"})
            self.assertEqual(addons.install_zip(z, d), ["Foo"])
            self.assertTrue((d / "Foo" / "new.lua").exists())
            self.assertFalse((d / "Foo" / "stale.lua").exists())
            self.assertEqual([p.name for p in d.iterdir()], ["Foo"])  # staging cleaned up

    def test_rejects_path_traversal_and_leaves_addons_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "AddOns"
            make_addon(d, "Foo", 1)
            z = Path(tmp) / "evil.zip"
            make_zip(z, {"Foo/Foo.toc": "x", "../evil.txt": "pwn"})
            with self.assertRaises(addons.AddonError):
                addons.install_zip(z, d)
            self.assertTrue((d / "Foo" / "Foo.toc").exists())
            self.assertFalse((Path(tmp) / "evil.txt").exists())

    def test_rejects_symlinks_and_non_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "AddOns"
            d.mkdir()
            z = Path(tmp) / "link.zip"
            with zipfile.ZipFile(z, "w") as zf:
                info = zipfile.ZipInfo("Foo/link")
                info.external_attr = 0o120777 << 16
                zf.writestr(info, "/etc/passwd")
            with self.assertRaises(addons.AddonError):
                addons.install_zip(z, d)
            bad = Path(tmp) / "bad.zip"
            bad.write_text("not a zip")
            with self.assertRaises(addons.AddonError):
                addons.install_zip(bad, d)

    def test_rolls_back_when_a_swap_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "AddOns"
            make_addon(d, "Aaa", 1)
            (d / "Aaa" / "keep.lua").write_text("keep")
            make_addon(d, "Bbb", 2)
            z = Path(tmp) / "two.zip"
            make_zip(z, {"Aaa/Aaa.toc": "n", "Bbb/Bbb.toc": "n"})

            import shutil as _shutil

            real_move = _shutil.move
            calls = {"n": 0}

            def flaky(src, dst, *a, **k):
                calls["n"] += 1
                # backup Aaa (1), install Aaa (2), backup Bbb (3) -> fail on install Bbb (4)
                if calls["n"] == 4:
                    raise OSError("disk full")
                return real_move(src, dst, *a, **k)

            addons.shutil.move = flaky
            try:
                with self.assertRaises(addons.AddonError):
                    addons.install_zip(z, d)
            finally:
                addons.shutil.move = real_move
            self.assertEqual((d / "Aaa" / "keep.lua").read_text(), "keep")
            self.assertTrue((d / "Bbb" / "Bbb.toc").exists())


class UpdateInstallTests(unittest.TestCase):
    def setup_wow(self, tmp):
        root = Path(tmp) / "World of Warcraft"
        addons_dir = root / "_retail_" / "Interface" / "AddOns"
        addons_dir.mkdir(parents=True)
        (root / ".build.info").write_text(BUILD_INFO)
        return root, addons_dir

    def test_updates_outdated_and_skips_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, d = self.setup_wow(tmp)
            make_addon(d, "Old", 1)
            make_addon(d, "Current", 2, version="2.0.1")
            make_addon(d, "HandMade")
            client = FakeClient(
                {
                    1: [cf_file(11, "2030-01-01T00:00:00Z")],
                    2: [dict(cf_file(22), fileName="Current-2.0.1.zip")],
                },
                {11: {"Old/Old.toc": "## X-Curse-Project-ID: 1\n", "Old/v2.lua": "2"}},
            )
            state: dict = {}
            res = addons.update_install("_retail_", root, d, client, state)
            self.assertEqual(res["updated"], ["Old"])
            self.assertEqual(res["current"], 1)
            self.assertEqual(res["untracked"], 1)
            self.assertEqual(res["failed"], [])
            self.assertEqual(client.downloaded, [11])
            self.assertTrue((d / "Old" / "v2.lua").exists())
            self.assertEqual(state, {"1": 11, "2": 22})

            # second run: state says everything is current, nothing downloaded
            res2 = addons.update_install("_retail_", root, d, client, state)
            self.assertEqual(res2["updated"], [])
            self.assertEqual(client.downloaded, [11])

    def test_unknown_state_downloads_instead_of_assuming_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, d = self.setup_wow(tmp)
            make_addon(d, "Foo", 1)  # no matching version in the release name
            client = FakeClient({1: [cf_file(5)]}, {5: {"Foo/Foo.toc": "## X-Curse-Project-ID: 1\n"}})
            res = addons.update_install("_retail_", root, d, client, {})
            self.assertEqual(res["updated"], ["Foo"])

    def test_no_release_for_this_version_is_not_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, d = self.setup_wow(tmp)
            make_addon(d, "RetailOnly", 1)
            client = FakeClient({1: [cf_file(5, types=(67408,))]}, {})
            res = addons.update_install("_retail_", root, d, client, {})
            self.assertEqual((res["incompatible"], res["failed"], res["updated"]), (1, [], []))

    def test_classic_folder_matches_on_client_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "World of Warcraft"
            d = root / "_classic_" / "Interface" / "AddOns"
            d.mkdir(parents=True)
            (root / ".build.info").write_text(BUILD_INFO)  # wow_classic = 5.5.x
            make_addon(d, "Foo", 1)
            f = dict(cf_file(7, types=(81212,)), gameVersions=["3.4.5", "5.5.4"])
            client = FakeClient({1: [f]}, {7: {"Foo/Foo.toc": "## X-Curse-Project-ID: 1\n"}})
            res = addons.update_install("_classic_", root, d, client, {})
            self.assertEqual(res["updated"], ["Foo"])

    def test_one_failure_does_not_stop_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, d = self.setup_wow(tmp)
            make_addon(d, "Bad", 1)
            make_addon(d, "Good", 2)
            client = FakeClient(
                {1: [cf_file(11, "2030-01-01T00:00:00Z")], 2: [cf_file(22, "2030-01-01T00:00:00Z")]},
                {11: {"../escape": "x"}, 22: {"Good/Good.toc": "ok"}},
            )
            res = addons.update_install("_retail_", root, d, client, {})
            self.assertEqual(res["updated"], ["Good"])
            self.assertEqual(len(res["failed"]), 1)
            self.assertEqual(res["failed"][0]["addon"], "Bad")

    def test_unknown_flavor_is_skipped_with_a_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, d = self.setup_wow(tmp)
            res = addons.update_install("_mystery_", root, d, FakeClient({}, {}), {})
            self.assertIn("_mystery_", res["error"])
            self.assertEqual(res["updated"], [])


class WowRunningTests(unittest.TestCase):
    def test_detects_wow_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            (proc / "1").mkdir()
            (proc / "1" / "cmdline").write_bytes(b"/bin/bash\x00-c\x00ls")
            self.assertFalse(addons.wow_running(proc))
            (proc / "2").mkdir()
            (proc / "2" / "cmdline").write_bytes(b"Z:\\Games\\World of Warcraft\\_retail_\\Wow.exe\x00-launch")
            self.assertTrue(addons.wow_running(proc))


if __name__ == "__main__":
    unittest.main()
