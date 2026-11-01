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


def make_addon(addons_dir: Path, folder: str, project_id=None, mtime=None) -> None:
    d = addons_dir / folder
    d.mkdir(parents=True, exist_ok=True)
    lines = ["## Interface: 110200", f"## Title: {folder}"]
    if project_id is not None:
        lines.append(f"## X-Curse-Project-ID: {project_id}")
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

    def test_versions_map_to_types(self):
        self.assertEqual(addons.type_for_version("11.2.0.1"), addons.TYPE_RETAIL)
        self.assertEqual(addons.type_for_version("5.5.0.1"), addons.TYPE_MOP)
        self.assertEqual(addons.type_for_version("4.4.1.1"), addons.TYPE_CATA)
        self.assertEqual(addons.type_for_version("1.15.7.1"), addons.TYPE_CLASSIC_ERA)
        self.assertIsNone(addons.type_for_version("3.4.3.1"))  # no known type id
        self.assertIsNone(addons.type_for_version("garbage"))

    def test_type_id_for_flavors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".build.info").write_text(BUILD_INFO)
            self.assertEqual(addons.type_id_for("_retail_", root), addons.TYPE_RETAIL)
            self.assertEqual(addons.type_id_for("_classic_era_", root), addons.TYPE_CLASSIC_ERA)
            self.assertEqual(addons.type_id_for("_classic_", root), addons.TYPE_MOP)
            self.assertIsNone(addons.type_id_for("_something_new_", root))

    def test_classic_without_build_info_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(addons.type_id_for("_classic_", Path(tmp)))


class DiscoverTests(unittest.TestCase):
    def test_groups_folders_by_project_and_skips_untracked(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            make_addon(d, "Details", 61284)
            make_addon(d, "Details_Streamer", 61284)
            make_addon(d, "DBM-Core", 3358)
            make_addon(d, "HandWritten")  # no project id
            (d / ".wowupdater-old").mkdir()
            found = {a.project_id: a for a in addons.discover(d)}
            self.assertEqual(set(found), {61284, 3358})
            self.assertEqual(sorted(found[61284].folders), ["Details", "Details_Streamer"])


class PickFileTests(unittest.TestCase):
    def test_newest_matching_release_only(self):
        files = [
            cf_file(1, "2026-01-01T00:00:00Z"),
            cf_file(2, "2026-03-01T00:00:00Z"),
            cf_file(3, "2026-05-01T00:00:00Z", release=2),  # beta
            cf_file(4, "2026-06-01T00:00:00Z", types=(67408,)),  # other flavor
            cf_file(5, "2026-07-01T00:00:00Z", status=10),  # not approved
        ]
        self.assertEqual(addons.pick_file(files, 517)["id"], 2)
        self.assertEqual(addons.pick_file(files, 67408)["id"], 4)
        self.assertIsNone(addons.pick_file(files, 79434))

    def test_needs_update(self):
        a = addons.TrackedAddon(1, ["X"], newest_mtime=1_800_000_000)
        old = cf_file(9, "2020-01-01T00:00:00Z")
        new = cf_file(10, "2030-01-01T00:00:00Z")
        self.assertFalse(addons.needs_update(a, old, None))
        self.assertTrue(addons.needs_update(a, new, None))
        self.assertFalse(addons.needs_update(a, new, 10))
        self.assertTrue(addons.needs_update(a, old, 8))  # state beats the date heuristic


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
            make_addon(d, "Old", 1, mtime=1_700_000_000)  # installed long before the new release
            make_addon(d, "Current", 2, mtime=1_900_000_000)  # newer than its latest release
            client = FakeClient(
                {
                    1: [cf_file(11, "2030-01-01T00:00:00Z")],
                    2: [cf_file(22, "2026-01-01T00:00:00Z")],
                },
                {11: {"Old/Old.toc": "## X-Curse-Project-ID: 1\n", "Old/v2.lua": "2"}},
            )
            state: dict = {}
            res = addons.update_install("_retail_", root, d, client, state)
            self.assertEqual(res["updated"], ["Old"])
            self.assertEqual(res["failed"], [])
            self.assertEqual(client.downloaded, [11])
            self.assertTrue((d / "Old" / "v2.lua").exists())
            self.assertEqual(state, {"1": 11, "2": 22})

            # second run: state says everything is current, nothing downloaded
            res2 = addons.update_install("_retail_", root, d, client, state)
            self.assertEqual(res2["updated"], [])
            self.assertEqual(client.downloaded, [11])

    def test_one_failure_does_not_stop_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, d = self.setup_wow(tmp)
            make_addon(d, "Bad", 1, mtime=1_700_000_000)
            make_addon(d, "Good", 2, mtime=1_700_000_000)
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
