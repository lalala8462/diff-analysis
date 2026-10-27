"""End-to-end interactive local project revision comparison without GitLab network."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from diff_analysis.catalog import CatalogStore
from diff_analysis.wizard import run_wizard, resolve_revision


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], stderr=subprocess.STDOUT
    ).decode("utf-8").strip()


def sample_repo(folder, name):
    repo = Path(folder) / name
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    source = repo / "Pay.java"
    source.write_text(
        "class Pay { int method(){return 1;} }", encoding="utf-8")
    git(repo, "add", "Pay.java")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "V1.0")
    source.write_text(
        "class Pay { int method(){return 2;} }", encoding="utf-8")
    git(repo, "add", "Pay.java")
    git(repo, "commit", "-qm", "candidate")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "V1.1")
    return repo, base, head


def answers(seq):
    iterator = iter(seq)
    def reply(prompt):
        return next(iterator)
    return reply


class WizardTests(unittest.TestCase):
    def setUp(self):
        mock = patch('diff_analysis.maven_inventory.default_projects', return_value=([], []))
        mock.start()
        self.addCleanup(mock.stop)
        run_root = patch('diff_analysis.wizard.resolve_run_root', side_effect=lambda path: Path(path).resolve())
        run_root.start()
        self.addCleanup(run_root.stop)

    def test_git_tag_branch_sha_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, old, head = sample_repo(tmp, "project")
            self.assertEqual(old, resolve_revision(repo, "V1.0"))
            self.assertEqual(head, resolve_revision(repo, "V1.1"))
            self.assertEqual(old, resolve_revision(repo, "HEAD~1"))

    def test_two_projects_with_independent_commits(self):
        with tempfile.TemporaryDirectory() as tmp:
            one, old1, head1 = sample_repo(tmp, "one")
            two, old2, head2 = sample_repo(tmp, "two")
            folder = Path(tmp)
            args = SimpleNamespace(
                db=str(folder / "db.sqlite3"),
                output=None, json_output=None, run_root=str(folder / 'runs'))
            responses = [
                "1", str(one), "rhf/one", "V1.0", "V1.1", "", "", "",
                "1", str(two), "rhf/two", "V1.0", "V1.1", "", "", "",
                "0", "y"
            ]
            logs = []
            self.assertTrue(run_wizard(args, answers(responses), logs.append))
            runs = list((folder / 'runs').iterdir())
            self.assertEqual(1, len(runs))
            result = json.loads((runs[0] / 'analysis.json').read_text(encoding="utf-8"))
            self.assertEqual(2, len(result["comparisons"]))
            self.assertEqual(old1, result["comparisons"][0]["base_sha"])
            self.assertEqual(head1, result["comparisons"][0]["head_sha"])
            self.assertEqual(old2, result["comparisons"][1]["base_sha"])
            self.assertEqual(head2, result["comparisons"][1]["head_sha"])
            self.assertIn("输入的版本标签", (runs[0] / 'report.md').read_text(encoding="utf-8"))
            self.assertFalse((runs[0] / 'legacy_report.md').exists())
            self.assertFalse((runs[0] / 'review.json').exists())
            self.assertFalse((folder / 'report.md').exists())
            self.assertFalse((folder / 'review.json').exists())
            # The interactive route also writes a separate offline evidence bundle.
            # Its default run root is relative to the process working directory.
            self.assertTrue(any("新版候选需求与证据包" in line for line in logs))

            with CatalogStore(args.db) as store:
                self.assertEqual(2, store.coverage()["total_active"])
                self.assertEqual(old1, store.snapshot("rhf/one", "V1.0")["commit_sha"])
                self.assertEqual(old2, store.snapshot("rhf/two", "V1.0")["commit_sha"])
                self.assertIsNone(store.snapshot("rhf/one", "V1.1"))

    def test_cancel_before_confirmation_makes_no_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, _, _ = sample_repo(tmp, "one")
            folder = Path(tmp)
            args = SimpleNamespace(db=str(folder / "db.sqlite3"),
                                   output=None, json_output=None, run_root=str(folder / 'runs'))
            responses = ["1", str(repo), "rhf/one", "V1.0", "V1.1", "", "", "",
                         "0", "n"]
            self.assertFalse(run_wizard(args, answers(responses), lambda _: None))
            self.assertFalse(Path(args.db).exists())
            self.assertFalse((folder / 'runs').exists())

    def test_empty_owner_keeps_existing_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, _, _ = sample_repo(tmp, "one")
            folder = Path(tmp)
            args = SimpleNamespace(db=str(folder / "db.sqlite3"),
                                   output=None, json_output=None, run_root=str(folder / 'runs'))
            with CatalogStore(args.db) as store:
                store.add_project("rhf/one", "original-team")
            responses = ["1", str(repo), "rhf/one", "V1.0", "V1.1", "", "", "",
                         "0", "y"]
            self.assertTrue(run_wizard(args, answers(responses), lambda _: None))
            with CatalogStore(args.db) as store:
                self.assertEqual(
                    "original-team",
                    next(p["owner"] for p in store.list_projects()
                         if p["project_path"] == "rhf/one"))


    def test_gitlab_mr_mode_stores_unmerged_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, old, head = sample_repo(tmp, "one")
            folder = Path(tmp)
            args = SimpleNamespace(db=str(folder / "db.sqlite3"),
                                   output=None, json_output=None, run_root=str(folder / 'runs'))
            url = "https://gitlab.example.cn/rhf/one/-/merge_requests/11"
            mr = {"project": "rhf/one", "url": url,
                  "state": "opened", "origin": "https://gitlab.example.cn",
                  "target_branch": "V1.0", "source_branch": "V1.1",
                  "base": old, "start": old, "head": head}
            responses = ["2", url, "", "", "", "", "0", "y"]
            with patch.dict(os.environ, {"GITLAB_TOKEN": "test-token"}), \
                    patch("diff_analysis.wizard.get_mr", return_value=mr), \
                    patch("diff_analysis.wizard.target_branch_sha", return_value=old), \
                    patch("diff_analysis.wizard.clone_mr", return_value=repo):
                self.assertTrue(run_wizard(args, answers(responses), lambda _: None))
            with CatalogStore(args.db) as store:
                self.assertEqual(old, store.snapshot("rhf/one", "V1.0")["commit_sha"])
                self.assertIsNone(store.snapshot("rhf/one", "V1.1"))
                self.assertEqual("SOURCE_HEAD_ONLY", store.candidates()[0]["state"])

    def test_evidence_project_accepts_identical_sha_without_change_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, sha, _ = sample_repo(tmp, "consumer")
            root = Path(tmp)
            args = SimpleNamespace(
                db=str(root / "catalog.sqlite3"),
                output=None, json_output=None,
                run_root=str(root / "runs"))
            responses = [
                "3", str(repo), "team/consumer",
                "V1.0", "V1.0", "", "", "",
                "0", "y"
            ]
            self.assertTrue(run_wizard(args, answers(responses), lambda _: None))
            runs = list((root / "runs").iterdir())
            self.assertEqual(1, len(runs))
            data = json.loads((runs[0] / "analysis.json").read_text("utf-8"))
            self.assertEqual("EVIDENCE", data["ledgers"][0]["role"])
            self.assertEqual([], data["groups"])
            self.assertEqual(sha, data["ledgers"][0]["base"])
            self.assertEqual(sha, data["ledgers"][0]["head"])
            with CatalogStore(args.db) as store:
                self.assertIsNone(store.snapshot("team/consumer", "V1.0"))


    def test_invalid_equal_revisions_do_not_create_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, _, _ = sample_repo(tmp, "one")
            folder = Path(tmp)
            args = SimpleNamespace(db=str(folder / "db.sqlite3"),
                                   output=None, json_output=None, run_root=str(folder / 'runs'))
            # The invalid project is rejected, then q cancels the wizard.
            responses = ["1", str(repo), "rhf/one", "V1.0", "V1.0", "q"]
            lines = []
            self.assertFalse(run_wizard(args, answers(responses), lines.append))
            self.assertTrue(any("同一个 Commit" in line for line in lines))
            self.assertFalse(Path(args.db).exists())


if __name__ == "__main__":
    unittest.main()
