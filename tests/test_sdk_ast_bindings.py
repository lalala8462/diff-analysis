"""AST source symbol ownership: no same-name or same-arity false attribution."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from diff_analysis.scanner import java_toolchain, ScannerError
from diff_analysis.sdk_ast_bindings import attributed_uses, _method_arity


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], stderr=subprocess.STDOUT).decode().strip()


def init(repo):
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    return repo


def commit(repo, path, content):
    dest = repo / path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "code")
    return git(repo, "rev-parse", "HEAD")


class AstContractTests(unittest.TestCase):
    def test_generic_parameter_count(self):
        self.assertEqual(2, _method_arity("java.util.Map<String,Integer>,int"))
        self.assertEqual(0, _method_arity(""))

    def test_ast_proves_owner_not_same_name_member(self):
        try:
            java_toolchain()
        except (ScannerError, OSError):
            self.skipTest("Full JDK unavailable; AST attribution gap expected")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sdk_repo, user_repo = init(root / "sdk-repo"), init(root / "consumer-repo")
            path = "src/main/java/a/Dto.java"
            old = commit(sdk_repo, path,
                         'package a;\npublic class Dto {\n'
                         '  public String status() { return ""; }\n}\n')
            new = commit(sdk_repo, path,
                         'package a;\npublic class Dto {\n'
                         '  public int status() { return 1; }\n}\n')
            consumer_path = "src/main/java/c/Consumer.java"
            consumer_sha = commit(
                user_repo, consumer_path,
                'package c;\nimport a.Dto;\n'
                'class Local { public String status() { return ""; } }\n'
                'public class Consumer {\n'
                '  Dto dto;\n  Local local;\n'
                '  void run() { Object a = dto.status(); Object b = local.status(); }\n}\n')
            sdk = {"project": "g/sdk", "module": "pom.xml",
                   "base_sha": old, "head_sha": new,
                   "java_change_paths": [path]}
            change = {
                "identity": "METHOD:status/0", "type": "a.Dto",
                "simple_name": "Dto", "path": path,
                "old": {"line": 3, "signature": "String()"},
                "new": {"line": 3, "signature": "int()"}}
            entries = {
                "g/sdk": {"repo": sdk_repo, "base": old, "head": new},
                "g/user": {"repo": user_repo, "base": None, "head": consumer_sha}}
            snapshots = {"g/sdk": {
                "base": {"names": [path]}, "head": {"names": [path]}}}
            findings, gaps = attributed_uses(
                sdk, "g/user", entries, snapshots, [change], root / "cache")
            self.assertEqual({"base", "head"},
                             {hit["producer_side"] for hit in findings},
                             "Attribution gaps: " + str(gaps[:4]))
            self.assertEqual({consumer_path}, {hit["consumer_path"] for hit in findings})
            self.assertEqual({7}, {hit["line"] for hit in findings})
            self.assertTrue(all(hit["binding_status"] ==
                                "JAVAC_ATTRIBUTED_SDK_SOURCE" for hit in findings))


if __name__ == "__main__":
    unittest.main()
