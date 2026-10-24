"""Hash-pinned per-side classpath configuration without Maven or Gradle execution."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from diff_analysis.java_classpath import load_classpaths, for_project


class JavaClasspathTests(unittest.TestCase):
    def test_different_side_classpaths_and_hash_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            basejar = root / "v1.jar"
            headjar = root / "v2.jar"
            basejar.write_bytes(b"old type definitions")
            headjar.write_bytes(b"new type definitions")
            sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
            manifest = root / "classpath.json"
            payload = {"schema_version": "java-classpath-v1", "projects": [{
                "project": "team/service", "base_sha": "a"*40,
                "head_sha": "b"*40,
                "base": [{"path": str(basejar), "sha256": sha(basejar)}],
                "head": [{"path": str(headjar), "sha256": sha(headjar)}]}]}
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            pins = load_classpaths(manifest)
            chosen = for_project(pins, "team/service", "a"*40, "b"*40)
            self.assertIn("v1.jar", chosen["BASE"]["classpath"])
            self.assertIn("v2.jar", chosen["HEAD"]["classpath"])
            self.assertNotEqual(chosen["BASE"]["fingerprint"],
                                chosen["HEAD"]["fingerprint"])
            headjar.write_bytes(b"tampered dependency")
            with self.assertRaises(ValueError):
                load_classpaths(manifest)

    def test_no_implicit_classpath(self):
        self.assertEqual({}, load_classpaths(None))
        self.assertEqual({"BASE": None, "HEAD": None},
                         for_project({}, "team/one", "a"*40, "b"*40))


if __name__ == "__main__":
    unittest.main()
