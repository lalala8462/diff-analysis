"""P1-B regression: catalog, pinned baselines, candidate status and drift safety."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from diff_analysis.baseline import prepare_baseline, prepare_mr_baseline
from diff_analysis.catalog import CatalogStore, BaselineError


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], stderr=subprocess.STDOUT
    ).decode("utf-8").strip()


def history(root):
    repo = Path(root) / "sample"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "Pay.java").write_text(
        "class Pay { int read(){return 1;} }", encoding="utf-8")
    git(repo, "add", "Pay.java")
    git(repo, "commit", "-qm", "V1.0")
    old = git(repo, "rev-parse", "HEAD")
    (repo / "Pay.java").write_text(
        "class Pay { int read(){return 2;} }", encoding="utf-8")
    git(repo, "add", "Pay.java")
    git(repo, "commit", "-qm", "V1.1")
    return repo, old, git(repo, "rev-parse", "HEAD")


class CatalogTests(unittest.TestCase):
    def test_user_scope_revision_and_missing_project(self):
        with tempfile.TemporaryDirectory() as td:
            with CatalogStore(Path(td) / "state.sqlite3") as db:
                self.assertEqual(0, db.revision())
                self.assertEqual(1, db.add_project("rhf/spms-back"))
                self.assertEqual(1, db.add_project("rhf/spms-back"))
                self.assertEqual(2, db.add_project("rhf/reconciliation-job"))
                self.assertEqual(2, db.coverage()["total_active"])
                self.assertFalse(all(p["has_snapshot"] for p in db.coverage()["projects"]))
                with self.assertRaises(BaselineError):
                    db.require_project("rhf/not-listed")

    def test_unsafe_project_and_version(self):
        with tempfile.TemporaryDirectory() as td:
            with CatalogStore(Path(td) / "state.sqlite3") as db:
                for path in ("../etc", "rhf/../payments", "https://gitlab/rhf/x"):
                    with self.assertRaises(BaselineError):
                        db.add_project(path)


class BaselineTests(unittest.TestCase):
    def test_initial_scan_reuses_old_version_and_rejects_sha_drift(self):
        with tempfile.TemporaryDirectory() as td:
            repo, old, head = history(td)
            with CatalogStore(Path(td) / "state.sqlite3") as db:
                db.add_project("rhf/pay")
                baseline, created = prepare_baseline(db, "rhf/pay", "V1.0", repo, old)
                self.assertTrue(created)
                self.assertEqual(old, baseline["commit_sha"])
                self.assertEqual("PROVISIONAL", baseline["status"])
                self.assertTrue(baseline["methods"])
                self.assertEqual(1, len(baseline["inventory"]))
                self.assertTrue(db.coverage()["projects"][0]["has_snapshot"])
                cached, created = prepare_baseline(db, "rhf/pay", "V1.0", repo, old)
                self.assertFalse(created)
                self.assertEqual(old, cached["commit_sha"])
                with self.assertRaisesRegex(BaselineError, "BASELINE_DIVERGED"):
                    prepare_baseline(db, "rhf/pay", "V1.0", repo, head)
                self.assertEqual(old, db.snapshot("rhf/pay", "V1.0")["commit_sha"])
                other, _ = prepare_baseline(db, "rhf/pay", "V1.1", repo, head)
                self.assertEqual(head, other["commit_sha"])
                verified = db.verify("rhf/pay", "V1.0")
                self.assertEqual("VERIFIED", verified["status"])
                self.assertIsNotNone(verified["verified_at"])

    def test_open_mr_creates_target_baseline_not_merge_base(self):
        with tempfile.TemporaryDirectory() as td:
            repo, old, head = history(td)
            data = {"url": "https://gitlab.example.cn/rhf/pay/-/merge_requests/1",
                    "project": "rhf/pay", "origin": "https://gitlab.example.cn",
                    "state": "opened", "start": old, "base": old, "head": head,
                    "target_branch": "V1.0"}
            with CatalogStore(Path(td) / "state.sqlite3") as db:
                db.add_project("rhf/pay")
                with patch("diff_analysis.baseline.get_mr", return_value=data), \
                     patch("diff_analysis.baseline.clone_mr", return_value=repo), \
                     patch("diff_analysis.baseline.target_branch_sha", return_value=old):
                    result = prepare_mr_baseline(
                        db, data["url"], "V1.0", "V1.1",
                        "gitlab.example.cn", "dummy")
                self.assertEqual(old, result["snapshot"]["commit_sha"])
                self.assertEqual(head, result["candidate"]["source_head_sha"])
                self.assertEqual("SOURCE_HEAD_ONLY", result["candidate"]["state"])
                self.assertIsNone(db.snapshot("rhf/pay", "V1.1"))

    def test_open_mr_rejects_changed_target(self):
        with tempfile.TemporaryDirectory() as td:
            repo, old, head = history(td)
            data = {"url": "https://gitlab.example.cn/rhf/pay/-/merge_requests/1",
                    "project": "rhf/pay", "origin": "https://gitlab.example.cn",
                    "state": "opened", "start": old, "base": old, "head": head,
                    "target_branch": "V1.0"}
            with CatalogStore(Path(td) / "state.sqlite3") as db:
                db.add_project("rhf/pay")
                with patch("diff_analysis.baseline.get_mr", return_value=data), \
                     patch("diff_analysis.baseline.target_branch_sha", return_value=head):
                    with self.assertRaisesRegex(BaselineError, "TARGET_MOVED"):
                        prepare_mr_baseline(
                            db, data["url"], "V1.0", "V1.1",
                            "gitlab.example.cn", "dummy")
                self.assertEqual([], db.snapshots())


if __name__ == "__main__":
    unittest.main()
