"""Stage 0: fixed-SHA source indexes are reusable without hiding coverage gaps."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from diff_analysis.source_index import source_snapshot_index


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args],
                                   stderr=subprocess.STDOUT).decode("utf-8").strip()


def commit(repo, name, text):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "snapshot")
    return git(repo, "rev-parse", "HEAD")


class SourceIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "user.email", "test@example.com")
        self.cache = self.root / "cache"

    def test_cache_reuse_and_fixed_sha_without_touching_worktree(self):
        first = commit(self.repo, "src/Pay.java", "class Pay {}\n")
        old, hit = source_snapshot_index(self.repo, "team/pay", first, self.cache)
        self.assertFalse(hit)
        self.assertEqual(["src/Pay.java"], [f["path"] for f in old["files"]])
        again, hit = source_snapshot_index(self.repo, "team/pay", first, self.cache)
        self.assertTrue(hit)
        self.assertEqual(old, again)

        second = commit(self.repo, "mappers/Pay.xml", "<mapper/>\n")
        new, hit = source_snapshot_index(self.repo, "team/pay", second, self.cache)
        self.assertFalse(hit)
        self.assertEqual(2, new["file_count"])
        self.assertNotEqual(old["index_key"], new["index_key"])
        (self.repo / "src" / "Pay.java").write_text("working tree is not committed")
        pinned, hit = source_snapshot_index(self.repo, "team/pay", second, self.cache)
        self.assertTrue(hit)
        self.assertEqual(new, pinned)

    def test_rule_change_and_corrupt_cache_rebuild(self):
        sha = commit(self.repo, "A.java", "class A {}\n")
        old, hit = source_snapshot_index(self.repo, "p", sha, self.cache)
        self.assertFalse(hit)
        changed, hit = source_snapshot_index(self.repo, "p", sha, self.cache,
                                             rules_version="next-parser")
        self.assertFalse(hit)
        self.assertNotEqual(old["index_key"], changed["index_key"])
        cache_file = self.cache / old["index_key"][:2] / (old["index_key"] + ".json")
        cache_file.write_text('{"payload":{"files":[]},"digest":"bad"}', encoding="utf-8")
        restored, hit = source_snapshot_index(self.repo, "p", sha, self.cache)
        self.assertFalse(hit)
        self.assertEqual(old, restored)
        record = json.loads(cache_file.read_text("utf-8"))
        self.assertEqual(restored, record["payload"])

    def test_budget_and_extension_are_part_of_identity(self):
        commit(self.repo, "A.java", "class A {}\n")
        sha = commit(self.repo, "B.sql", "select 1;\n")
        full, hit = source_snapshot_index(self.repo, "p", sha, self.cache)
        self.assertEqual(2, full["file_count"])
        limited, hit = source_snapshot_index(self.repo, "p", sha, self.cache,
                                             max_files=1)
        self.assertEqual(1, limited["file_count"])
        self.assertTrue(limited["truncated"])
        self.assertIn("SOURCE_INDEX_FILE_LIMIT",
                      [x["reason"] for x in limited["omissions"]])
        java_only, hit = source_snapshot_index(self.repo, "p", sha, self.cache,
                                               extensions={".java"})
        self.assertEqual(["A.java"], [x["path"] for x in java_only["files"]])
        self.assertNotEqual(java_only["index_key"], full["index_key"])

    def test_inconsistent_coverage_metadata_forces_cache_rebuild(self):
        from diff_analysis.source_index import _digest
        sha = commit(self.repo, "A.java", "class A {}\\n")
        initial, _ = source_snapshot_index(self.repo, "p", sha, self.cache)
        path = (self.cache / initial["index_key"][:2] /
                (initial["index_key"] + ".json"))
        wrapper = json.loads(path.read_text("utf-8"))
        wrapper["payload"]["file_count"] = 0
        wrapper["digest"] = _digest(wrapper["payload"])
        path.write_text(json.dumps(wrapper), encoding="utf-8")
        restored, hit = source_snapshot_index(self.repo, "p", sha, self.cache)
        self.assertFalse(hit)
        self.assertEqual(1, restored["file_count"])

    def test_invalid_sha_fails_instead_of_using_implicit_head(self):
        with self.assertRaises(Exception):
            source_snapshot_index(self.repo, "p", "main", self.cache)


if __name__ == "__main__":
    unittest.main()
