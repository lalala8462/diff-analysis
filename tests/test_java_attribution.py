"""Javac attributed method binding tests. Only compiler analysis, not project execution."""
import shutil
import tempfile
import unittest
from pathlib import Path

from diff_analysis.scanner import scan_tree, build_graph, java_toolchain, compatible_source_scope
from diff_analysis.relation_graph import side_graph


class AttributedJavaTests(unittest.TestCase):
    def test_missing_source_scope_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'source_scope'):
            compatible_source_scope({'source_scope': ''}, {})
        self.assertTrue(compatible_source_scope({'source_scope': ''}, {'source_scope': ''}))

    def test_maven_module_source_scope(self):
        methods, calls, errors = self._run_java({
            'one/src/main/java/p/Parent.java': 'package p; class Parent { void invoke() {} }',
            'one/src/main/java/p/Child.java': 'package p; class Child extends Parent { void go() { invoke(); } }',
            'one/src/test/java/p/Test.java': 'package p; class Test { void go() { new Parent().invoke(); } }',
            'two/src/main/java/q/Other.java': 'package q; class Other { void go() {} }'})
        self.assertFalse(errors, errors)
        self.assertEqual(['p.Parent#invoke()', 'p.Parent#invoke()'],
                         [c['target_binding_id'] for c in calls if c['name'] == 'invoke'])

    def test_main_cannot_bind_test_or_unrelated_module_source(self):
        methods, calls, errors = self._run_java({
            'one/src/main/java/p/A.java': 'package p; class A { void f() { Test.run(); Other.run(); } }',
            'one/src/test/java/p/Test.java': 'package p; class Test { static void run() {} }',
            'two/src/main/java/p/Other.java': 'package p; class Other { static void run() {} }'})
        self.assertTrue(errors)
        self.assertFalse(any(c['target_binding_id'] for c in calls
                             if c['path'].endswith('A.java') and c['name'] == 'run'))
        reverse, _ = build_graph(methods, calls)
        self.assertFalse(any('p.A#f()' in callers for callers in reverse.values()))

    def test_duplicate_symbols_are_not_overwritten(self):
        methods, _, errors = self._run_java({
            'one/src/main/java/p/A.java': 'package p; class A { void f() {} }',
            'two/src/main/java/p/A.java': 'package p; class A { void f() {} }'})
        self.assertNotIn('p.A#f()', methods)
        self.assertTrue(any(e.get('reason') == 'DUPLICATE_METHOD_SYMBOL' for e in errors))

    def _run_java(self, sources):
        try:
            java_toolchain()
        except Exception as exc:
            self.skipTest("JDK unavailable: " + str(exc))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, source in sources.items():
                p = root / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(source, encoding="utf-8")
            return scan_tree(root, root / "scanner-cache")

    def test_overload_same_arity_has_exact_javac_target(self):
        methods, calls, errors = self._run_java({
            "a/Demo.java": (
                "package a;\n"
                "class Demo {\n"
                "  void accept(String x) {}\n"
                "  void accept(Integer x) {}\n"
                "  void run() { accept(\"hello\"); }\n"
                "}\n")})
        self.assertFalse(errors)
        targets = [x for x in calls if x["name"] == "accept"]
        self.assertEqual(1, len(targets))
        self.assertIn("java.lang.String", targets[0]["target_binding_id"])
        self.assertEqual("JAVAC_ATTRIBUTED", targets[0]["binding_status"])
        graph = side_graph({"project": "a/demo", "base": "0"*40, "head": "1"*40,
                            "methods": methods, "calls": calls,
                            'java_coverage': {'HEAD': {'status': 'COMPLETE'}}}, "HEAD")
        hits = [e for e in graph["edges"] if e["caller"].endswith("#run()")]
        self.assertEqual(1, len(hits))
        self.assertIn("String", hits[0]["callee"])
        self.assertEqual("JAVAC_ATTRIBUTED", hits[0]["binding"])
        self.assertEqual("RESOLVED_CODE_RELATION", hits[0]["status"])

    def test_same_project_inherited_method_attributed(self):
        methods, calls, errors = self._run_java({
            "p/Parent.java": "package p; class Parent { void invoke() {} }\n",
            "p/Child.java": "package p; class Child extends Parent { void go() { invoke(); } }\n"
        })
        target = next(x for x in calls if x["name"] == "invoke")
        self.assertEqual("p.Parent#invoke()", target["target_binding_id"])
        reverse, unresolved = build_graph(methods, calls)
        self.assertTrue(any("Child#go()" in src
                            for callers in reverse.values() for src in callers))

    def test_type_unavailable_does_not_upgrade_edge(self):
        methods = {
            "p.Demo#run()": {"class": "p.Demo", "name": "run",
                             "arity": 0, "path": "Demo.java",
                             "start": 1, "end": 2},
            "p.Demo#go()": {"class": "p.Demo", "name": "go",
                            "arity": 0, "path": "Demo.java",
                            "start": 4, "end": 5}}
        for method in methods.values():
            method['source_scope'] = ''
        calls = [{"owner": "p.Demo#run()", "name": "go",
                  "receiver": "this", "receiver_type": "p.Demo",
                  "arity": 0, "path": "Demo.java", "line": 2,
                  "target_binding_id": ""}]
        graph = side_graph({"project": "p/demo", "head": "1"*40, "base": "0"*40,
                            "methods": methods, "calls": calls,
                            'java_coverage': {'HEAD': {'status': 'COMPLETE'}}}, "HEAD")
        self.assertEqual("STATIC_CANDIDATE", graph["edges"][0]["status"])
        self.assertEqual("NOT_TYPE_CHECKED", graph["edges"][0]["binding"])


if __name__ == "__main__":
    unittest.main()
