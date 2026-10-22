"""End-to-end interactive local project revision comparison without GitLab network."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

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
                output=str(folder / "report.md"),
                json_output=str(folder / "report.json"))
            responses = [
                "1", str(one), "rhf/one", "V1.0", "V1.1", "", "", "",
                "1", str(two), "rhf/two", "V1.0", "V1.1", "", "", "",
                "0", "y"
            ]
            logs = []
            self.assertTrue(run_wizard(args, answers(responses), logs.append))
            result = json.loads(Path(args.json_output).read_text(encoding="utf-8"))
            self.assertEqual(2, len(result["comparisons"]))
            self.assertEqual(old1, result["comparisons"][0]["base_sha"])
            self.assertEqual(head1, result["comparisons"][0]["head_sha"])
            self.assertEqual(old2, result["comparisons"][1]["base_sha"])
            self.assertEqual(head2, result["comparisons"][1]["head_sha"])
            self.assertIn("本次输入的项目及修订号",
                          Path(args.output).read_text(encoding="utf-8"))
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
                                   output=str(folder / "report.md"),
                                   json_output=str(folder / "report.json"))
            responses = ["1", str(repo), "rhf/one", "V1.0", "V1.1", "", "", "",
                         "0", "n"]
            self.assertFalse(run_wizard(args, answers(responses), lambda _: None))
            self.assertFalse(Path(args.db).exists())
            self.assertFalse(Path(args.output).exists())

    def test_empty_owner_keeps_existing_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, _, _ = sample_repo(tmp, "one")
            folder = Path(tmp)
            args = SimpleNamespace(db=str(folder / "db.sqlite3"),
                                   output=str(folder / "report.md"),
                                   json_output=str(folder / "report.json"))
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


if __name__ == "__main__":
    unittest.main()
