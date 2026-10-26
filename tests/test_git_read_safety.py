"""固定 Git 对象读取不得调用仓库配置的外部差异程序。"""
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from diff_analysis.gitops import git, diff_hunks
from diff_analysis.release_analysis import final_diff_ledger


class GitReadSafetyTests(unittest.TestCase):
    def test_ledger_and_history_do_not_execute_textconv_or_external_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init", "-q")
            git(repo, "config", "user.name", "Test")
            git(repo, "config", "user.email", "test@example.com")
            git(repo, "config", "core.autocrlf", "false")
            (repo / ".gitattributes").write_text(
                "*.java diff=probe\n", encoding="utf-8")
            source = repo / "A.java"
            source.write_text("class A { int value = 1; }\n", encoding="utf-8")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "base")
            base = git(repo, "rev-parse", "HEAD").decode().strip()
            source.write_text("class A { int value = 2; }\n", encoding="utf-8")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "TEST-1 head")
            head = git(repo, "rev-parse", "HEAD").decode().strip()
            marker = root / "executed.txt"
            helper = root / "text converter.py"
            helper.write_text(
                "from pathlib import Path\n"
                "Path(" + repr(str(marker)) + ").write_text('executed', encoding='utf-8')\n"
                "print('converted source')\n", encoding="utf-8")
            command = " ".join(shlex.quote(str(p).replace("\\", "/"))
                               for p in (sys.executable, helper))
            git(repo, "config", "diff.probe.textconv", command)
            git(repo, "config", "diff.external", command)

            ledger = final_diff_ledger(repo, "test/repo", base, head)
            hunks = diff_hunks(repo, base, head, "A.java")
            git(repo, "log", "-p", base + ".." + head)
            git(repo, "show", head, "--", "A.java")
            git(repo, "blame", head, "--", "A.java")
            self.assertFalse(marker.exists())
            self.assertEqual(1, len(hunks))
            self.assertEqual(1, hunks[0]["new_start"])
            change = ledger["final_changes"][0]
            self.assertIn("value = 1", change["before"][0]["text"])
            self.assertIn("value = 2", change["after"][0]["text"])
            # 对照查询证明转换器配置确实生效，前面的未执行断言不是空测。
            subprocess.check_output([
                "git", "-C", str(repo), "diff", "--no-ext-diff", base, head],
                stderr=subprocess.STDOUT)
            self.assertTrue(marker.exists())


if __name__ == "__main__":
    unittest.main()
