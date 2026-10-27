"""Protocol inventory is observational only: no guessed service bindings."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from diff_analysis.protocol_discovery import discover


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args],
                                   stderr=subprocess.STDOUT).decode("utf-8").strip()


class DiscoveryTests(unittest.TestCase):
    def test_pinned_protocol_hints_do_not_become_edges(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo = root / "project"
            repo.mkdir()
            git(repo, "init", "-q")
            git(repo, "config", "user.name", "Test")
            git(repo, "config", "user.email", "test@example.com")
            src = ("@DubboReference\n"
                   "@PostMapping(\"/pay\")\n"
                   "@RabbitListener(queues = \"trans-pay\")\n"
                   "SELECT * FROM trans_record\n")
            (repo / "App.java").write_text(src, encoding="utf-8")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "base")
            sha = git(repo, "rev-parse", "HEAD")
            release = {"schema_version": "release-evidence-v1",
                       "ledgers": [{"project": "team/pay", "base": sha,
                                    "head": sha, "final_changes": []}]}
            (root / "analysis.json").write_text(json.dumps(release), encoding="utf-8")
            res = discover(root, {"team/pay": repo})
            self.assertGreaterEqual(res["hints"], 4)
            self.assertEqual(1, res["index_builds"])
            self.assertEqual(1, res["index_hits"])
            replay = discover(root, {"team/pay": repo})
            self.assertEqual(2, replay["index_hits"])
            self.assertEqual(0, replay["index_builds"])
            result = json.loads((root / "protocol_discovery_latest.json").read_text("utf-8"))
            kinds = {x["kind"] for x in result["hints"]}
            self.assertTrue({"DUBBO", "HTTP", "MQ", "DATABASE"}.issubset(kinds))
            self.assertTrue(all(x["status"] == "DISCOVERY_ONLY"
                                for x in result["hints"]))
            self.assertFalse((root / "bindings_latest.json").exists())


if __name__ == "__main__":
    unittest.main()
