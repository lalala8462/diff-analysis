"""P1-C/P2-A offline review workflow tests without AI or business build."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from diff_analysis.review_workflow import (
    build_questions, export_questions, import_review, supplement)


BASE, HEAD = "a" * 40, "b" * 40


def sample_release():
    diff = {"id": "D-1234567890abcdef", "project": "team/pay",
            "base_sha": BASE, "head_sha": HEAD}
    group = {"id": "G-1234567890abcdef", "change_ids": [diff["id"]],
             "issue_key": "PAY-21"}
    return {
        "schema_version": "release-evidence-v1",
        "ledgers": [{"project": "team/pay", "base": BASE, "head": HEAD,
                     "final_changes": [diff]}],
        "groups": [group], "cross_group_checks": [],
        "consumer_candidates": [],
        "relations": {"graphs": []}
    }


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], stderr=subprocess.STDOUT
    ).decode("utf-8").strip()


class OfflineWorkflowTests(unittest.TestCase):
    def test_questions_not_auto_verdicts_or_complete_evidence(self):
        data = build_questions(sample_release())
        self.assertEqual(1, len(data["questions"]))
        q = data["questions"][0]
        self.assertEqual("NEEDS_REVIEW", q["status"])
        self.assertEqual("NOT_IMPORTED", q["ai_status"])
        self.assertEqual("NOT_VERIFIED", data["evidence_manifest"]["METHOD"]["coverage"])
        self.assertEqual("UNKNOWN", data["reading_scope"]["external_ai"])

    def test_import_refuses_unscoped_reference_and_unconfirmed_goal(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            release = sample_release()
            (root / "analysis.json").write_text(json.dumps(release), encoding="utf-8")
            questions = export_questions(root, release)
            qid = questions["questions"][0]["id"]
            positive = {"object_type": "QUESTION", "object_id": qid,
                        "actor_kind": "AI", "actor_id": "offline-model",
                        "dimension": "CONFORMANCE", "verdict": "SUPPORT",
                        "reason": "Behavior may match the expected goal",
                        "evidence_ids": ["D-1234567890abcdef"]}
            with self.assertRaises(ValueError):
                import_review(root, {"schema_version": "review-import-v1",
                                     "decisions": [positive]})
            positive["dimension"] = "IMPACT"
            positive["evidence_ids"] = ["D-does-not-exist"]
            with self.assertRaises(ValueError):
                import_review(root, {"schema_version": "review-import-v1",
                                     "decisions": [positive]})
            self.assertFalse((root / "review_state.json").exists())

    def test_separate_ai_and_human_and_explicit_confirmed_goal(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            release = sample_release()
            (root / "analysis.json").write_text(json.dumps(release), encoding="utf-8")
            qid = export_questions(root, release)["questions"][0]["id"]
            item = {
                "schema_version": "review-import-v1",
                "expected_goals": [{
                    "id": "GOAL-payment-1", "status": "HUMAN_CONFIRMED",
                    "author": "reviewer", "text": "Keep accepting previously valid statuses"
                }],
                "decisions": [
                    {"object_type": "QUESTION", "object_id": qid,
                     "actor_kind": "AI", "actor_id": "local-model",
                     "dimension": "CONFORMANCE", "verdict": "SUPPORT",
                     "reason": "Derived from change-window evidence",
                     "expected_goal_id": "GOAL-payment-1",
                     "evidence_ids": ["D-1234567890abcdef"]},
                    {"object_type": "QUESTION", "object_id": qid,
                     "actor_kind": "HUMAN", "actor_id": "reviewer",
                     "dimension": "CONFORMANCE", "verdict": "REJECT",
                     "reason": "Consumer behavior still unconfirmed",
                     "expected_goal_id": "GOAL-payment-1",
                     "evidence_ids": ["D-1234567890abcdef"]}
                ]
            }
            outcome = import_review(root, item)
            self.assertEqual(2, outcome["decisions_added"])
            state = json.loads((root / "review_state.json").read_text("utf-8"))
            self.assertEqual(["AI", "HUMAN"],
                             [x["actor_kind"] for x in state["decisions"]])
            self.assertTrue(all(x["basis_check"] == "NOT_REVIEWED"
                                for x in state["decisions"]))

    def test_pinned_supplement_and_duplicate_no_progress(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo = root / "git"
            repo.mkdir()
            git(repo, "init", "-q")
            git(repo, "config", "user.name", "Test")
            git(repo, "config", "user.email", "test@example.com")
            (repo / "Pay.java").write_text(
                "class Pay {\n void pay() {}\n}\n", encoding="utf-8")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "base")
            real_sha = git(repo, "rev-parse", "HEAD")
            release = sample_release()
            release["ledgers"][0]["base"] = real_sha
            release["ledgers"][0]["head"] = real_sha
            (root / "analysis.json").write_text(json.dumps(release), encoding="utf-8")
            qid = export_questions(root, release)["questions"][0]["id"]
            request = {"schema_version": "evidence-requests-v1",
                       "selectors": [{
                           "question_id": qid, "project": "team/pay",
                           "sha": real_sha, "side": "BASE",
                           "path": "Pay.java", "start": 1, "end": 3,
                           "reason": "Need full consumer method context"
                       }]}
            first = supplement(root, request, {"team/pay": repo})
            self.assertEqual("PROVIDED", first["results"][0]["status"])
            self.assertEqual(1, first["new_evidence"])
            second = supplement(root, request, {"team/pay": repo})
            self.assertEqual(0, second["new_evidence"])
            self.assertEqual("NO_NEW_RELEVANT_EVIDENCE", second["stop_reason"])
            request["selectors"][0]["sha"] = "f" * 40
            third = supplement(root, request, {"team/pay": repo})
            self.assertEqual("OUT_OF_SCOPE", third["results"][0]["status"])

    def test_reject_path_traversal_without_git_execution(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            rel = sample_release()
            (root / "analysis.json").write_text(json.dumps(rel), encoding="utf-8")
            qid = export_questions(root, rel)["questions"][0]["id"]
            invalid = {"schema_version": "evidence-requests-v1",
                       "selectors": [{
                           "question_id": qid, "project": "team/pay",
                           "sha": BASE, "side": "BASE", "path": "../secret",
                           "start": 1, "end": 2,
                           "reason": "Need full method context"
                       }]}
            with self.assertRaises(ValueError):
                supplement(root, invalid, {"team/pay": root})
            self.assertFalse((root / "supplement_state.json").exists())


if __name__ == "__main__":
    unittest.main()
