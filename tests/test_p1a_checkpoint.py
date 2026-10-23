"""P1-A durable Git ledger and per-project scan failure isolation."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from diff_analysis.cli import run
from diff_analysis.release_report import (start_ledger_checkpoint, write_release_bundle,
                                          rebuild_from_checkpoint)
from diff_analysis.release_analysis import final_diff_ledger, attach_review_to_ledger
from diff_analysis.scanner import ScannerError


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], stderr=subprocess.STDOUT
    ).decode("utf-8").strip()


def sample_repo(root, name):
    repo = root / name
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    target = repo / "Pay.java"
    target.write_text("class Pay { int amount() { return 1; } }\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    target.write_text("class Pay { int amount() { return 2; } }\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "PAY-21 change")
    return repo, base, git(repo, "rev-parse", "HEAD")


class LedgerCheckpointTests(unittest.TestCase):
    def test_checkpoint_survives_scanner_failure_and_legacy_output_has_warning(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo, base, head = sample_repo(root, "one")
            args = SimpleNamespace(
                command="review-local", repo=str(repo), base=base, head=head,
                project="demo/one", catalog=[], run_root=str(root / "runs"),
                output=str(root / "legacy.md"), json_output=str(root / "legacy.json"),
                db=str(root / "state.sqlite3"), refactoringminer_bin=None,
                codetracker_evidence=None, attribution_rules=None)
            with patch("diff_analysis.cli.review_repo",
                       side_effect=ScannerError("simulated missing javac")):
                run(args)
            dirs = list((root / "runs").iterdir())
            self.assertEqual(1, len(dirs))
            bundle = dirs[0]
            checkpoint = json.loads((bundle / "ledger_checkpoint.json").read_text("utf-8"))
            release = json.loads((bundle / "analysis.json").read_text("utf-8"))
            self.assertEqual("SAVED", checkpoint["state"])
            self.assertEqual(1, len(checkpoint["ledgers"]))
            change_id = checkpoint["ledgers"][0]["final_changes"][0]["id"]
            self.assertEqual(change_id, release["ledgers"][0]["final_changes"][0]["id"])
            self.assertEqual("PARTIAL_EVIDENCE", release["status"])
            self.assertEqual("FAILED", release["project_phase_status"][0]["static_scan"])
            self.assertEqual("STATIC_SCAN", release["stage_failures"][0]["stage"])
            self.assertTrue((bundle / "evidence" / (change_id + ".md")).exists())
            self.assertIn("FAILED", (bundle / "report.md").read_text("utf-8"))
            self.assertIn("部分扫描失败", Path(args.output).read_text("utf-8"))

    def test_bad_project_does_not_erase_successful_project(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo, base, head = sample_repo(root, "one")
            missing = root / "not-a-git-repository"
            specs = [(repo, "demo/one", base, head),
                     (missing, "demo/bad", base, head)]
            bundle, checkpoint = start_ledger_checkpoint(specs, root / "runs")
            self.assertEqual("PARTIAL", checkpoint["state"])
            self.assertEqual(1, len(checkpoint["ledgers"]))
            self.assertEqual("demo/bad", checkpoint["failures"][0]["project"])
            output, release = write_release_bundle(
                specs, [], [], root / "runs",
                checkpoint={"root": bundle, "manifest": checkpoint})
            self.assertEqual(bundle, output)
            self.assertEqual("PARTIAL_EVIDENCE", release["status"])
            self.assertEqual(1, len(release["ledgers"]))
            self.assertEqual("FAILED", release["project_phase_status"][1]["git_ledger"])
            self.assertTrue((bundle / "report.md").exists())

    def test_offline_rebuild_preserves_raw_diff_without_repeating_git_or_scan(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo, base, head = sample_repo(root, "one")
            first, checkpoint = start_ledger_checkpoint(
                [(repo, "demo/one", base, head)], root / "runs")
            source = first / "ledger_checkpoint.json"
            before = source.read_bytes()
            with patch("diff_analysis.release_report.final_diff_ledger",
                       side_effect=AssertionError("must not re-run Git")):
                with patch("diff_analysis.scanner.scan_tree",
                           side_effect=AssertionError("must not run Java")):
                    rebuilt, release = rebuild_from_checkpoint(
                        source, root / "runs", root / "state.sqlite3")
            self.assertNotEqual(first, rebuilt)
            self.assertEqual(before, source.read_bytes())
            self.assertEqual("PARTIAL_EVIDENCE", release["status"])
            self.assertEqual("NOT_RERUN", release["stage_failures"][0]["error_type"])
            ids = [x["id"] for x in checkpoint["ledgers"][0]["final_changes"]]
            self.assertEqual(ids, [x["id"] for x in release["ledgers"][0]["final_changes"]])
            self.assertTrue((rebuilt / "report.md").exists())
            self.assertTrue((rebuilt / "ai_review.md").exists())

    def test_rebuild_rejects_invalid_checkpoint_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo, base, head = sample_repo(root, "one")
            first, manifest = start_ledger_checkpoint(
                [(repo, "demo/one", base, head)], root / "runs")
            manifest["ledgers"][0]["final_changes"][0]["id"] = "../escaping"
            corrupted = root / "invalid.json"
            corrupted.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ValueError):
                rebuild_from_checkpoint(corrupted, root / "runs")

    def test_review_enrichment_preserves_saved_git_diff_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            repo, base, head = sample_repo(Path(folder), "one")
            ledger = final_diff_ledger(repo, "demo/one", base, head)
            before = [c["id"] for c in ledger["final_changes"]]
            self.assertTrue(before)
            attach_review_to_ledger(ledger, {
                "old_methods": {}, "methods": {
                    "Pay#amount()": {"path": "Pay.java", "start": 1, "end": 1}}})
            self.assertEqual(before, [c["id"] for c in ledger["final_changes"]])
            self.assertEqual(["Pay#amount()"], ledger["final_changes"][0]["symbols"])
            self.assertEqual("SEMANTIC_MAPPED",
                             ledger["final_changes"][0]["status"])


if __name__ == "__main__":
    unittest.main()
