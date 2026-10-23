"""Refactoring evolution integration, safe downgrade, and more precise issue grouping."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from diff_analysis.refactoring_enrichment import (
    parse_refactoringminer, attach_evolution, load_codetracker_bridge,
    run_refactoringminer)
from diff_analysis.release_analysis import final_diff_ledger, candidate_groups, assemble_release


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args],
                                   stderr=subprocess.STDOUT).decode("utf-8").strip()


def setup(path):
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.com")
    return path


def commit(repo, filename, contents, message):
    path = repo / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def loc(path, start, end, name):
    return {"filePath": path, "startLine": start, "endLine": end,
            "codeElement": name, "codeElementType": "METHOD_DECLARATION"}


class RefactoringTests(unittest.TestCase):
    def test_rename_method_direct_attaches_to_final_hunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = setup(Path(tmp) / "repo")
            base = commit(repo, "Pay.java",
                          "class Pay {\n int oldName(){ return 1; }\n}\n", "base")
            head = commit(repo, "Pay.java",
                          "class Pay {\n int newName(){ return 1; }\n}\n", "PAY-101 rename")
            payload = {"commits": [{"sha1": head, "refactorings": [{
                "type": "Rename Method", "description": "Rename oldName to newName",
                "leftSideLocations": [loc("Pay.java", 2, 2, "oldName")],
                "rightSideLocations": [loc("Pay.java", 2, 2, "newName")]
            }]}]}
            events, warnings = parse_refactoringminer(payload, base, head)
            self.assertEqual([], warnings)
            self.assertEqual("DIRECT_COMMIT_CANDIDATE", events[0]["confidence"])
            ledger = final_diff_ledger(repo, "rhf/pay", base, head)
            linked = attach_evolution(ledger, events)
            self.assertEqual(1, len(linked))
            self.assertEqual("BOTH_SIDES_MATCHED", linked[0]["mapping_status"])
            self.assertEqual("Rename Method", ledger["final_changes"][0]["evolution_types"][0])

    def test_move_method_across_classes_maps_both_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = setup(Path(tmp) / "repo")
            base = commit(repo, "Old.java",
                          "class Old {\n int calc(){ return 2; }\n}\n", "base")
            head = commit(repo, "New.java",
                          "class New {\n int calc(){ return 2; }\n}\n", "PAY-102 move")
            (repo / "Old.java").unlink()
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "PAY-102 remove Old")
            head = git(repo, "rev-parse", "HEAD")
            ledger = final_diff_ledger(repo, "rhf/pay", base, head)
            events = [{"kind": "Move Method",
                       "old": {"path": "Old.java", "start": 2, "end": 2},
                       "new": {"path": "New.java", "start": 2, "end": 2},
                       "source": "CODETRACKER_BRIDGE",
                       "confidence": "EXTERNAL_TRACKED_CANDIDATE",
                       "description": "", "commit_sha": None}]
            linked = attach_evolution(ledger, events)
            self.assertEqual(1, len(linked))
            self.assertTrue(linked[0]["old_change_ids"])
            self.assertTrue(linked[0]["new_change_ids"])

    def test_extract_method_creates_evolution_evidence_not_forced_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = setup(Path(tmp) / "repo")
            base = commit(repo, "Service.java",
                          "class Service {\n int process(){ return 2 + 3; }\n}\n", "base")
            head = commit(repo, "Service.java",
                          "class Service {\n int process(){ return calculate(); }\n"
                          " int calculate(){ return 2 + 3; }\n}\n", "PAY-111 extract")
            payload = {"commits": [{"sha1": head, "refactorings": [{
                "type": "Extract Method", "description": "Extract calculate",
                "leftSideLocations": [loc("Service.java", 2, 2, "process")],
                "rightSideLocations": [loc("Service.java", 3, 3, "calculate")]
            }]}]}
            events, _ = parse_refactoringminer(payload, base, head)
            ledger = final_diff_ledger(repo, "rhf/pay", base, head)
            relations = attach_evolution(ledger, events)
            self.assertTrue(relations)
            self.assertEqual("Extract Method", relations[0]["kind"])
            before = {change["id"] for change in ledger["final_changes"]}
            groups, by_id = candidate_groups([ledger])
            self.assertEqual(before, set(by_id))
            self.assertTrue(all(g["status"] == "CANDIDATE" for g in groups))

    def test_multicommit_locations_are_not_claimed_direct(self):
        old = "a" * 40
        head = "b" * 40
        payload = {"commits": [
            {"sha1": "c" * 40, "refactorings": [{
                "type": "Move Method",
                "leftSideLocations": [loc("A.java", 1, 2, "x")],
                "rightSideLocations": [loc("B.java", 10, 11, "x")]}]},
            {"sha1": head, "refactorings": []}
        ]}
        events, _ = parse_refactoringminer(payload, old, head)
        self.assertEqual("HISTORY_CANDIDATE", events[0]["confidence"])

    def test_codetracker_bridge_requires_matching_pinned_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "rhf_pay.json"
            content = {
                "schema_version": "codetracker-bridge-v1",
                "project": "rhf/pay", "base_sha": "a" * 40, "head_sha": "b" * 40,
                "mappings": [{
                    "kind": "Move Method",
                    "old": {"path": "A.java", "start": 1, "end": 3},
                    "new": {"path": "B.java", "start": 5, "end": 8}
                }]}
            file.write_text(json.dumps(content), encoding="utf-8")
            relations, warnings = load_codetracker_bridge(
                tmp, "rhf/pay", "a" * 40, "b" * 40)
            self.assertEqual([], warnings)
            self.assertEqual("CODETRACKER_BRIDGE", relations[0]["source"])
            mismatch, warnings = load_codetracker_bridge(
                tmp, "rhf/pay", "a" * 40, "c" * 40)
            self.assertEqual([], mismatch)
            self.assertEqual("CODETRACKER_BRIDGE_INVALID", warnings[0]["type"])

    def test_absent_refactoringminer_does_not_drop_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = setup(Path(tmp) / "repo")
            base = commit(repo, "A.java", "class A {}\n", "base")
            head = commit(repo, "A.java", "class A { int v; }\n", "PAY-333")
            result = assemble_release(
                [(repo, "rhf/pay", base, head)],
                refactoringminer_bin=str(Path(tmp) / "does-not-exist"))
            self.assertTrue(result["ledgers"][0]["final_changes"])
            self.assertEqual("PARTIAL", result["ledgers"][0]["enrichment"]["refactoringminer_status"])
            self.assertTrue(result["ledgers"][0]["enrichment"]["warnings"])

    def test_blame_separates_two_task_ids_in_one_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = setup(Path(tmp) / "repo")
            base = commit(repo, "Service.java",
                          "class S {\n"
                          " int first(){return 1;}\n"
                          " int separator(){return 5;}\n"
                          " int second(){return 1;}\n"
                          "}\n", "base")
            commit(repo, "Service.java",
                   "class S {\n"
                   " int first(){return 2;}\n"
                   " int separator(){return 5;}\n"
                   " int second(){return 1;}\n"
                   "}\n", "PAY-101 fix first")
            head = commit(repo, "Service.java",
                          "class S {\n"
                          " int first(){return 2;}\n"
                          " int separator(){return 5;}\n"
                          " int second(){return 3;}\n"
                          "}\n", "PAY-202 fix second")
            ledger = final_diff_ledger(repo, "rhf/pay", base, head)
            self.assertGreaterEqual(len(ledger["final_changes"]), 2)
            groups, by_change = candidate_groups([ledger])
            self.assertEqual({"PAY-101", "PAY-202"},
                             {g["issue_key"] for g in groups})
            self.assertTrue(all(g["group_basis"] == "LINE_BLAME_ISSUE_CANDIDATE"
                                for g in groups))
            self.assertEqual(len(ledger["final_changes"]), len(by_change))


if __name__ == "__main__":
    unittest.main()
