"""P1-A durable Git ledger and per-project scan failure isolation."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from diff_analysis.cli import run, parser
from diff_analysis.release_report import (start_ledger_checkpoint, write_release_bundle,
                                          rebuild_from_checkpoint, resolve_run_root)
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
    def setUp(self):
        mock = patch('diff_analysis.maven_inventory.default_projects', return_value=([], []))
        mock.start()
        self.addCleanup(mock.stop)
        run_root = patch('diff_analysis.cli.resolve_run_root', side_effect=lambda path: Path(path).resolve())
        run_root.start()
        self.addCleanup(run_root.stop)

    def test_checkpoint_survives_scanner_failure_and_report_has_warning(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo, base, head = sample_repo(root, "one")
            args = SimpleNamespace(
                command="review-local", repo=str(repo), base=base, head=head,
                project="demo/one", catalog=[], run_root=str(root / "runs"),
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
            # Derived graph stage runs after the initial JSON is created;
            # its final status must be persisted back to that same artifact.
            self.assertIn("unified_status", release)
            self.assertEqual(release["status"],
                             "PARTIAL_EVIDENCE")
            self.assertEqual("SAVED", checkpoint["state"])
            self.assertEqual(1, len(checkpoint["ledgers"]))
            change_id = checkpoint["ledgers"][0]["final_changes"][0]["id"]
            self.assertEqual(change_id, release["ledgers"][0]["final_changes"][0]["id"])
            self.assertEqual("PARTIAL_EVIDENCE", release["status"])
            self.assertEqual("FAILED", release["project_phase_status"][0]["static_scan"])
            self.assertEqual("STATIC_SCAN", release["stage_failures"][0]["stage"])
            self.assertTrue((bundle / "evidence" / (change_id + ".md")).exists())
            stages = json.loads((bundle / "stage_manifest.json").read_text("utf-8"))
            self.assertEqual("PARTIAL", stages["state"])
            self.assertEqual("COMPLETE", stages["report_stage"]["status"])
            self.assertIn("FAILED", (bundle / "report.md").read_text("utf-8"))
            self.assertIn('阶段覆盖', (bundle / 'report.md').read_text('utf-8'))
            self.assertFalse((root / 'report.md').exists())

    def test_report_uses_a_new_run_directory_each_time(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo, base, head = sample_repo(root, 'one')
            args = SimpleNamespace(command='review-local', repo=str(repo), base=base, head=head,
                                   project='demo/one', catalog=[], run_root=str(root / 'runs'),
                                   db=str(root / 'state.sqlite3'),
                                   refactoringminer_bin=None, codetracker_evidence=None, attribution_rules=None)
            with patch('diff_analysis.cli.review_repo', side_effect=ScannerError('simulated')):
                run(args)
                run(args)
            runs = list((root / 'runs').iterdir())
            self.assertEqual(2, len(runs))
            for bundle in runs:
                self.assertTrue((bundle / 'report.md').is_file())
                self.assertTrue((bundle / 'analysis.json').is_file())
                self.assertFalse((bundle / 'legacy_report.md').exists())
                self.assertFalse((bundle / 'review.json').exists())
            self.assertFalse((root / 'report.md').exists())
            self.assertFalse((root / 'review.json').exists())

    def test_legacy_output_options_removed_and_run_root_is_restricted(self):
        for flag in ('--output', '--json-output'):
            with self.subTest(flag=flag), self.assertRaises(SystemExit), patch('sys.stderr'):
                parser().parse_args(['wizard', flag, 'outside.md'])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'runs'
            with patch('diff_analysis.release_report.DEFAULT_RUN_ROOT', root):
                self.assertEqual(root.resolve(), resolve_run_root())
                self.assertEqual((root / 'batch').resolve(), resolve_run_root(root / 'batch'))
                with self.assertRaises(ValueError):
                    resolve_run_root(Path(folder))
                with self.assertRaises(ValueError):
                    resolve_run_root(root / '..' / 'elsewhere')

    def test_one_gitlab_mr_prepare_failure_keeps_other_git_ledger(self):
        from diff_analysis.gitlab import GitLabError
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo, base, head = sample_repo(root, "successful")
            good = "https://gitlab.example.com/team/good/-/merge_requests/1"
            bad = "https://gitlab.example.com/team/bad/-/merge_requests/2"
            args = SimpleNamespace(
                command="review-mrs", mr=[bad, good],
                allowed_host="gitlab.example.com", allow_http=False,
                token_env="GITLAB_TOKEN", evidence=[], catalog=[],
                run_root=str(root / "runs"),
                db=str(root / "state.sqlite3"), refactoringminer_bin=None,
                codetracker_evidence=None, attribution_rules=None)
            good_mr = {"project": "team/good", "base": base, "head": head,
                       "url": good}
            with patch.dict("os.environ", {"GITLAB_TOKEN": "local-test-only"}):
                with patch("diff_analysis.cli.get_mr",
                           side_effect=[GitLabError("invalid MR"), good_mr]):
                    with patch("diff_analysis.cli.clone_mr", return_value=repo):
                        with patch("diff_analysis.cli.review_repo",
                                   side_effect=ScannerError("no JDK")):
                            run(args)
            dirs = list((root / "runs").iterdir())
            self.assertEqual(1, len(dirs))
            release = json.loads((dirs[0] / "analysis.json").read_text("utf-8"))
            self.assertEqual(1, len(release["ledgers"]))
            self.assertEqual("PARTIAL_EVIDENCE", release["status"])
            phases = [r["stage"] for r in release["stage_failures"]]
            self.assertIn("PREPARE", phases)
            self.assertIn("STATIC_SCAN", phases)
            self.assertTrue((dirs[0] / "report.md").is_file())
            stages = json.loads((dirs[0] / "stage_manifest.json").read_text("utf-8"))
            self.assertEqual("COMPLETE", stages["report_stage"]["status"])


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
