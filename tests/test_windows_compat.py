"""Portable GitLab authentication and Windows path safety regression tests."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from diff_analysis.gitlab import git_auth_environment
from diff_analysis.gitops import safe_source_path
from diff_analysis.scanner import scan_tree


class PortableAuthTests(unittest.TestCase):
    def test_askpass_answers_git_credential_prompt(self):
        # This test must pass on both Git for Windows and Linux git.
        token = "test-only-NOT-A-REAL-TOKEN_99"
        with git_auth_environment(token) as env:
            helper = Path(env["GIT_ASKPASS"])
            self.assertTrue(helper.exists())
            self.assertNotIn(token, helper.read_text(encoding="utf-8"))
            result = subprocess.run(
                ["git", "-c", "credential.helper=", "credential", "fill"],
                input="protocol=https\nhost=gitlab.example.cn\n\n",
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", env=env)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("username=oauth2", result.stdout)
            self.assertIn("password=" + token, result.stdout)
        self.assertFalse(helper.exists())

    def test_askpass_has_native_platform_extension(self):
        with git_auth_environment("dummy-token") as env:
            ext = ".cmd" if os.name == "nt" else ".sh"
            self.assertTrue(env["GIT_ASKPASS"].endswith(ext))


class WindowsPathTests(unittest.TestCase):
    def test_windows_reserved_names_and_characters(self):
        for path in ("src/CON.java", "src/aux.XML", "src/X:bad.java",
                     "src/file?.java", "src/hello .java", "src/../hack.java"):
            self.assertFalse(safe_source_path(path, windows=True), path)
        self.assertTrue(safe_source_path("src/main/java/中文/Pay.java", windows=True))
        self.assertTrue(safe_source_path("src/main/java/Pay.java", windows=False))

    def test_utf8_java_symbol_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "项目源码"
            root.mkdir()
            (root / "订单.java").write_text(
                "package demo; class 订单 { void 查询() { } }",
                encoding="utf-8")
            methods, calls, errors = scan_tree(root, Path(td) / "cache")
            self.assertFalse(errors)
            self.assertIn("demo.订单#查询()", methods)


if __name__ == "__main__":
    unittest.main()
