"""Stage 4: resource-fact persistence and exact ReviewUnit overlay invalidation."""
import json
import tempfile
import unittest
from pathlib import Path

from diff_analysis.resource_review import (
    normalized_candidates, refresh_resource_review)


class ResourceReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.release = {
            "ledgers": [{"project": "producer", "base": "a" * 40,
                         "head": "b" * 40,
                         "final_changes": [
                             {"id": "D-1", "path": "A.java", "old_path": "A.java",
                              "old_range": {"start": 9, "count": 2},
                              "new_range": {"start": 10, "count": 2}},
                             {"id": "D-2", "path": "B.sql", "old_path": "B.sql",
                              "old_range": {"start": 2, "count": 1},
                              "new_range": {"start": 2, "count": 1}}]}],
            "groups": [{"id": "G-A", "change_ids": ["D-1"]},
                       {"id": "G-B", "change_ids": ["D-2"]}]
        }
        candidates = [
            {"id": "R-1", "kind": "SPRING_BEAN", "resource": "legacy",
             "producer_project": "producer", "consumer_project": "webapi",
             "consumer_sha": "c" * 40, "change_ids": ["D-1"],
             "source_uses": [{"path": "A.java", "side": "head",
                              "start": 10, "end": 10}],
             "consumer_uses": [{"path": "C.java", "start": 3, "end": 3}]},
            {"id": "R-2", "kind": "DATABASE_TABLE", "resource": "trans_record",
             "producer_project": "producer", "consumer_project": "adpts",
             "consumer_sha": "d" * 40, "change_ids": ["D-2"],
             "source_uses": [{"path": "B.sql", "side": "head",
                              "start": 2, "end": 2}],
             "consumer_uses": [{"path": "Read.sql", "start": 1, "end": 1}]}]
        self.facts = {"schema_version": "resource-impact-index-v1",
                      "candidates": candidates}
        (self.root / "resource_impact_latest.json").write_text(
            json.dumps(self.facts), encoding="utf-8")

    def test_initial_assignments_and_stable_reuse(self):
        initial = refresh_resource_review(self.root, self.release)
        self.assertEqual(2, initial["groups"])
        self.assertEqual(2, initial["questions"])
        self.assertEqual(2, initial["recomputed_groups"])
        again = refresh_resource_review(self.root, self.release)
        self.assertEqual(2, again["reused_groups"])
        latest = json.loads((self.root / "resource_review_latest.json").read_text("utf-8"))
        self.assertEqual({"G-A", "G-B"}, set(latest["groups"]))
        self.assertEqual({"RESOURCE_CONSUMER"},
                         {q["kind"] for q in latest["questions"]})
        self.assertEqual("R-1", latest["groups"]["G-A"]["candidates"][0]["candidate_id"])
        self.assertEqual(self.facts, json.loads(
            (self.root / "resource_impact_latest.json").read_text("utf-8")))

    def test_split_reassigns_only_overlapping_head_resource(self):
        refresh_resource_review(self.root, self.release)
        units = {
            "D-1": [
                {"id": "U-C", "primary_group": "G-C", "range_basis": "NEW_LINE_SEGMENT_ONLY",
                 "unassigned": False, "new_range": {"start": 10, "count": 1},
                 "old_range": None},
                {"id": "U-A", "primary_group": "G-A", "range_basis": "NEW_LINE_SEGMENT_ONLY",
                 "unassigned": False, "new_range": {"start": 11, "count": 1},
                 "old_range": None}],
            "D-2": [
                {"id": "U-B", "primary_group": "G-B", "range_basis": "WHOLE_RAW_CHANGE",
                 "unassigned": False, "new_range": {"start": 2, "count": 1},
                 "old_range": {"start": 2, "count": 1}}]}
        self.assertEqual(1, refresh_resource_review(
            self.root, self.release, units=units, affected={"G-A", "G-C"})["reused_groups"])
        latest = json.loads((self.root / "resource_review_latest.json").read_text("utf-8"))
        self.assertIn("G-C", latest["groups"])
        self.assertNotIn("G-A", latest["groups"])
        self.assertEqual("REUSED", latest["groups"]["G-B"]["recompute_status"])
        self.assertTrue(any(g["reason"] == "SOURCE_NOT_IN_EXACT_UNIT_RANGE"
                            for g in latest["coverage_gaps"]))

    def test_old_side_only_is_not_assigned_after_new_line_split(self):
        self.facts["candidates"][0]["source_uses"] = [
            {"path": "A.java", "side": "base", "start": 9, "end": 9}]
        (self.root / "resource_impact_latest.json").write_text(
            json.dumps(self.facts), encoding="utf-8")
        units = {"D-1": [
            {"id": "U-C", "primary_group": "G-C", "range_basis": "NEW_LINE_SEGMENT_ONLY",
             "unassigned": False, "new_range": {"start": 10, "count": 1}}],
            "D-2": [{"id": "U-B", "primary_group": "G-B", "range_basis": "WHOLE_RAW_CHANGE",
                     "unassigned": False}]}
        refresh_resource_review(self.root, self.release, units=units)
        latest = json.loads((self.root / "resource_review_latest.json").read_text("utf-8"))
        self.assertNotIn("G-C", latest["groups"])
        self.assertTrue(latest["coverage_gaps"])

    def test_review_package_ingests_resource_questions_and_evidence_plan(self):
        from diff_analysis.revision_pipeline import update_review_package
        self.release["schema_version"] = "release-evidence-v1"
        (self.root / "analysis.json").write_text(
            json.dumps(self.release), encoding="utf-8")
        (self.root / "questions.json").write_text(
            json.dumps({"questions": []}), encoding="utf-8")
        refresh_resource_review(self.root, self.release)
        first = update_review_package(self.root)
        self.assertEqual(2, first["questions"])
        package = json.loads((self.root / "review_package_latest.json").read_text("utf-8"))
        self.assertTrue(any(q["kind"] == "RESOURCE_CONSUMER"
                            for q in package["questions"]))
        self.assertTrue(any(x["kind"] == "RESOURCE_CONSUMER"
                            for x in package["evidence_plan"]))
        again = update_review_package(self.root)
        self.assertEqual(2, again["questions_reused"])

    def test_same_resource_identity_merges_all_change_evidence(self):
        def impact(change_id, line):
            return {"kind": "REDIS_KEY", "resource": "policy:",
                    "producer_project": "spms", "consumer_project": "webapi",
                    "base_sha": "a" * 40, "head_sha": "b" * 40,
                    "consumer_sha": "c" * 40, "change_ids": [change_id],
                    "source_uses": [{"path": "Cache.java", "start": line, "end": line,
                                      "side": "head"}],
                    "consumer_uses": [{"path": "Read.java", "start": line, "end": line}],
                    "status": "STATIC_RESOURCE_NAME_ONLY_UNCONFIRMED"}
        normalized = normalized_candidates({
            "shared_resources": {"impacts": [impact("D-1", 3),
                                             impact("D-2", 7)]}})
        self.assertEqual(1, len(normalized["candidates"]))
        item = normalized["candidates"][0]
        self.assertEqual(["D-1", "D-2"], item["change_ids"])
        self.assertEqual(2, len(item["consumer_uses"]))

    def test_cross_resource_kind_candidates_are_distinct(self):
        db = {
            "database": {"impacts": [{
                "table": "accounting", "producer_project": "spms",
                "consumer_project": "webapi", "producer_base_sha": "a" * 40,
                "producer_head_sha": "b" * 40, "consumer_sha": "c" * 40,
                "change_ids": ["D-1"], "producer_uses": [],
                "consumer_uses": [], "status": "TABLE_NAME_ONLY_UNCONFIRMED"}]},
            "shared_resources": {"impacts": [{
                "kind": "REDIS_KEY", "resource": "accounting",
                "producer_project": "spms", "consumer_project": "webapi",
                "base_sha": "a" * 40, "head_sha": "b" * 40,
                "consumer_sha": "c" * 40, "change_ids": ["D-1"],
                "source_uses": [], "consumer_uses": [],
                "status": "STATIC_RESOURCE_NAME_ONLY_UNCONFIRMED"}]}}
        indexed = normalized_candidates(db)
        self.assertEqual(2, len(indexed["candidates"]))
        self.assertEqual(2, len({x["id"] for x in indexed["candidates"]}))
        self.assertEqual({"DATABASE_TABLE", "REDIS_KEY"},
                         {x["kind"] for x in indexed["candidates"]})


if __name__ == "__main__":
    unittest.main()
