"""Pinned changed identifier -> cross-project consumer branch evidence."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from diff_analysis.contract_context import discover_contract_context


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args],
                                   stderr=subprocess.STDOUT).decode().strip()


def repo_at(root, name, content):
    repo = root / name
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Example")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "Service.java").write_text(content, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "pinned")
    return repo, git(repo, "rev-parse", "HEAD")


class ContractContextTests(unittest.TestCase):
    def test_changed_field_traces_to_unchanged_remote_decision_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a, a_sha = repo_at(root, "producer",
                "class Service {\n"
                " void pay() {\n"
                "   String transactionState = \"PAID\";\n"
                " }\n}\n")
            b, b_sha = repo_at(root, "consumer",
                "class Service {\n"
                " void consume(String transactionState) {\n"
                "   if (transactionState.equals(\"PAID\")) {\n"
                "     process();\n"
                "   }\n"
                " }\n}\n")
            symbol_a, symbol_b = "p.Pay#pay()", "p.Receive#consume(java.lang.String)"
            group_id = "G-1212121212121212"
            data = {
                "schema_version": "release-evidence-v1",
                "groups": [{"id": group_id, "change_ids": ["D-abcdefabcdefabcd"]}],
                "ledgers": [
                    {"project": "a", "base": a_sha, "head": a_sha,
                     "final_changes": [{
                         "id": "D-abcdefabcdefabcd", "project": "a",
                         "path": "Service.java", "old_path": "Service.java",
                         "new_range": {"start": 3, "count": 1},
                         "old_range": {"start": 3, "count": 1},
                         "after": [{"line": 3, "text": 'String transactionState = "PAID";'}],
                         "before": [{"line": 3, "text": 'String transactionState = "NEW";'}],
                         "symbols_old": [symbol_a], "symbols_new": [symbol_a]
                     }]},
                    {"project": "b", "base": b_sha, "head": b_sha,
                     "final_changes": []}
                ],
                "relations": {"graphs": [
                    {"project": "a", "side": "HEAD", "snapshot_sha": a_sha,
                     "method_index": {symbol_a: {"path": "Service.java",
                                                  "start": 2, "end": 4}}},
                    {"project": "b", "side": "HEAD", "snapshot_sha": b_sha,
                     "method_index": {symbol_b: {"path": "Service.java",
                                                  "start": 2, "end": 6}}}
                ]}
            }
            (root / "analysis.json").write_text(json.dumps(data), encoding="utf-8")
            (root / "unified_impact_latest.json").write_text(json.dumps({
                "findings": [{
                    "group_id": group_id,
                    "consumer_node": "method|HEAD|b|" + b_sha + "|" + symbol_b
                }]
            }), encoding="utf-8")
            result = discover_contract_context(root, {"a": a, "b": b})
            self.assertGreaterEqual(result["facts"], 2)
            saved = json.loads((root / "contract_context_latest.json").read_text("utf-8"))
            branches = [v for v in saved["facts"] if
                        v["project"] == "b" and v["kind"] == "DECISION_BRANCH"]
            self.assertEqual(1, len(branches))
            self.assertIn("transactionState", branches[0]["identifiers"])
            self.assertEqual(3, branches[0]["line"])
            self.assertEqual("LEXICAL_CANDIDATE", branches[0]["status"])
            self.assertNotIn("business_correct", branches[0])


    def test_dto_fields_and_mybatis_mapping_without_java_method_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "contract"
            repo.mkdir()
            git(repo, "init", "-q")
            git(repo, "config", "user.name", "Example")
            git(repo, "config", "user.email", "test@example.com")
            files = {
                "PayResponse.java": (
                    "class PayResponse {\\n"
                    " private String transactionState;\\n"
                    "}\\n"),
                "PaymentMapper.xml": (
                    "<mapper>\\n"
                    " <result column=\\"trans_state\\" property=\\"transactionState\\"/>\\n"
                    "</mapper>\\n"),
                "payment.sql": "SELECT transactionState FROM trans_record;\\n"
            }
            for path, content in files.items():
                (repo / path).write_text(content.replace("\\\\n", "\\n"),
                                         encoding="utf-8")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "contract")
            sha = git(repo, "rev-parse", "HEAD")
            group = "G-1212121212121212"
            changes = []
            for index, path in enumerate(files):
                line = (2 if path != "payment.sql" else 1)
                changes.append({
                    "id": "D-%016d" % (index + 1), "project": "contracts",
                    "path": path, "old_path": path,
                    "old_range": {"start": line, "count": 1},
                    "new_range": {"start": line, "count": 1},
                    "after": [{"line": line, "text": "transactionState"}],
                    "before": [{"line": line, "text": "transactionState"}],
                    "symbols_new": [], "symbols_old": []
                })
            data = {"schema_version": "release-evidence-v1",
                    "groups": [{"id": group,
                                "change_ids": [c["id"] for c in changes]}],
                    "ledgers": [{"project": "contracts", "base": sha,
                                 "head": sha, "final_changes": changes}],
                    "relations": {"graphs": []}}
            (root / "analysis.json").write_text(json.dumps(data), encoding="utf-8")
            summary = discover_contract_context(root, {"contracts": repo})
            self.assertGreaterEqual(summary["facts"], 4)
            facts = json.loads((root / "contract_context_latest.json").read_text("utf-8"))["facts"]
            types = {f["kind"] for f in facts}
            self.assertIn("CONTRACT_FIELD_DECLARATION", types)
            self.assertIn("MYBATIS_MAPPING_CANDIDATE", types)
            self.assertIn("SQL_FIELD_REFERENCE", types)


if __name__ == "__main__":
    unittest.main()
