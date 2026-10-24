"""Automated, bounded review-question method/contract evidence planning."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from diff_analysis.auto_evidence import auto_evidence
from diff_analysis.review_workflow import export_questions


def git(path, *argv):
    return subprocess.check_output(["git", "-C", str(path), *argv],
                                   stderr=subprocess.STDOUT).decode("utf-8").strip()


class AutoEvidenceTests(unittest.TestCase):
    def _setup(self, root, with_graph=True):
        repo = root / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        git(repo, "config", "user.name", "Test")
        git(repo, "config", "user.email", "test@example.com")
        (repo / "PayApi.java").write_text(
            "class PayApi {\n void pay() {\n  int x = 1;\n }\n}\n",
            encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "pinned")
        sha = git(repo, "rev-parse", "HEAD")
        diff = {"id": "D-9999999999999999", "project": "team/pay",
                "path": "PayApi.java", "old_path": "PayApi.java",
                "new_range": {"start": 2, "count": 2},
                "old_range": {"start": 2, "count": 2},
                "symbols_old": ["PayApi#pay()"],
                "symbols_new": ["PayApi#pay()"]}
        methods = {"PayApi#pay()": {"path": "PayApi.java",
                                   "start": 2, "end": 4}}
        graphs = [{
            "project": "team/pay", "side": side, "snapshot_sha": sha,
            "method_index": methods, "edges": []
        } for side in ("BASE", "HEAD")] if with_graph else []
        release = {
            "schema_version": "release-evidence-v1",
            "ledgers": [{"project": "team/pay", "base": sha, "head": sha,
                         "final_changes": [diff]}],
            "groups": [{"id": "G-9999999999999999",
                        "change_ids": [diff["id"]], "issue_key": "PAY-1"}],
            "cross_group_checks": [], "consumer_candidates": [],
            "relations": {"graphs": graphs}}
        (root / "analysis.json").write_text(json.dumps(release), encoding="utf-8")
        export_questions(root, release)
        return repo

    def test_auto_evidence_reads_both_sides_without_guesswork(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo = self._setup(root)
            result = auto_evidence(root, {"team/pay": repo})
            self.assertEqual(4, result["planned"])
            self.assertEqual(4, result["new_evidence"])
            manifest = json.loads((root / "auto_evidence_plan.json").read_text("utf-8"))
            self.assertEqual({"BASE", "HEAD"},
                             {s["side"] for s in manifest["selectors"]})
            self.assertEqual({"METHOD", "CONTRACT"},
                             {s["kind"] for s in manifest["selectors"]})
            self.assertTrue(all(x["status"] == "PROVIDED" for x in result["results"]))

    def test_missing_method_graph_is_reported_not_invented(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo = self._setup(root, with_graph=False)
            result = auto_evidence(root, {"team/pay": repo})
            # Contract file is still proven by versioned filename, even when
            # the Java scanner was unavailable for both sides.
            self.assertEqual(2, result["planned"])
            plan = json.loads((root / "auto_evidence_plan.json").read_text("utf-8"))
            self.assertTrue(any(x["reason"] == "METHOD_GRAPH_UNAVAILABLE"
                                for x in plan["omissions"]))


if __name__ == "__main__":
    unittest.main()
