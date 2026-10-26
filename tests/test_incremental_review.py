"""Incremental grouping re-propagation without changing pinned graph facts."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from diff_analysis.review_feedback import apply_group_feedback
from diff_analysis.review_workflow import export_questions, import_review, _hash, _write_json
from diff_analysis.review_feedback import review_status

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
            self.assertTrue(any(x["to_group"] == G2 for x in impact["direct_group_checks"]))
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
                                and x["source_symbol"] == SB
                                for x in derived["impact_findings"]))
            self.assertFalse(any(x["source_group"] == G2 and x["side"] == "BASE"
                                 and x["source_symbol"] == SB
                                 for x in derived["impact_findings"]))
            self.assertTrue(any(x["source_group"] == G2 and x["side"] == "BASE"
                                and x["source_symbol"] == SA
                                for x in derived["impact_findings"]))

    def test_unrelated_second_revision_preserves_manual_group_question(self):
        for missing_prior in (False, True):
            with self.subTest(missing_prior=missing_prior), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                original = self._save(root)

                def save(path, data):
                    if missing_prior and path.name == "recomputed-001.json":
                        return
                    _write_json(path, data)

                with patch("diff_analysis.incremental_review._write_json", side_effect=save):
                    first = apply_group_feedback(root, {
                        "schema_version": "group-feedback-v1", "actions": [{
                            "action": "SPLIT_GROUP", "source_group_id": G1,
                            "diff_ids": [D1], "new_group_label": "独立人工需求",
                            "actor": "reviewer", "reason": "根据业务证据拆分需求"}]})
                self.assertIsNone(first["recompute_error"])
                manual = first["created_groups"][0]["id"]
                before = json.loads((root / "review_package_latest.json").read_text("utf-8"))
                question = next(q for q in before["questions"] if manual in q["group_ids"])
                import_review(root, {"schema_version": "review-import-v1", "decisions": [{
                    "object_type": "QUESTION", "object_id": question["id"],
                    "dimension": "IMPACT", "actor_kind": "HUMAN", "actor_id": "reviewer",
                    "verdict": "SUPPORT", "reason": "已核对人工组对应变化与消费者",
                    "evidence_ids": [D1]}]})
                second = apply_group_feedback(root, {
                    "schema_version": "group-feedback-v1", "actions": [{
                        "action": "MOVE_UNIT", "unit_id": "U-" + _hash([D2, "whole"])[:16],
                        "target_group_id": G3, "actor": "reviewer", "reason": "修正另一组的需求归属"}]})
                self.assertIsNone(second["recompute_error"])
                after = json.loads((root / "review_package_latest.json").read_text("utf-8"))
                retained = next(q for q in after["questions"] if manual in q["group_ids"])
                self.assertEqual([D1], retained["change_ids"])
                self.assertFalse(any(G1 in q["group_ids"] for q in after["questions"]))
                if not missing_prior:
                    self.assertEqual(question["id"], retained["id"])
                    self.assertEqual("CURRENT", review_status(root)["current_latest"][0]["current_applicability"])
                self.assertEqual(original, json.loads((root / "analysis.json").read_text("utf-8")))


if __name__ == "__main__":
    unittest.main()
