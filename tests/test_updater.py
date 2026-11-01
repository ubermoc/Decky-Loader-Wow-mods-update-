import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "py_modules"))

from wow_addon_tools import updater  # noqa: E402

P = updater.PLUGIN_FOLDER
GOOD_URL = updater.DOWNLOAD_PREFIX + "latest/WoW.Addon.Updater.zip"


def make_zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return path


def good_files(tag: str) -> dict[str, str]:
    return {
        f"{P}/plugin.json": "{}",
        f"{P}/main.py": f"# {tag}",
        f"{P}/dist/index.js": f"// {tag}",
        f"{P}/VERSION": "0.0.2\n",
        f"{P}/BUILD": "bbbbbbb\n",
    }


class ParseReleaseTests(unittest.TestCase):
    def release(self, body, url=GOOD_URL, name="WoW.Addon.Updater.zip"):
        return {"body": body, "assets": [{"name": name, "browser_download_url": url}]}

    def test_new_notes_format(self):
        got = updater.parse_release(self.release("Version 0.0.2 build ABC1234"))
        self.assertEqual(got, {"version": "0.0.2", "build": "abc1234", "asset_url": GOOD_URL})

    def test_old_notes_format_still_gives_a_build(self):
        got = updater.parse_release(self.release("Built from 551ae8f"))
        self.assertEqual((got["version"], got["build"]), (None, "551ae8f"))

    def test_asset_from_foreign_host_is_ignored(self):
        got = updater.parse_release(self.release("Version 1 build abc1234", url="https://evil.example/x.zip"))
        self.assertIsNone(got["asset_url"])

    def test_non_zip_asset_is_ignored(self):
        got = updater.parse_release(self.release("Version 1 build abc1234", name="notes.txt"))
        self.assertIsNone(got["asset_url"])

    def test_empty_release(self):
        self.assertEqual(updater.parse_release({}), {"version": None, "build": None, "asset_url": None})


class VersionCompareTests(unittest.TestCase):
    def test_same_and_different(self):
        self.assertFalse(updater.update_available("abc1234", "abc1234"))
        self.assertFalse(updater.update_available("abc1234", "ABC1234ffff"))  # short vs long hash
        self.assertTrue(updater.update_available("abc1234", "def5678"))

    def test_dev_build_can_update_and_unknown_latest_cannot(self):
        self.assertTrue(updater.update_available("dev", "abc1234"))
        self.assertFalse(updater.update_available("abc1234", None))


class SslContextTests(unittest.TestCase):
    def setUp(self):
        import ssl
        self.ssl = ssl
        self.real_ctx = ssl.create_default_context  # grab before patching

    def test_uses_first_existing_system_bundle_and_keeps_verification_on(self):
        wanted = "/etc/ca-certificates/extracted/tls-ca-bundle.pem"
        seen = {}

        def fake_ctx(cafile=None, **_):
            seen["cafile"] = cafile
            return self.real_ctx()

        with mock.patch.object(updater.os.path, "isfile", side_effect=lambda p: p == wanted), \
             mock.patch.object(updater.ssl, "create_default_context", side_effect=fake_ctx):
            ctx = updater.ssl_context()
        self.assertEqual(seen["cafile"], wanted)
        self.assertEqual(ctx.verify_mode, self.ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_falls_back_to_default_when_no_bundle_exists(self):
        with mock.patch.object(updater.os.path, "isfile", return_value=False):
            ctx = updater.ssl_context()
        self.assertEqual(ctx.verify_mode, self.ssl.CERT_REQUIRED)

    def test_skips_unloadable_bundle(self):
        calls = []

        def fake_ctx(cafile=None, **_):
            calls.append(cafile)
            if cafile == "/etc/ssl/certs/ca-certificates.crt":
                raise self.ssl.SSLError("bad bundle")
            return self.real_ctx()

        with mock.patch.object(updater.os.path, "isfile", return_value=True), \
             mock.patch.object(updater.ssl, "create_default_context", side_effect=fake_ctx):
            updater.ssl_context()
        self.assertEqual(calls[0], "/etc/ssl/certs/ca-certificates.crt")
        self.assertEqual(calls[1], "/etc/ca-certificates/extracted/tls-ca-bundle.pem")

    def test_requests_pass_the_context(self):
        import io

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        body = b'{"body": "Version 1 build abc1234", "assets": []}'
        with mock.patch.object(updater.urllib.request, "urlopen", return_value=Resp(body)) as uo, \
             mock.patch.object(updater, "ssl_context", return_value="CTX"):
            got = updater.fetch_latest()
        self.assertEqual(uo.call_args.kwargs["context"], "CTX")
        self.assertEqual(got["build"], "abc1234")


class InstalledTests(unittest.TestCase):
    def test_reads_version_and_build_or_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self.assertEqual(updater.read_installed(d), {"version": "unknown", "build": "dev"})
            (d / "VERSION").write_text("0.0.1\n")
            (d / "BUILD").write_text("aaaaaaa\n")
            self.assertEqual(updater.read_installed(d), {"version": "0.0.1", "build": "aaaaaaa"})


class InstallZipTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.plugin = self.root / "plugin"
        (self.plugin / "dist").mkdir(parents=True)
        (self.plugin / "main.py").write_text("# old")
        (self.plugin / "dist" / "index.js").write_text("// old")
        (self.plugin / "dist" / "stale.js").write_text("// stale file from old build")
        (self.plugin / "BUILD").write_text("aaaaaaa\n")

    def test_replaces_files_and_removes_stale_ones(self):
        z = make_zip(self.root / "u.zip", good_files("new"))
        updater.install_zip(z, self.plugin)
        self.assertEqual((self.plugin / "main.py").read_text(), "# new")
        self.assertEqual((self.plugin / "dist" / "index.js").read_text(), "// new")
        self.assertFalse((self.plugin / "dist" / "stale.js").exists())
        self.assertEqual(updater.read_installed(self.plugin), {"version": "0.0.2", "build": "bbbbbbb"})

    def test_rejects_path_traversal_and_changes_nothing(self):
        files = good_files("new")
        files["../evil.py"] = "boom"
        z = make_zip(self.root / "u.zip", files)
        with self.assertRaises(updater.UpdateError):
            updater.install_zip(z, self.plugin)
        self.assertEqual((self.plugin / "main.py").read_text(), "# old")
        self.assertFalse((self.root / "evil.py").exists())

    def test_rejects_wrong_layout(self):
        z = make_zip(self.root / "u.zip", {"main.py": "x", "plugin.json": "{}"})
        with self.assertRaises(updater.UpdateError):
            updater.install_zip(z, self.plugin)
        self.assertEqual((self.plugin / "main.py").read_text(), "# old")

    def test_rejects_missing_required_file(self):
        files = good_files("new")
        del files[f"{P}/dist/index.js"]
        z = make_zip(self.root / "u.zip", files)
        with self.assertRaises(updater.UpdateError) as ctx:
            updater.install_zip(z, self.plugin)
        self.assertIn("dist/index.js", str(ctx.exception))
        self.assertEqual((self.plugin / "main.py").read_text(), "# old")

    def test_rejects_non_zip(self):
        bad = self.root / "u.zip"
        bad.write_bytes(b"not a zip")
        with self.assertRaises(updater.UpdateError):
            updater.install_zip(bad, self.plugin)

    def test_rolls_back_if_a_move_fails_midway(self):
        z = make_zip(self.root / "u.zip", good_files("new"))
        real_move = updater.shutil.move
        calls = {"n": 0}

        def flaky(src, dst):
            # Fail on the first move of a NEW file into the plugin dir (after at least one backup move).
            if str(dst).startswith(str(self.plugin)) and "staged" in str(src):
                calls["n"] += 1
                if calls["n"] == 3:
                    raise OSError("disk full")
            return real_move(src, dst)

        with mock.patch.object(updater.shutil, "move", side_effect=flaky):
            with self.assertRaises(updater.UpdateError):
                updater.install_zip(z, self.plugin)
        self.assertEqual((self.plugin / "main.py").read_text(), "# old")
        self.assertEqual((self.plugin / "dist" / "index.js").read_text(), "// old")
        self.assertTrue((self.plugin / "dist" / "stale.js").exists())
        self.assertEqual((self.plugin / "BUILD").read_text(), "aaaaaaa\n")

    def test_download_refuses_unexpected_host(self):
        with self.assertRaises(updater.UpdateError):
            updater.download("https://evil.example/x.zip", self.root / "x.zip")
        self.assertFalse((self.root / "x.zip").exists())


if __name__ == "__main__":
    unittest.main()
