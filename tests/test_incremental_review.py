"""Incremental grouping re-propagation without changing pinned graph facts."""
import json
import tempfile
import unittest
from pathlib import Path

from diff_analysis.review_feedback import apply_group_feedback
from diff_analysis.review_workflow import export_questions, _hash

SA, SB, SC = "pkg.Pay#a()", "pkg.Pay#b()", "pkg.Pay#c()"
G1, G2, G3 = "G-aaaaaaaaaaaaaaaa", "G-bbbbbbbbbbbbbbbb", "G-cccccccccccccccc"
D1, D2, D3 = "D-aaaaaaaaaaaaaaaa", "D-bbbbbbbbbbbbbbbb", "D-cccccccccccccccc"
SHA0, SHA1 = "0"*40, "1"*40


def fixture():
    methods = {
        SA: {"path": "Pay.java", "start": 2, "end": 3},
        SB: {"path": "Pay.java", "start": 5, "end": 6},
        SC: {"path": "Pay.java", "start": 8, "end": 9}}
    edges = [{"id": "E-abc", "caller": SA, "callee": SB,
              "binding": "JAVAC_ATTRIBUTED"},
             {"id": "E-def", "caller": SC, "callee": SA,
              "binding": "JAVAC_ATTRIBUTED"}]
    changes = []
    for cid, start, symbol in ((D1, 5, SB), (D2, 2, SA), (D3, 8, SC)):
        changes.append({"id": cid, "project": "demo/pay",
                        "path": "Pay.java", "old_path": "Pay.java",
                        "new_range": {"start": start, "count": 2},
                        "old_range": {"start": start, "count": 2},
                        "symbols": [symbol], "symbols_new": [symbol],
                        "symbols_old": [symbol]})
    graphs = []
    for side, sha in (("HEAD", SHA1), ("BASE", SHA0)):
        graphs.append({"project": "demo/pay", "side": side,
                       "snapshot_sha": sha, "method_index": methods,
                       "edges": edges})
    return {"schema_version": "release-evidence-v1",
            "ledgers": [{"project": "demo/pay", "base": SHA0,
                         "head": SHA1, "final_changes": changes}],
            "groups": [{"id": gid, "change_ids": [cid], "issue_key": None}
                       for gid, cid in ((G1, D1), (G2, D2), (G3, D3))],
            "cross_group_checks": [], "consumer_candidates": [],
            "relations": {"graphs": graphs}}


class IncrementalReviewTests(unittest.TestCase):
    def _save(self, root):
        release = fixture()
        (root / "analysis.json").write_text(json.dumps(release), encoding="utf-8")
        export_questions(root, release)
        return release

    def test_changed_group_repropagated_unaffected_group_cache_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = self._save(root)
            uid = "U-" + _hash([D1, "whole"])[:16]
            def move(to):
                return apply_group_feedback(root, {
                    "schema_version": "group-feedback-v1",
                    "actions": [{"action": "MOVE_UNIT", "unit_id": uid,
                                 "target_group_id": to, "actor": "reviewer",
                                 "reason": "Correct requirement ownership"}]})
            first = move(G2)
            self.assertIsNone(first["recompute_error"])
            self.assertTrue((root / "recomputed_latest.json").is_file())
            impact = json.loads((root / "recomputed_latest.json").read_text("utf-8"))
            self.assertEqual(1, impact["revision"])
            self.assertTrue(any(x["from_group"] == G2 for x in impact["direct_group_checks"]))
            second = move(G1)
            self.assertIsNone(second["recompute_error"])
            self.assertGreaterEqual(second["recomputed"]["reused_groups"], 1)
            self.assertEqual(before,
                             json.loads((root / "analysis.json").read_text("utf-8")))

    def test_split_hunk_does_not_invent_old_side_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._save(root)
            feedback = apply_group_feedback(root, {
                "schema_version": "group-feedback-v1",
                "actions": [{
                    "action": "SPLIT_HUNK", "diff_id": D1,
                    "reason": "Two independently reviewed line changes",
                    "actor": "reviewer",
                    "segments": [{"start": 5, "count": 1, "group_id": G1},
                                 {"start": 6, "count": 1, "group_id": G2}]
                }]})
            self.assertIsNone(feedback["recompute_error"])
            derived = json.loads((root / "recomputed_latest.json").read_text("utf-8"))
            self.assertTrue(any(g["status"] == "OLD_UNIT_RANGE_NOT_PROVEN"
                                for g in derived["coverage_gaps"]))
            self.assertTrue(any(x["source_group"] == G2 and x["side"] == "HEAD"
                                for x in derived["impact_findings"]))
            self.assertFalse(any(x["source_group"] == G2 and x["side"] == "BASE"
                                 for x in derived["impact_findings"]))


if __name__ == "__main__":
    unittest.main()
