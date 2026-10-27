"""Multi-commit identity, extract-risk and exact-SHA mixed-task attribution tests."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from diff_analysis.element_history import build_identity_chains, attach_chains
from diff_analysis.extract_assessment import assess_extract_event
from diff_analysis.attribution_rules import apply_attribution_rules
from diff_analysis.release_analysis import (
    final_diff_ledger, candidate_groups, assemble_release)
from diff_analysis.release_report import write_release_bundle


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args],
                                   stderr=subprocess.STDOUT).decode().strip()


def init(tmp):
    repo = Path(tmp) / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    return repo


def put(repo, filename, source):
    path = repo / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def commit(repo, msg):
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", msg)
    return git(repo, "rev-parse", "HEAD")


def location(path, name, line=2):
    return {"path": path, "start": line, "end": line,
            "element": "int " + name + "()"}


def event(kind, sha, old, new):
    return {"kind": kind, "commit_sha": sha, "old": old, "new": new,
            "source": "REFACTORING_MINER", "confidence": "HISTORY_CANDIDATE",
            "description": ""}


class IdentityChainTests(unittest.TestCase):
    def test_two_consecutive_commits_link_rename_then_move(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = init(temp)
            put(repo, "A.java", "class A {\n int oldName(){return 1;}\n}\n")
            base = commit(repo, "base")
            put(repo, "A.java", "class A {\n int middle(){return 1;}\n}\n")
            first = commit(repo, "PAY-1 rename")
            put(repo, "A.java", "class A {\n}\n")
            put(repo, "B.java", "class B {\n int middle(){return 1;}\n}\n")
            head = commit(repo, "PAY-1 move")
            events = [
                event("Rename Method", first, location("A.java", "oldName"),
                      location("A.java", "middle")),
                event("Move Method", head, location("A.java", "middle"),
                      location("B.java", "middle"))
            ]
            chains, warnings = build_identity_chains(repo, base, head, events)
            self.assertEqual([], warnings)
            self.assertEqual(1, len(chains))
            self.assertEqual(2, chains[0]["step_count"])
            self.assertEqual("CONTIGUOUS_CANDIDATE", chains[0]["status"])
            ledger = final_diff_ledger(repo, "rhf/pay", base, head)
            attach_chains(ledger, chains)
            self.assertEqual("NET_DIFF_BOTH_SIDES",
                             ledger["element_chains"][0]["mapping_status"])

    def test_intermediate_modification_to_same_file_breaks_chain(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = init(temp)
            put(repo, "A.java", "class A {\n int oldName(){return 1;}\n}\n")
            base = commit(repo, "base")
            put(repo, "A.java", "class A {\n int middle(){return 1;}\n}\n")
            first = commit(repo, "PAY-1 rename")
            put(repo, "A.java", "class A {\n int middle(){return 2;}\n}\n")
            commit(repo, "PAY-2 change same file")
            put(repo, "A.java", "class A {\n}\n")
            put(repo, "B.java", "class B {\n int middle(){return 2;}\n}\n")
            head = commit(repo, "PAY-1 move")
            events = [
                event("Rename Method", first, location("A.java", "oldName"),
                      location("A.java", "middle")),
                event("Move Method", head, location("A.java", "middle"),
                      location("B.java", "middle"))
            ]
            chains, _ = build_identity_chains(repo, base, head, events)
            self.assertEqual(2, len(chains))
            self.assertTrue(any(c["status"] == "BOUNDARY_UNVERIFIED"
                                for c in chains))

    def test_ambiguous_successors_are_not_silently_joined(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = init(temp)
            put(repo, "A.java", "class A {\n int oldName(){return 1;}\n}\n")
            base = commit(repo, "base")
            put(repo, "A.java", "class A {\n int middle(){return 1;}\n}\n")
            first = commit(repo, "PAY-1 rename")
            put(repo, "B.java", "class B {\n int middle(){return 1;}\n}\n")
            head = commit(repo, "PAY-1 copy/move")
            steps = [
                event("Rename Method", first, location("A.java", "oldName"),
                      location("A.java", "middle")),
                event("Move Method", head, location("A.java", "middle"),
                      location("B.java", "middle")),
                event("Move And Rename Method", head, location("A.java", "middle"),
                      location("B.java", "another"))
            ]
            chains, warnings = build_identity_chains(repo, base, head, steps)
            self.assertTrue(any(w["type"] == "ELEMENT_CHAIN_AMBIGUOUS"
                                for w in warnings))
            self.assertEqual(3, len(chains))


class ExtractReviewTests(unittest.TestCase):
    def _make(self, temp, helper):
        repo = init(temp)
        put(repo, "A.java", "class A {\n int original(){ return 2 + 3; }\n}\n")
        commit(repo, "base")
        put(repo, "A.java", "class A {\n int original(){return helper();}\n"
            " int helper(){return " + helper + ";}\n}\n")
        head = commit(repo, "PAY-1 extracted")
        return repo, event(
            "Extract Method", head, location("A.java", "original"),
            location("A.java", "helper", 3))

    def test_same_body_is_only_textual_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            repo, ev = self._make(temp, "2 + 3")
            result = assess_extract_event(repo, ev)
            self.assertEqual("TEXTUAL_EXTRACTION_CANDIDATE", result["status"])
            self.assertIn("not prove behavior equivalence", result["limitation"].lower())
            self.assertIn("purity", result["limitation"])
            self.assertIn("call-site correctness", result["limitation"])

    def test_caller_condition_change_detected_even_if_extracted_body_is_identical(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = init(temp)
            put(repo, "A.java",
                "class A {\n int original(int a){if(a>0){return 2+3;}return 0;}\n}\n")
            commit(repo, "base")
            put(repo, "A.java",
                "class A {\n int original(int a){if(a>=0){return helper();}return 0;}\n"
                " int helper(){return 2+3;}\n}\n")
            head = commit(repo, "PAY-1 extract and modify condition")
            ev = event("Extract Method", head,
                       {"path": "A.java", "start": 2, "end": 2,
                        "element": "int original(int)"},
                       location("A.java", "helper", 3))
            result = assess_extract_event(repo, ev)
            self.assertTrue(result["extracted_body_found_verbatim"])
            self.assertEqual("POSSIBLE_MIXED_BEHAVIOR", result["status"])
            self.assertTrue(result["caller_control_flow_diff"])

    def test_modified_operator_flags_mixed_behavior(self):
        with tempfile.TemporaryDirectory() as temp:
            repo, ev = self._make(temp, "2 - 3")
            result = assess_extract_event(repo, ev)
            self.assertEqual("POSSIBLE_MIXED_BEHAVIOR", result["status"])
            self.assertIn("-", result["new_risk_tokens"])


class MixedCommitTests(unittest.TestCase):
    def setUp(self):
        mock = patch('diff_analysis.maven_inventory.default_projects', return_value=([], []))
        mock.start()
        self.addCleanup(mock.stop)

    def _example(self, temp):
        repo = init(temp)
        put(repo, "A.java",
            "class A {\n int first(){return 1;}\n"
            " int untouched(){return 5;}\n int second(){return 1;}\n}\n")
        base = commit(repo, "base")
        put(repo, "A.java",
            "class A {\n int first(){return 2;}\n"
            " int untouched(){return 5;}\n int second(){return 3;}\n}\n")
        head = commit(repo, "PAY-123 also PAY-456 mixed commit")
        return repo, base, head

    def test_explicit_hunk_attribution_separates_one_mixed_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            repo, base, head = self._example(temp)
            ledger = final_diff_ledger(repo, "rhf/pay", base, head)
            self.assertEqual(2, len(ledger["final_changes"]))
            original, _ = candidate_groups([ledger])
            self.assertEqual(1, len(original))
            self.assertEqual('PAY-123', original[0]['issue_key'])
            decisions = [
                {"diff_id": ledger["final_changes"][0]["id"],
                 "issue_keys": ["PAY-123"], "reason": "Approved payment ticket"},
                {"diff_id": ledger["final_changes"][1]["id"],
                 "issue_keys": ["PAY-456"], "reason": "Approved refund ticket"}
            ]
            file = Path(temp) / "attribution.json"
            file.write_text(json.dumps({
                "schema_version": "attribution-rules-v1",
                "project": "rhf/pay", "base_sha": base, "head_sha": head,
                "decisions": decisions}), encoding="utf-8")
            self.assertEqual([], apply_attribution_rules(ledger, file))
            groups, memberships = candidate_groups([ledger])
            self.assertEqual({"PAY-123", "PAY-456"},
                             {g["issue_key"] for g in groups})
            self.assertTrue(all(g["group_basis"] == "HUMAN_EXACT_SHA_DIFF"
                                for g in groups))
            self.assertEqual(2, len(memberships))

    def test_invalid_sha_fails_closed_without_mutating_diff(self):
        with tempfile.TemporaryDirectory() as temp:
            repo, base, head = self._example(temp)
            ledger = final_diff_ledger(repo, "rhf/pay", base, head)
            file = Path(temp) / "bad.json"
            file.write_text(json.dumps({
                "schema_version": "attribution-rules-v1",
                "project": "rhf/pay", "base_sha": base, "head_sha": "0" * 40,
                "decisions": []}), encoding="utf-8")
            warnings = apply_attribution_rules(ledger, file)
            self.assertEqual("ATTRIBUTION_RULES_INVALID", warnings[0]["type"])
            self.assertTrue(all("manual_attribution" not in x
                                for x in ledger["final_changes"]))

    def test_two_tasks_within_same_hunk_remains_review_required(self):
        with tempfile.TemporaryDirectory() as temp:
            repo, base, head = self._example(temp)
            ledger = final_diff_ledger(repo, "rhf/pay", base, head)
            item = ledger["final_changes"][0]
            file = Path(temp) / "mixed.json"
            file.write_text(json.dumps({
                "schema_version": "attribution-rules-v1",
                "project": "rhf/pay", "base_sha": base, "head_sha": head,
                "decisions": [{
                    "diff_id": item["id"], "issue_keys": ["PAY-123", "PAY-456"],
                    "reason": "Both changes are inside the same Hunk",
                    "segments": []}]}), encoding="utf-8")
            warnings = apply_attribution_rules(ledger, file)
            self.assertEqual("MIXED_HUNK_WITHOUT_SEGMENTS", warnings[0]["type"])
            groups, _ = candidate_groups([ledger])
            target = [g for g in groups if item["id"] in g["change_ids"]][0]
            self.assertIsNone(target["issue_key"])
            self.assertEqual("MIXED_HUNK_REVIEW", target["group_basis"])

    def test_integrated_offline_report_retains_chain_and_risk_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = init(temp)
            put(repo, "A.java", "class A {\n int original(){return 2+3;}\n}\n")
            base = commit(repo, "base")
            put(repo, "A.java",
                "class A {\n int original(){return helper();}\n"
                " int helper(){return 2-3;}\n}\n")
            head = commit(repo, "PAY-100 mixed extraction")
            event_data = [event("Extract Method", head,
                                location("A.java", "original"),
                                location("A.java", "helper", 3))]
            with patch("diff_analysis.release_analysis.run_refactoringminer",
                       return_value=(event_data, [])):
                folder, release = write_release_bundle(
                    [(repo, "rhf/pay", base, head)], [], [],
                    Path(temp) / "runs", refactoringminer_bin="fake")
            self.assertEqual(1, release["ledgers"][0]["enrichment"]["mixed_behavior_candidates"])
            text = (folder / "ai_review.md").read_text(encoding="utf-8")
            self.assertIn("POSSIBLE_MIXED_BEHAVIOR", text)
            self.assertEqual("STATIC_CANDIDATES_ONLY", release["status"])


if __name__ == "__main__":
    unittest.main()
