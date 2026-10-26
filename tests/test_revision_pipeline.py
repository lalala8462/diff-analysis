"""Selective reverse-edge cache and goal-dependent question recomputation."""
import json
import tempfile
import unittest
from pathlib import Path

from diff_analysis.unified_pipeline import rebuild_unified
from diff_analysis.revision_pipeline import update_review_package
from diff_analysis.review_workflow import export_questions, import_review
from diff_analysis.review_feedback import review_status, apply_group_feedback


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
    def test_first_group_revision_keeps_unrelated_impact_decisions_current(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = release()
            (root / "analysis.json").write_text(json.dumps(data), encoding="utf-8")
            export_questions(root, data)
            rebuild_unified(root)
            import_review(root, {"schema_version": "review-import-v1", "decisions": [
                {"object_type": obj, "object_id": oid, "dimension": "IMPACT",
                 "actor_kind": "HUMAN", "actor_id": "reviewer", "verdict": "SUPPORT",
                 "reason": "已核对该对象原始影响范围", "evidence_ids": [did]}
                for obj, oid, did in (("GROUP", G1, D1), ("DIFF", D1, D1),
                                      ("GROUP", G2, D2), ("DIFF", D2, D2))]})
            result = apply_group_feedback(root, {"schema_version": "group-feedback-v1", "actions": [{
                "action": "SPLIT_GROUP", "source_group_id": G1, "diff_ids": [D1],
                "new_group_label": "独立人工需求", "actor": "reviewer", "reason": "根据业务证据拆分第一组"}]})
            self.assertIsNone(result["recompute_error"])
            flags = {(d["object_type"], d["object_id"]): d["current_applicability"]
                     for d in review_status(root)["current_latest"]}
            self.assertEqual("NEEDS_REVIEW", flags[("GROUP", G1)])
            self.assertEqual("NEEDS_REVIEW", flags[("DIFF", D1)])
            self.assertEqual("CURRENT", flags[("GROUP", G2)])
            self.assertEqual("CURRENT", flags[("DIFF", D2)])

    def test_binding_change_invalidates_related_group_and_diff_decisions(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                data = release()
                consumer = "p.Consumer#run()"
                data["ledgers"].append({"project": "team/consumer", "base": SHA0,
                                        "head": SHA1, "final_changes": []})
                data["relations"]["graphs"].append({
                    "project": "team/consumer", "side": "HEAD", "snapshot_sha": SHA1,
                    "method_index": {consumer: {"path": "Consumer.java", "start": 3, "end": 5}},
                    "edges": []})
                (root / "analysis.json").write_text(json.dumps(data), encoding="utf-8")
                export_questions(root, data)
                rebuild_unified(root)
                update_review_package(root)
                payload = {"schema_version": "review-import-v1", "decisions": [
                    {"object_type": obj, "object_id": oid, "dimension": "IMPACT",
                     "actor_kind": "HUMAN", "actor_id": "reviewer", "verdict": "SUPPORT",
                     "reason": "已核对当前固定版本的影响范围", "evidence_ids": [did]}
                    for obj, oid, did in (("GROUP", G1, D1), ("DIFF", D1, D1),
                                          ("GROUP", G2, D2), ("DIFF", D2, D2))]}
                import_review(root, payload)
                if legacy:
                    state_path = root / "review_state.json"
                    state = json.loads(state_path.read_text("utf-8"))
                    for decision in state["decisions"]:
                        decision.pop("object_dependency_fingerprint", None)
                    state_path.write_text(json.dumps(state), encoding="utf-8")
                bindings = {
                    "endpoints": [
                        {"id": "provider", "project": "team/pay", "side": "HEAD",
                         "sha": SHA1, "role": "PROVIDER",
                         "evidence": {"path": "Pay.java", "start": 2, "end": 2}},
                        {"id": "consumer", "project": "team/consumer", "side": "HEAD",
                         "sha": SHA1, "role": "CONSUMER",
                         "evidence": {"path": "Consumer.java", "start": 3, "end": 3}}],
                    "edges": [{"id": "connection", "kind": "DUBBO", "side": "HEAD",
                               "caller_endpoint": "consumer", "callee_endpoint": "provider",
                               "caller_sha": SHA1, "callee_sha": SHA1,
                               "binding_status": "USER_ATTESTED_MATCH", "evidence_refs": []}]}
                outcome = rebuild_unified(root, binding_report=bindings)
                self.assertGreater(outcome["cross_project_questions"], 0)
                # 重复刷新也不能把没有指纹的旧判断恢复为有效。
                for _ in range(2):
                    update_review_package(root)
                    flags = {(d["object_type"], d["object_id"]): d["current_applicability"]
                             for d in review_status(root)["current_latest"]}
                    self.assertEqual("NEEDS_REVIEW", flags[("GROUP", G1)])
                    self.assertEqual("NEEDS_REVIEW", flags[("DIFF", D1)])
                    self.assertEqual("CURRENT", flags[("GROUP", G2)])
                    self.assertEqual("CURRENT", flags[("DIFF", D2)])
                import_review(root, {**payload, "decisions": payload["decisions"][:2]})
                update_review_package(root)
                self.assertTrue(all(d["current_applicability"] == "CURRENT"
                                    for d in review_status(root)["current_latest"]))
                self.assertEqual(data, json.loads((root / "analysis.json").read_text("utf-8")))

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
