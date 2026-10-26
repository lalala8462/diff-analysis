"""Regression tests for reviewed persistence, minimal fixtures and snapshots."""
import json
import tempfile
import unittest
from pathlib import Path

from diff_analysis.review_workflow import export_questions
from diff_analysis.unified_pipeline import rebuild_unified
from diff_analysis.revision_pipeline import update_review_package


BASE, HEAD = "a" * 40, "b" * 40
DID, GID = "D-1234567890abcdef", "G-1234567890abcdef"


def minimal_release():
    return {
        "schema_version": "release-evidence-v1",
        "groups": [{"id": GID, "change_ids": [DID], "issue_key": None}],
        "ledgers": [{"project": "team/pay", "base": BASE, "head": HEAD,
                     "final_changes": [{"id": DID, "project": "team/pay",
                                        "base_sha": BASE, "head_sha": HEAD,
                                        "symbols_old": [], "symbols_new": []}]}],
        "cross_group_checks": [], "consumer_candidates": [],
        "relations": {"graphs": []}
    }


class HardeningTests(unittest.TestCase):
    def _setup(self, root):
        release = minimal_release()
        (root / "analysis.json").write_text(json.dumps(release), encoding="utf-8")
        export_questions(root, release)
        return release

    def test_question_export_handles_optional_path_and_uses_ledger_versions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self._setup(root)
            report = (root / "questions.md").read_text(encoding="utf-8")
            self.assertIn(BASE + " → " + HEAD, report)
            self.assertIn("(路径未记录)", report)

    def test_repeated_review_package_keeps_distinct_snapshots(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self._setup(root)
            rebuild_unified(root)
            first = update_review_package(root)
            self.assertEqual(1, first["questions_recomputed"])
            second = update_review_package(root)
            self.assertEqual(1, second["questions_reused"])
            snapshots = list((root / "revisions").glob("review-000-000-*.json"))
            self.assertGreaterEqual(len(snapshots), 2)
            original = json.loads((root / "analysis.json").read_text("utf-8"))
            self.assertEqual(minimal_release(), original)

    def test_explicit_group_units_keep_revision_in_unified_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self._setup(root)
            units = {DID: [{
                "id": "U-1234567890abcdef",
                "primary_group": GID, "range_basis": "WHOLE_RAW_CHANGE",
                "old_range": {"start": 1, "count": 1},
                "new_range": {"start": 1, "count": 1},
                "unassigned": False
            }]}
            (root / "review_state.json").write_text(json.dumps({
                "schema_version": "review-state-v1",
                "expected_goals": [], "rule_points": [],
                "decisions": [], "basis_checks": [],
                "group_revisions": [{"revision": 1, "units": units}],
                "goal_revision_events": []
            }), encoding="utf-8")
            rebuild_unified(root, units=units, affected={GID})
            data = json.loads((root / "unified_impact_latest.json").read_text("utf-8"))
            self.assertEqual(1, data["revision"])
            files = list((root / "revisions").glob("unified-impact-001-*.json"))
            self.assertEqual(1, len(files))


if __name__ == "__main__":
    unittest.main()
