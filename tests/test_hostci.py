import importlib.util
import pathlib
import sys
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


if __name__ == "__main__":
    unittest.main()
