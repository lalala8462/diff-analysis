"""Selective reverse-edge cache and goal-dependent question recomputation."""
import json
import tempfile
import unittest
from pathlib import Path

from diff_analysis.unified_pipeline import rebuild_unified
from diff_analysis.revision_pipeline import update_review_package
from diff_analysis.review_workflow import export_questions, import_review


SHA0, SHA1 = "0" * 40, "1" * 40
D1, D2 = "D-1111111111111111", "D-2222222222222222"
G1, G2 = "G-1111111111111111", "G-2222222222222222"
A, B, X, Y = "p.Pay#a()", "p.Pay#b()", "p.Pay#x()", "p.Pay#y()"


def change(cid, method, at):
    return {"id": cid, "project": "team/pay", "path": "Pay.java",
            "old_path": "Pay.java",
            "old_range": {"start": at, "count": 1},
            "new_range": {"start": at, "count": 1},
            "symbols_new": [method], "symbols_old": [method],
            "symbols": [method]}


def release():
    return {
        "schema_version": "release-evidence-v1",
        "ledgers": [{"project": "team/pay", "base": SHA0, "head": SHA1,
                     "final_changes": [change(D1, A, 2), change(D2, X, 12)]}],
        "groups": [
            {"id": G1, "change_ids": [D1], "issue_key": "PAY-1"},
            {"id": G2, "change_ids": [D2], "issue_key": "PAY-2"}],
        "cross_group_checks": [], "consumer_candidates": [],
        "relations": {"graphs": [{
            "project": "team/pay", "side": "HEAD", "snapshot_sha": SHA1,
            "method_index": {
                A: {"path": "Pay.java", "start": 2, "end": 4},
                B: {"path": "Pay.java", "start": 5, "end": 7},
                X: {"path": "Pay.java", "start": 12, "end": 14},
                Y: {"path": "Pay.java", "start": 15, "end": 17}},
            "edges": [{"id": "e-b-a", "caller": B, "callee": A,
                       "binding": "JAVAC_ATTRIBUTED"}]}]}
    }


class RevisionPipelineTests(unittest.TestCase):
    def test_binding_or_java_edge_change_recomputes_only_reachable_group(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = release()
            (root / "analysis.json").write_text(json.dumps(data), encoding="utf-8")
            first = rebuild_unified(root)
            self.assertEqual(2, first["recomputed_groups"])
            second = rebuild_unified(root)
            self.assertEqual(2, second["reused_groups"])
            data["relations"]["graphs"][0]["edges"].append({
                "id": "e-y-x", "caller": Y, "callee": X,
                "binding": "JAVAC_ATTRIBUTED"})
            (root / "analysis.json").write_text(json.dumps(data), encoding="utf-8")
            third = rebuild_unified(root)
            self.assertEqual(1, third["recomputed_groups"])
            self.assertEqual(1, third["reused_groups"])
            info = json.loads((root / "unified_impact_latest.json").read_text("utf-8"))
            self.assertIn(G2, info["coverage"]["affected_groups"])
            self.assertNotIn(G1, info["coverage"]["affected_groups"])

    def test_goal_replacement_only_replans_related_questions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = release()
            (root / "analysis.json").write_text(json.dumps(data), encoding="utf-8")
            questions = export_questions(root, data)["questions"]
            rebuild_unified(root)
            initial = update_review_package(root)
            self.assertEqual(2, initial["questions_recomputed"])
            expected = {
                "schema_version": "review-import-v1",
                "expected_goals": [{
                    "id": "GOAL-original", "status": "HUMAN_CONFIRMED",
                    "author": "reviewer", "text": "Keep older statuses accepted"
                }],
                "decisions": [{
                    "object_type": "GROUP", "object_id": G1,
                    "dimension": "CONFORMANCE", "actor_kind": "HUMAN",
                    "actor_id": "reviewer", "verdict": "SUPPORT",
                    "reason": "Initial acceptance condition was reviewed",
                    "evidence_ids": [D1],
                    "expected_goal_id": "GOAL-original"
                }]
            }
            import_review(root, expected)
            changed = {
                "schema_version": "review-import-v1",
                "expected_goals": [{
                    "id": "GOAL-revised", "status": "HUMAN_CONFIRMED",
                    "author": "reviewer",
                    "text": "Reject expired and previously valid payment state",
                    "supersedes": "GOAL-original"
                }]
            }
            outcome = import_review(root, changed)
            self.assertEqual(1, outcome["review_package"]["questions_recomputed"])
            self.assertEqual(1, outcome["review_package"]["questions_reused"])
            package = json.loads((root / "review_package_latest.json").read_text("utf-8"))
            revised = {q["group_ids"][0]: q["revision_status"]
                       for q in package["questions"]}
            self.assertEqual("REVISED", revised[G1])
            self.assertEqual("REUSED", revised[G2])
            self.assertEqual(1, len(package["decisions_to_revalidate"]))
            self.assertEqual(data, json.loads((root / "analysis.json").read_text("utf-8")))


if __name__ == "__main__":
    unittest.main()
