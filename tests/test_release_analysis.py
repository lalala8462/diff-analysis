"""Final net diff, history provenance, candidate grouping and separate cross-group checks."""
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from diff_analysis.release_analysis import (
    collect_commits, final_diff_ledger, candidate_groups, assemble_release,
    consumer_candidates)
from diff_analysis.release_report import write_release_bundle, _register_run


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], stderr=subprocess.STDOUT
    ).decode("utf-8").strip()


def setup(repo):
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    return repo


def commit(repo, name, code, message):
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(code, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


class ReleaseEvidenceTests(unittest.TestCase):
    def setUp(self):
        mock = patch('diff_analysis.maven_inventory.default_projects', return_value=([], []))
        mock.start()
        self.addCleanup(mock.stop)

    def test_final_net_change_not_sum_of_intermediate_commits(self):
        with tempfile.TemporaryDirectory() as root:
            repo = setup(Path(root) / "repo")
            base = commit(repo, "Pay.java", "class Pay { int a = 1; }\n", "base")
            commit(repo, "Pay.java", "class Pay { int a = 2; }\n", "PAY-12 interim")
            head = commit(repo, "Pay.java", "class Pay { int a = 1; }\n", "PAY-12 rollback")
            evidence = final_diff_ledger(repo, "rhf/pay", base, head)
            self.assertEqual([], evidence["final_changes"])
            self.assertEqual(2, evidence["history"]["total"])

    def test_unique_issue_groups_multiple_commits(self):
        with tempfile.TemporaryDirectory() as root:
            repo = setup(Path(root) / "repo")
            base = commit(repo, "A.java", "class A {}\n", "base")
            commit(repo, "A.java", "class A { int a; }\n", "PAY-123 first")
            commit(repo, "B.java", "class B { int b; }\n", "PAY-123 second")
            head = commit(repo, "C.java", "class C { int c; }\n", "PAY-124 other")
            evidence = final_diff_ledger(repo, "rhf/pay", base, head)
            ids = [c["id"] for c in evidence["final_changes"]]
            self.assertEqual(len(ids), len(set(ids)))
            groups, assigned = candidate_groups([evidence])
            self.assertEqual(3, len(evidence["final_changes"]))
            self.assertEqual(2, len(groups))
            self.assertEqual(3, len(assigned))
            issue_groups = {g["issue_key"]: g for g in groups}
            self.assertEqual(2, len(issue_groups["PAY-123"]["change_ids"]))
            self.assertEqual(1, len(issue_groups["PAY-124"]["change_ids"]))

    def test_only_leading_issue_is_extracted(self):
        with tempfile.TemporaryDirectory() as root:
            repo = setup(Path(root) / "repo")
            base = commit(repo, "A.java", "class A {}\n", "base")
            commit(repo, "A.java", "class A { int a; }\n", "PAY-13 and PAY-14 mixed")
            head = commit(repo, "B.java", "class B { int b; }\n", "PAY-13 and PAY-14 mixed")
            evidence = final_diff_ledger(repo, "rhf/pay", base, head)
            groups, assigned = candidate_groups([evidence])
            self.assertEqual(1, len(groups))
            self.assertEqual({c["id"] for c in evidence["final_changes"]}, set(assigned))
            for group in groups:
                self.assertEqual("PAY-13", group["issue_key"])
                self.assertEqual(2, len(group["change_ids"]))
                self.assertEqual("CANDIDATE", group["status"])

    def test_renamed_file_has_file_level_event(self):
        with tempfile.TemporaryDirectory() as root:
            repo = setup(Path(root) / "repo")
            base = commit(repo, "Old.java", "class T {}\n", "base")
            git(repo, "mv", "Old.java", "New.java")
            git(repo, "commit", "-qm", "rename only")
            head = git(repo, "rev-parse", "HEAD")
            evidence = final_diff_ledger(repo, "rhf/pay", base, head)
            self.assertEqual(1, len(evidence["final_changes"]))
            self.assertTrue(evidence["final_changes"][0]["file_status"].startswith("R"))
            self.assertEqual("UNSUPPORTED", evidence["final_changes"][0]["status"])

    def test_version_divergence_marks_history_unavailable(self):
        with tempfile.TemporaryDirectory() as root:
            repo = setup(Path(root) / "repo")
            base = commit(repo, "A.java", "class A {}\n", "base")
            head = commit(repo, "A.java", "class A { int a; }\n", "PAY-13 first")
            # reverse direction intentionally makes base not an ancestor
            timeline = collect_commits(repo, head, base)
            self.assertEqual("BASE_NOT_ANCESTOR", timeline["status"])
            evidence = final_diff_ledger(repo, "rhf/pay", head, base)
            self.assertGreater(len(evidence["final_changes"]), 0)

    def test_single_run_evidence_and_offline_ai_reports(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            repo = setup(directory / "repo")
            base = commit(repo, "A.java", "class A {}\n", "base")
            head = commit(repo, "A.java", "class A { int a; }\n", "PAY-13 change")
            specs = [(repo, "rhf/pay", base, head)]
            out, release = write_release_bundle(
                specs, [], [], directory / "runs", directory / "analysis.sqlite3")
            self.assertTrue((out / "report.md").exists())
            self.assertTrue((out / "analysis.json").exists())
            self.assertEqual("OFFLINE_AI_INPUT", release["report_mode"])
            self.assertEqual(1, len(release["groups"]))
            group_id = release["groups"][0]["id"]
            group_text = (
                out / "group_reports" / (group_id + ".md")).read_text("utf-8")
            self.assertIn(group_id, group_text)
            self.assertIn("Git 历史", group_text)
            self.assertIn("PAY-13 change", group_text)
            ai_text = (out / "ai_review.md").read_text("utf-8")
            self.assertIn(group_id, ai_text)
            self.assertIn("旧版代码", ai_text)
            self.assertFalse((out / "model_inputs").exists())
            self.assertNotIn("model_results", release)
            change_id = release["ledgers"][0]["final_changes"][0]["id"]
            evidence = (out / "evidence" / (change_id + ".md")).read_text("utf-8")
            self.assertIn("旧版", evidence)
            self.assertIn("新版", evidence)
            self.assertIn(change_id, (out / "report.md").read_text("utf-8"))
            # Even with exact changes, no business conclusion is asserted.
            self.assertEqual("STATIC_CANDIDATES_ONLY", release["status"])

    def test_offline_report_keeps_all_changes_without_ai_budget_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = setup(Path(tmp) / "repo")
            base = commit(repo, "A.java", "class A {}\n", "base")
            head = commit(repo, "A.java", "class A { int x; }\n", "PAY-222 change")
            output, release = write_release_bundle(
                [(repo, "rhf/pay", base, head)], [], [], Path(tmp) / "runs")
            ai_text = (output / "ai_review.md").read_text(encoding="utf-8")
            ids = [c["id"] for ledger in release["ledgers"]
                   for c in ledger["final_changes"]]
            self.assertTrue(ids)
            for identifier in ids:
                self.assertIn(identifier, ai_text)
                self.assertTrue((output / "evidence" / (identifier + ".md")).is_file())
            self.assertIn("OFFLINE_AI_INPUT", (output / "analysis.json").read_text("utf-8"))
            self.assertFalse((output / "model_inputs").exists())

    def test_shared_file_creates_cross_group_review_not_forced_merge(self):
        with tempfile.TemporaryDirectory() as root:
            repo = setup(Path(root) / "repo")
            base = commit(repo, "A.java",
                          "class A {\n int a = 1;\n void separator() {}\n int b = 1;\n}\n", "base")
            mid = commit(repo, "A.java",
                         "class A {\n int a = 2;\n void separator() {}\n int b = 1;\n}\n", "PAY-111 change")
            head = commit(repo, "A.java",
                          "class A {\n int a = 2;\n void separator() {}\n int b = 2;\n}\n", "PAY-222 change")
            release = assemble_release([(repo, "rhf/pay", base, head)])
            self.assertGreaterEqual(len(release["groups"]), 2)
            self.assertTrue(any(x["kind"] == "SHARED_FILE_WEAK"
                                for x in release["cross_group_checks"]))


    def test_unmodified_consumer_kept_as_candidate(self):
        ledger = {"final_changes": [{
            "id": "D-test", "project": "rhf/pay",
            "symbols": ["x.Payment#pay()"]}]}
        P0 = [{
            "callee_project": "rhf/pay", "callee": "x.Payment#pay()",
            "caller_project": "rhf/reconciliation", "caller": "y.Job#run()",
            "path": "src/Job.java", "line": 12}]
        checks = consumer_candidates([ledger], {"D-test": "G-pay"}, P0)
        self.assertEqual(1, len(checks))
        self.assertEqual("G-pay", checks[0]["group_id"])
        self.assertEqual("rhf/reconciliation", checks[0]["consumer_project"])
        self.assertEqual("CANDIDATE", checks[0]["status"])


    def test_analysis_run_sqlite_connection_closed_on_success_and_failure(self):
        """Windows must be able to remove the SQLite file without gc.collect()."""
        connections = []
        original_connect = sqlite3.connect

        class TrackedConnection(sqlite3.Connection):
            explicitly_closed = False

            def close(self):
                self.explicitly_closed = True
                return super().close()

        def connect_tracked(*args, **kwargs):
            kwargs["factory"] = TrackedConnection
            conn = original_connect(*args, **kwargs)
            connections.append(conn)
            return conn

        with tempfile.TemporaryDirectory() as root:
            db = Path(root) / "analysis.sqlite3"
            release = {
                "status": "STATIC_CANDIDATES_ONLY",
                "report_mode": "OFFLINE_AI_INPUT",
                "project_scope": ["rhf/pay"],
                "groups": [],
                "cross_group_checks": [],
                "consumer_candidates": [],
                "created_at_utc": "2026-10-23T00:00:00Z",
            }
            with patch("sqlite3.connect", side_effect=connect_tracked):
                _register_run(db, "run-1", root, release)
                self.assertEqual(1, len(connections))
                self.assertTrue(connections[0].explicitly_closed)

                # A duplicate run ID must roll back and still close the handle.
                with self.assertRaises(sqlite3.IntegrityError):
                    _register_run(db, "run-1", root, release)
                self.assertEqual(2, len(connections))
                self.assertTrue(connections[1].explicitly_closed)

            # On Windows this would raise WinError 32 if any connection remained open.
            db.unlink()
            self.assertFalse(db.exists())


if __name__ == "__main__":
    unittest.main()
