"""Fixed-SHA SDK contract extraction and unchanged consumer regression tests."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from diff_analysis.maven_inventory import analyze
from diff_analysis.release_analysis import final_diff_ledger
from diff_analysis.sdk_contracts import declarations, _consumer_uses


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], stderr=subprocess.STDOUT
    ).decode("utf-8").strip()


def setup(path):
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.com")
    return path


def commit(repo, path, text):
    dest = repo / path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "test")
    return git(repo, "rev-parse", "HEAD")


class ContractTests(unittest.TestCase):
    def test_field_annotation_method_enum_and_private_exclusion(self):
        code = '''package a;
public class Dto {
  @JsonProperty("old_name")
  private String status;
  public String getStatus() { return status; }
  private void helper() {}
}'''
        model = declarations(code, "src/main/java/a/Dto.java")
        self.assertEqual("a.Dto", model["type"])
        self.assertIn("serialized=old_name", model["members"]["FIELD:status"]["signature"])
        self.assertEqual("String()", model["members"]["METHOD:getStatus/0"]["signature"])
        self.assertNotIn("METHOD:helper/0", model["members"])
        enum = declarations("package a; public enum State { READY, FAILED, UNKNOWN; }", "State.java")
        self.assertIn("ENUM:UNKNOWN", enum["members"])

    def test_import_alone_not_a_priority(self):
        contract = {"type": "a.Dto", "simple_name": "Dto", "identity": "METHOD:getStatus/0"}
        self.assertEqual([], _consumer_uses("import a.Dto;\nclass C {}", contract))
        code = '''import a.Dto;
class C {
    Dto dto;
    void process() { dto.getStatus(); }
}'''
        self.assertEqual([4], [p["line"] for p in _consumer_uses(code, contract)])
        self.assertEqual([], _consumer_uses("class C { void f() { dto.getStatus(); } }", contract))

    def test_unchanged_consumers_and_dependency_version_independent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sdk, client = setup(root / "sdk"), setup(root / "client")
            pom = "<project><groupId>g</groupId><artifactId>demo-api</artifactId><version>1</version></project>"
            base = commit(sdk, "pom.xml", pom)
            java = "src/main/java/a/Dto.java"
            before = '''package a;
public class Dto {
    public String getStatus() { return ""; }
}'''
            after = '''package a;
public class Dto {
    public int getStatus() { return 1; }
}'''
            base = commit(sdk, java, before)
            head = commit(sdk, java, after)
            cpom = ("<project><groupId>g</groupId><artifactId>client</artifactId><version>1</version>"
                    "<dependencies><dependency><groupId>g</groupId><artifactId>demo-api</artifactId>"
                    "<version>1</version></dependency></dependencies></project>")
            commit(client, "pom.xml", cpom)
            consumer_sha = commit(client, "src/main/java/C.java", '''import a.Dto;
class C {
    Dto dto;
    void process() { dto.getStatus(); }
}''')
            specs = [(sdk, "g/api", base, head)]
            ledger = final_diff_ledger(sdk, "g/api", base, head)
            manifest = root / "projects.json"
            manifest.write_text(json.dumps({"projects": [
                {"project": "g/client", "repo": str(client), "revision": consumer_sha}
            ]}), encoding="utf-8")
            result = analyze(specs, [ledger], manifest)
            from diff_analysis.human_report import write_html
            output = root / "report"
            output.mkdir()
            write_html(output, {"ledgers": [ledger]}, specs, [], manifest)
            html = (output / "report.html").read_text("utf-8")
            self.assertIn("SDK 契约与消费者项目：g/client", html)
            self.assertIn("METHOD:getStatus/0", html)
            self.assertEqual(1, len(result["contract_impacts"]))
            impact = result["contract_impacts"][0]
            self.assertEqual("g/client", impact["consumer"])
            self.assertEqual(consumer_sha, impact["consumer_head_sha"])
            self.assertEqual("1", impact["dependency_version"])
            self.assertEqual("METHOD:getStatus/0", impact["uses"][0]["contract"]["identity"])
            self.assertEqual("MODIFIED", impact["uses"][0]["contract"]["status"])
            self.assertFalse(any(x["kind"] == "变化类引用SDK" for x in result["alerts"]))

    def test_import_only_does_not_produce_contract_use(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sdk, client = setup(root / "sdk"), setup(root / "client")
            pom = "<project><groupId>g</groupId><artifactId>demo-api</artifactId><version>1</version></project>"
            commit(sdk, "pom.xml", pom)
            base = commit(sdk, "src/main/java/a/Dto.java",
                          "package a;\npublic class Dto {\n public String getStatus();\n}")
            head = commit(sdk, "src/main/java/a/Dto.java",
                          "package a;\npublic class Dto {\n public int getStatus();\n}")
            cpom = ("<project><groupId>g</groupId><artifactId>client</artifactId><version>1</version>"
                    "<dependencies><dependency><groupId>g</groupId><artifactId>demo-api</artifactId>"
                    "<version>1</version></dependency></dependencies></project>")
            commit(client, "pom.xml", cpom)
            sha = commit(client, "src/main/java/C.java", "import a.Dto;\nclass C {}\n")
            manifest = root / "projects.json"
            manifest.write_text(json.dumps({"projects": [
                {"project": "g/client", "repo": str(client), "revision": sha}
            ]}), encoding="utf-8")
            result = analyze([(sdk, "g/api", base, head)],
                             [final_diff_ledger(sdk, "g/api", base, head)], manifest)
            self.assertEqual([], result["contract_impacts"])


if __name__ == "__main__":
    unittest.main()
