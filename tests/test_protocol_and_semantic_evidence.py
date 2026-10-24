"""Pinned source backed integration and semantic evidence checks."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from diff_analysis.connection_bindings import correlate
from diff_analysis.semantic_evidence import collect_semantic_evidence
from diff_analysis.review_workflow import export_questions


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args],
                                   stderr=subprocess.STDOUT).decode("utf-8").strip()


def make_repo(root, name, text):
    repo = root / name
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "Service.java").write_text(text, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", name)
    return repo, git(repo, "rev-parse", "HEAD")


def release(proj, sha, path, bounds):
    diff = {"id": "D-1111111111111111", "project": proj,
            "path": path, "old_path": path, "base_sha": sha, "head_sha": sha,
            "new_range": {"start": 2, "count": 1},
            "old_range": {"start": 2, "count": 1}}
    return {
        "schema_version": "release-evidence-v1",
        "ledgers": [{"project": proj, "base": sha, "head": sha,
                     "final_changes": [diff]}],
        "groups": [{"id": "G-1111111111111111",
                    "change_ids": [diff["id"]], "issue_key": None}],
        "cross_group_checks": [], "consumer_candidates": [],
        "relations": {"graphs": [{
            "project": proj, "side": "HEAD", "snapshot_sha": sha,
            "method_index": {
                "p.Service#pay()": {"path": path, "start": bounds[0],
                                   "end": bounds[1], "name": "pay",
                                   "class": "p.Service"}},
            "edges": []}]}
    }


def observation(project, sha, kind, role, identity, line, term,
                attested=True, side="HEAD"):
    return {
        "project": project, "side": side, "sha": sha, "kind": kind,
        "role": role, "identity": identity,
        "evidence": {"path": "Service.java", "start": line, "end": line,
                     "contains": [term]},
        "deployment_binding": ({
            "author": "integrator", "environment": identity.get("environment", "uat"),
            "service_id": identity["service_id"],
            "reason": "Verified environment config with deployment owner"
        } if attested else None)
    }


class ProtocolBindingsTests(unittest.TestCase):
    def test_four_protocols_never_infer_from_name_alone(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            text = ("class Service {\n"
                    " String dubbo = \"PayService\";\n"
                    " String http = \"/pay\";\n"
                    " String mq = \"payment-queue\";\n"
                    " String sql = \"trans_record\";\n"
                    "}\n")
            a, sa = make_repo(root, "a", text)
            b, sb = make_repo(root, "b", text)
            analysis = release("team/a", sa, "Service.java", (1, 6))
            analysis["ledgers"].append({
                "project": "team/b", "base": sb, "head": sb,
                "final_changes": []})
            (root / "analysis.json").write_text(json.dumps(analysis), encoding="utf-8")
            ids = {
                "DUBBO": ({"interface": "p.PayService", "group": "core",
                           "version": "1.0", "registry": "zk-prod",
                           "service_id": "payment-svc"}, 2, "PayService",
                          "CONSUMER", "PROVIDER"),
                "HTTP": ({"method": "POST", "path": "/pay",
                          "target_service": "payment-svc",
                          "environment": "uat", "service_id": "payment-svc"},
                         3, "/pay", "CLIENT", "SERVER"),
                "MQ": ({"broker": "broker-1", "destination": "payment-queue",
                        "route": "payment-key", "environment": "uat",
                        "service_id": "payment-svc"},
                       4, "payment-queue", "CONSUMER", "PRODUCER"),
                "DATABASE": ({"datasource": "db-main",
                              "schema": "pay", "table": "trans_record",
                              "environment": "uat", "service_id": "payment-svc"},
                             5, "trans_record", "READER", "WRITER")
            }
            inputs = []
            for kind, (ident, line, term, caller, callee) in ids.items():
                inputs.append(observation("team/a", sa, kind, caller, ident, line, term))
                inputs.append(observation("team/b", sb, kind, callee, ident, line, term))
            res = correlate(root, {"schema_version": "connection-bindings-v1",
                                   "observations": inputs},
                            {"team/a": a, "team/b": b})
            self.assertEqual(4, res["edges"])
            saved = json.loads((root / "bindings_latest.json").read_text("utf-8"))
            self.assertTrue(all(x["binding_status"] == "USER_ATTESTED_MATCH"
                                for x in saved["edges"]))
            # Assertions do not magically prove actual deployment.
            self.assertIn("not", saved["warning"].lower())

    def test_missing_attestation_stays_unconfirmed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            a, sa = make_repo(root, "a", "PayService\n")
            b, sb = make_repo(root, "b", "PayService\n")
            data = release("team/a", sa, "Service.java", (1, 1))
            data["ledgers"].append({"project": "team/b", "base": sb,
                                     "head": sb, "final_changes": []})
            (root / "analysis.json").write_text(json.dumps(data), encoding="utf-8")
            identity = {"interface": "PayService", "group": "g",
                        "version": "1", "registry": "zk", "service_id": "pay"}
            a_obs = observation("team/a", sa, "DUBBO", "CONSUMER",
                                identity, 1, "PayService", attested=True)
            b_obs = observation("team/b", sb, "DUBBO", "PROVIDER",
                                identity, 1, "PayService", attested=False)
            correlate(root, {"schema_version": "connection-bindings-v1",
                             "observations": [a_obs, b_obs]},
                      {"team/a": a, "team/b": b})
            data = json.loads((root / "bindings_latest.json").read_text("utf-8"))
            self.assertEqual("BINDING_UNCONFIRMED", data["edges"][0]["binding_status"])

    def test_sha_path_or_literal_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            a, sa = make_repo(root, "a", "PayService\n")
            data = release("team/a", sa, "Service.java", (1, 1))
            (root / "analysis.json").write_text(json.dumps(data), encoding="utf-8")
            obs = observation("team/a", sa, "DUBBO", "PROVIDER",
                              {"interface": "PayService", "group": "g",
                               "version": "1", "registry": "zk", "service_id": "pay"},
                              1, "NotInSource")
            with self.assertRaises(ValueError):
                correlate(root, {"schema_version": "connection-bindings-v1",
                                 "observations": [obs]}, {"team/a": a})
            self.assertFalse((root / "bindings_latest.json").exists())
            obs["evidence"]["contains"] = ["PayService"]
            obs["sha"] = "f" * 40
            with self.assertRaises(ValueError):
                correlate(root, {"schema_version": "connection-bindings-v1",
                                 "observations": [obs]}, {"team/a": a})


class SemanticEvidenceTests(unittest.TestCase):
    def test_complete_method_contract_and_reuse(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo, sha = make_repo(root, "src",
                                  "class Service {\n void pay() {\n  go();\n }\n}\n")
            analysis = release("team/src", sha, "Service.java", (2, 4))
            (root / "analysis.json").write_text(json.dumps(analysis), encoding="utf-8")
            qid = export_questions(root, analysis)["questions"][0]["id"]
            common = {"project": "team/src", "side": "HEAD", "sha": sha,
                      "question_id": qid}
            selectors = [{**common, "kind": "METHOD", "symbol": "p.Service#pay()"},
                         {**common, "kind": "CONTRACT", "path": "Service.java"}]
            first = collect_semantic_evidence(root,
                        {"schema_version": "semantic-evidence-v1",
                         "selectors": selectors}, {"team/src": repo})
            self.assertEqual(2, first["new_evidence"])
            saved = json.loads((root / "supplement_state.json").read_text("utf-8"))
            self.assertEqual(["METHOD", "CONTRACT"],
                             [x["kind"] for x in saved["evidence"]])
            method_id = saved["evidence"][0]["id"]
            code = (root / "supplements" / (method_id + ".txt")).read_text("utf-8")
            self.assertIn("2 |  void pay()", code)
            self.assertNotIn("1 | class Service", code)
            second = collect_semantic_evidence(root,
                         {"schema_version": "semantic-evidence-v1",
                          "selectors": selectors}, {"team/src": repo})
            self.assertEqual(0, second["new_evidence"])
            self.assertEqual("NO_NEW_RELEVANT_EVIDENCE", second["stop_reason"])

    def test_no_guessing_when_method_not_indexed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo, sha = make_repo(root, "src", "class Service {}\n")
            analysis = release("team/src", sha, "Service.java", (1, 1))
            (root / "analysis.json").write_text(json.dumps(analysis), encoding="utf-8")
            qid = export_questions(root, analysis)["questions"][0]["id"]
            result = collect_semantic_evidence(root, {
                "schema_version": "semantic-evidence-v1",
                "selectors": [{
                    "question_id": qid, "project": "team/src", "side": "HEAD",
                    "sha": sha, "kind": "METHOD", "symbol": "missing()"
                }]}, {"team/src": repo})
            self.assertEqual("NOT_FOUND", result["results"][0]["status"])


if __name__ == "__main__":
    unittest.main()
