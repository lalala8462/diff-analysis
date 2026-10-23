"""P1-B explicit EVIDENCE role and both-side version pins."""
import tempfile
import unittest
from pathlib import Path

from diff_analysis.release_analysis import candidate_groups
from diff_analysis.release_report import start_ledger_checkpoint
import subprocess


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args],
                                   stderr=subprocess.STDOUT).decode("utf-8").strip()


def setup(repo):
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    return repo


def commit(repo, filename, code, message):
    (repo / filename).write_text(code, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


class EvidenceRoleTests(unittest.TestCase):
    def test_same_sha_evidence_does_not_create_needless_requirement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = setup(root / "repo")
            sha = commit(repo, "Pay.java", "class Pay {}\n", "baseline")
            specs = [(repo, "team/evidence", sha, sha)]
            folder, manifest = start_ledger_checkpoint(
                specs, root / "runs",
                {("team/evidence", sha, sha): "EVIDENCE"})
            self.assertEqual("SAVED", manifest["state"])
            self.assertEqual("EVIDENCE", manifest["ledgers"][0]["role"])
            self.assertEqual([], manifest["ledgers"][0]["final_changes"])
            groups, grouped = candidate_groups(manifest["ledgers"])
            self.assertEqual([], groups)
            self.assertEqual({}, grouped)

    def test_evidence_change_is_inventory_not_release_requirement(self):
        changes = [{"id": "D-aaaaaaaaaaaaaaaa", "project": "team/evidence",
                    "issue_candidates": ["ABC-21"], "line_issue_candidates": [],
                    "line_commit_candidates": []}]
        groups, mapping = candidate_groups([{
            "project": "team/evidence", "role": "EVIDENCE",
            "final_changes": changes}])
        self.assertEqual([], groups)
        self.assertEqual({}, mapping)


if __name__ == "__main__":
    unittest.main()
