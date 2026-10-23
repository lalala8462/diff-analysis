"""Human grouping overlay, evidence checks and selective stale decisions."""
import json
import tempfile
import unittest
from pathlib import Path

from diff_analysis.review_workflow import export_questions, import_review
from diff_analysis.review_feedback import (
    apply_group_feedback, import_basis_checks, review_status)


SHA0, SHA1 = "1" * 40, "2" * 40
CID1, CID2 = "D-aaaaaaaaaaaaaaaa", "D-bbbbbbbbbbbbbbbb"
G1, G2 = "G-aaaaaaaaaaaaaaaa", "G-bbbbbbbbbbbbbbbb"


def release():
    return {
        "schema_version": "release-evidence-v1",
        "ledgers": [{"project": "team/service", "base": SHA0, "head": SHA1,
                     "final_changes": [
                         {"id": CID1, "project": "team/service",
                          "new_range": {"start": 4, "count": 5},
                          "old_range": {"start": 4, "count": 5}},
                         {"id": CID2, "project": "team/service",
                          "new_range": {"start": 20, "count": 1},
                          "old_range": {"start": 20, "count": 1}}
                     ]}],
        "groups": [
            {"id": G1, "change_ids": [CID1], "issue_key": "PAY-10"},
            {"id": G2, "change_ids": [CID2], "issue_key": "PAY-20"}],
        "cross_group_checks": [], "consumer_candidates": [],
        "relations": {"graphs": []}
    }


class FeedbackTests(unittest.TestCase):
    def test_split_hunk_keeps_unassigned_gap_and_raw_diff(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = release()
            (root / "analysis.json").write_text(json.dumps(data), encoding="utf-8")
            export_questions(root, data)
            result = apply_group_feedback(root, {
                "schema_version": "group-feedback-v1", "actions": [{
                    "action": "SPLIT_HUNK", "diff_id": CID1,
                    "reason": "Two requirement portions in same Hunk",
                    "actor": "reviewer",
                    "segments": [
                        {"start": 4, "count": 2, "group_id": G1},
                        {"start": 8, "count": 1, "group_id": G2}]
                }]})
            self.assertEqual(1, result["revision"])
            data_after = json.loads((root / "analysis.json").read_text("utf-8"))
            self.assertEqual(data, data_after)
            state = json.loads((root / "review_state.json").read_text("utf-8"))
            units = state["group_revisions"][0]["units"][CID1]
            self.assertEqual(3, len(units))
            self.assertTrue(any(x["unassigned"] and x["new_range"]["start"] == 6
                                for x in units))
            self.assertTrue(all(x["old_range"] is None for x in units))

    def test_split_group_creates_new_review_group_without_rewriting_ledger(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = release()
            (root / "analysis.json").write_text(json.dumps(data), encoding="utf-8")
            export_questions(root, data)
            result = apply_group_feedback(root, {
                "schema_version": "group-feedback-v1", "actions": [{
                    "action": "SPLIT_GROUP", "source_group_id": G1,
                    "diff_ids": [CID1], "new_group_label": "Separate payment feature",
                    "actor": "reviewer", "reason": "Evidence supports separate requirement"
                }]})
            self.assertEqual(1, len(result["created_groups"]))
            new_gid = result["created_groups"][0]["id"]
            self.assertTrue(new_gid.startswith("G-MAN-"))
            state = json.loads((root / "review_state.json").read_text("utf-8"))
            self.assertEqual(new_gid, state["group_revisions"][0]["units"][CID1][0]["primary_group"])
            self.assertEqual(data,
                             json.loads((root / "analysis.json").read_text("utf-8")))

    def test_overlapping_split_is_rejected_without_revision(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = release()
            (root / "analysis.json").write_text(json.dumps(data), encoding="utf-8")
            export_questions(root, data)
            payload = {
                "schema_version": "group-feedback-v1", "actions": [{
                    "action": "SPLIT_HUNK", "diff_id": CID1,
                    "reason": "Trying overlapping ownership",
                    "actor": "reviewer",
                    "segments": [
                        {"start": 4, "count": 3, "group_id": G1},
                        {"start": 6, "count": 3, "group_id": G2}]
                }]}
            with self.assertRaises(ValueError):
                apply_group_feedback(root, payload)
            self.assertFalse((root / "review_state.json").exists())

    def test_group_feedback_invalidates_related_but_not_unrelated_decisions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = release()
            (root / "analysis.json").write_text(json.dumps(data), encoding="utf-8")
            export_questions(root, data)
            decisions = {
                "schema_version": "review-import-v1",
                "decisions": [
                    {"object_type": "DIFF", "object_id": cid,
                     "dimension": "GROUPING", "actor_kind": "HUMAN",
                     "actor_id": "reviewer", "verdict": "SUPPORT",
                     "reason": "Initial grouping looked acceptable",
                     "evidence_ids": [cid]} for cid in (CID1, CID2)]}
            import_review(root, decisions)
            units_before = json.loads((root / "review_state.json").read_text("utf-8"))
            unit_id = "U-" + __import__("hashlib").sha256(
                json.dumps([CID1, "whole"], ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode()).hexdigest()[:16]
            result = apply_group_feedback(root, {
                "schema_version": "group-feedback-v1", "actions": [{
                    "action": "MOVE_UNIT", "unit_id": unit_id,
                    "target_group_id": G2, "actor": "reviewer",
                    "reason": "Belongs to second requirement"
                }]})
            self.assertEqual(1, len(result["decisions_needing_review"]))
            summary = review_status(root)
            flags = {x["object_id"]: x["current_applicability"]
                     for x in summary["current_latest"]}
            self.assertEqual("NEEDS_REVIEW", flags[CID1])
            self.assertEqual("CURRENT", flags[CID2])

    def test_independent_basis_check_does_not_overwrite_decision(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = release()
            (root / "analysis.json").write_text(json.dumps(data), encoding="utf-8")
            export_questions(root, data)
            import_review(root, {"schema_version": "review-import-v1",
                                "decisions": [{
                                    "object_type": "DIFF", "object_id": CID1,
                                    "dimension": "IMPACT", "actor_kind": "AI",
                                    "actor_id": "local-model", "verdict": "SUPPORT",
                                    "reason": "Potential matching caller exists",
                                    "evidence_ids": [CID1]}]})
            state = json.loads((root / "review_state.json").read_text("utf-8"))
            decision = state["decisions"][0]
            result = import_basis_checks(root, {
                "schema_version": "basis-checks-v1", "checks": [{
                    "decision_id": decision["id"], "actor_kind": "HUMAN",
                    "actor_id": "reviewer", "status": "NOT_SUPPORTED",
                    "reason": "Candidate edge has no real binding",
                    "evidence_ids": [CID1]}]})
            self.assertEqual(1, result["checks_added"])
            after = json.loads((root / "review_state.json").read_text("utf-8"))
            self.assertEqual(decision, after["decisions"][0])
            self.assertEqual("NOT_SUPPORTED", after["basis_checks"][0]["status"])


if __name__ == "__main__":
    unittest.main()
