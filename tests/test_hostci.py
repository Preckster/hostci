import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("hostci", pathlib.Path(__file__).parent.parent / "hostci.py")
hostci = importlib.util.module_from_spec(spec)
sys.modules["hostci"] = hostci
spec.loader.exec_module(hostci)


class ConfigTests(unittest.TestCase):
    def test_example_config_parses(self):
        hostci.CONFIG = pathlib.Path(__file__).parent.parent / "config.example.toml"
        repos = hostci.load_config()
        self.assertEqual(repos[0].name, "Preckster/dune-awakening-modding")
        self.assertEqual(repos[0].release_tag_glob, "v*")
        self.assertEqual(repos[1].jobs_timeout, 1800)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.repo = hostci.Repo(name="o/r")
        self.calls = []
        self.statuses = {}

        def fake_api(path, method="GET", fields=None):
            self.calls.append((method, path, fields))
            if path == "repos/o/r":
                return {"default_branch": "main"}
            if path == "repos/o/r/commits/main":
                return {"sha": "aaa"}
            if path.startswith("repos/o/r/pulls"):
                return [
                    {"number": 1, "head": {"sha": "bbb"}, "author_association": "OWNER", "labels": []},
                    {"number": 2, "head": {"sha": "ccc"}, "author_association": "NONE", "labels": []},
                    {"number": 3, "head": {"sha": "ddd"}, "author_association": "NONE",
                     "labels": [{"name": "ci:run"}]},
                ]
            if path.startswith("repos/o/r/releases"):
                return [{"tag_name": "v1.0.0"}]
            if path.startswith("repos/o/r/tags"):
                return [{"name": "v1.0.0", "commit": {"sha": "eee"}},
                        {"name": "v1.1.0", "commit": {"sha": "fff"}},
                        {"name": "nightly", "commit": {"sha": "ggg"}}]
            if "/statuses" in path and method == "GET":
                sha = path.split("/")[4]
                return self.statuses.get(sha, [])
            if path.endswith("/status"):
                sha = path.split("/")[4]
                return {"statuses": self.statuses.get(sha, [])}
            return None

        hostci.gh_api = fake_api

    def test_builds_main_trusted_prs_and_ci_for_untested_tag(self):
        builds = hostci.discover(self.repo)
        got = {(b.label, b.job) for b in builds}
        self.assertIn(("main", "ci"), got)
        self.assertIn(("PR #1", "ci"), got)
        self.assertIn(("PR #3", "ci"), got)          # untrusted but labelled
        self.assertNotIn(("PR #2", "ci"), got)       # untrusted, not labelled
        self.assertIn(("v1.1.0", "ci"), got)         # tag must pass ci before release
        self.assertNotIn(("nightly", "ci"), got)
        posted = [c for c in self.calls if c[0] == "POST"]
        self.assertTrue(any("ccc" in c[1] and c[2]["state"] == "pending" for c in posted))

    def test_release_after_ci_success(self):
        self.statuses["fff"] = [{"context": "hostci/ci", "state": "success", "description": "passed"}]
        builds = hostci.discover(self.repo)
        self.assertIn(("v1.1.0", "release"), {(b.label, b.job) for b in builds})

    def test_waiting_pr_runs_once_labelled(self):
        self.statuses["ddd"] = [{"context": "hostci/ci", "state": "pending",
                                 "description": hostci.WAITING + ", owner must add label"}]
        builds = hostci.discover(self.repo)
        self.assertIn(("PR #3", "ci"), {(b.label, b.job) for b in builds})


class AutoMergeTests(unittest.TestCase):
    def test_merges_only_clean_prs_with_auto_merge(self):
        prs = [
            {"number": 1, "autoMergeRequest": {"mergeMethod": "SQUASH"}, "mergeStateStatus": "CLEAN",
             "headRefOid": "aaa"},
            {"number": 2, "autoMergeRequest": {"mergeMethod": "SQUASH"}, "mergeStateStatus": "BLOCKED",
             "headRefOid": "bbb"},
            {"number": 3, "autoMergeRequest": None, "mergeStateStatus": "CLEAN", "headRefOid": "ccc"},
        ]
        calls = []

        class R:
            def __init__(self, out):
                self.stdout = out

        def fake_sh(cmd, **kw):
            calls.append(cmd)
            return R(json.dumps(prs) if cmd[:3] == ["gh", "pr", "list"] else "")

        orig = hostci.sh
        hostci.sh = fake_sh
        try:
            hostci.complete_auto_merges(hostci.Repo(name="o/r"))
        finally:
            hostci.sh = orig
        merges = [c for c in calls if c[:3] == ["gh", "pr", "merge"]]
        self.assertEqual(len(merges), 1)
        self.assertIn("1", merges[0])
        self.assertIn("--squash", merges[0])
        self.assertIn("aaa", merges[0])


class ReleaseAssetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wd = pathlib.Path(self.tmp.name)
        self.out = self.wd / "out" / "release"
        self.repo = hostci.Repo(name="o/r", release_assets=["Mod-{version}.zip", "Server-{tag}-*.tar.gz"])
        self.b = hostci.Build(self.repo, "abc", "refs/tags/v1.2.0", "release", "v1.2.0", tag="v1.2.0")

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, *names, data=b"x"):
        self.out.mkdir(parents=True, exist_ok=True)
        for n in names:
            (self.out / n).write_bytes(data)

    def test_complete_assets_get_verified_sums(self):
        self.put("Mod-1.2.0.zip", "Server-v1.2.0-linux.tar.gz", "SHA256SUMS")   # stale sums replaced
        assets = hostci.prepare_release(self.b, self.wd)
        self.assertEqual([p.name for p in assets],
                         ["Mod-1.2.0.zip", "Server-v1.2.0-linux.tar.gz", "SHA256SUMS"])
        sums = (self.out / "SHA256SUMS").read_text().splitlines()
        self.assertEqual(len(sums), 2)
        subprocess.run(["sha256sum", "--strict", "-c", "SHA256SUMS"], cwd=self.out, check=True,
                       capture_output=True)

    def test_test_tag_expects_the_versions_assets(self):
        self.assertEqual(hostci.expected_assets(self.repo, "test-v1.2.0"),
                         ["Mod-1.2.0.zip", "Server-v1.2.0-*.tar.gz"])

    def test_empty_release_dir_fails(self):
        with self.assertRaisesRegex(hostci.ReleaseError, "empty"):
            hostci.prepare_release(self.b, self.wd)
        self.repo.release_allow_empty = True
        self.assertEqual(hostci.prepare_release(self.b, self.wd), [])

    def test_missing_expected_asset_fails(self):
        self.put("Mod.dll")                       # the v0.4.1 voices case: a DLL, no zips
        with self.assertRaisesRegex(hostci.ReleaseError, "Mod-1.2.0.zip"):
            hostci.prepare_release(self.b, self.wd)

    def test_wrong_version_fails(self):
        self.put("Mod-1.1.0.zip", "Server-v1.2.0-linux.tar.gz")
        with self.assertRaisesRegex(hostci.ReleaseError, "missing release assets Mod-1.2.0.zip"):
            hostci.prepare_release(self.b, self.wd)

    def test_zero_byte_asset_fails(self):
        self.put("Mod-1.2.0.zip", "Server-v1.2.0-linux.tar.gz", data=b"")
        with self.assertRaisesRegex(hostci.ReleaseError, "empty release assets"):
            hostci.prepare_release(self.b, self.wd)

    def _publish(self, remote_assets):
        calls = []

        class R:
            stdout = ""

        def fake_sh(cmd, **kw):
            calls.append(cmd)
            return orig_sh(cmd, **kw) if cmd[0] == "sha256sum" else R()

        def fake_api(path, method="GET", fields=None):
            return [{"tag_name": "v1.2.0", "id": 7, "html_url": "u", "assets": remote_assets(self.out)}]

        orig_sh, orig_api = hostci.sh, hostci.gh_api
        hostci.sh, hostci.gh_api = fake_sh, fake_api
        try:
            hostci.publish_release(self.b, self.wd)
        finally:
            hostci.sh, hostci.gh_api = orig_sh, orig_api
        return calls

    def test_publish_creates_draft_then_publishes_verified(self):
        self.put("Mod-1.2.0.zip", "Server-v1.2.0-linux.tar.gz")

        def remote(out):
            return [{"name": p.name, "size": p.stat().st_size, "state": "uploaded"} for p in out.iterdir()]
        calls = self._publish(remote)
        create = next(c for c in calls if c[:3] == ["gh", "release", "create"])
        self.assertIn("--draft", create)
        self.assertTrue(any(c[:2] == ["gh", "api"] and "draft=false" in c for c in calls))

    def test_publish_keeps_draft_when_upload_incomplete(self):
        self.put("Mod-1.2.0.zip", "Server-v1.2.0-linux.tar.gz")

        def remote(out):
            return [{"name": "Mod-1.2.0.zip", "size": 1, "state": "uploaded"}]
        with self.assertRaisesRegex(hostci.ReleaseError, "draft v1.2.0 kept"):
            self._publish(remote)


if __name__ == "__main__":
    unittest.main()
