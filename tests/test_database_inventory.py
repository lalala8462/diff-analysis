"""Stage 2: pinned table-use observations and conservative cross-project results."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from diff_analysis.database_inventory import extract_table_uses, analyze_table_impacts


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args],
                                   stderr=subprocess.STDOUT).decode("utf-8").strip()


def setup(path):
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.com")
    return path


def commit(repo, path, text):
    file = repo / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "test")
    return git(repo, "rev-parse", "HEAD")


class TableInventoryTests(unittest.TestCase):
    def test_mybatis_java_and_ddl(self):
        xml = ('<mapper><select id="one">SELECT * FROM trans_record\n'
               ' WHERE state=1</select><update id="two">'
               'UPDATE accounting.trans_record SET x=1</update></mapper>')
        uses, gaps = extract_table_uses(xml, "RecordMapper.xml")
        self.assertEqual(["trans_record", "accounting.trans_record"],
                         [x["table"] for x in uses])
        self.assertEqual(["READ", "WRITE"], [x["role"] for x in uses])
        java = ('@DS("master")\n@TableName("trans_record")\n'
                'class Record {}\n@Select("SELECT * FROM trans_record")')
        uses, _ = extract_table_uses(java, "Record.java")
        self.assertEqual(["MAPPING", "READ"], [x["role"] for x in uses])
        self.assertTrue(all(x["datasource_hint"] == "master" for x in uses))
        ddl, _ = extract_table_uses(
            'CREATE TABLE trans_record (id INT);\nALTER TABLE trans_record ADD x INT;', "ddl.sql")
        self.assertEqual(["DDL", "DDL"], [x["role"] for x in ddl])
        self.assertEqual([1, 2], [x["start"] for x in ddl])

    def test_delete_from_is_a_write_not_a_read(self):
        uses, gaps = extract_table_uses(
            "DELETE FROM trans_record WHERE status='CLOSED';", "clean.sql")
        self.assertEqual([], gaps)
        self.assertEqual([("trans_record", "WRITE")],
                         [(x["table"], x["role"]) for x in uses])

    def test_delete_is_a_writer_and_sql_strings_are_not_tables(self):
        uses, gaps = extract_table_uses(
            "DELETE FROM trans_record WHERE note = 'FROM fake_table';",
            "cleanup.sql")
        self.assertEqual([], gaps)
        self.assertEqual(["trans_record"], [use["table"] for use in uses])
        self.assertEqual(["WRITE"], [use["role"] for use in uses])

    def test_comments_and_random_literals_do_not_create_table(self):
        text = ('/* SELECT * FROM fake_table */\n'
                'SELECT * FROM real_table;\n-- UPDATE fake_table SET a=1\n')
        uses, _ = extract_table_uses(text, "schema.sql")
        self.assertEqual(["real_table"], [x["table"] for x in uses])
        uses, _ = extract_table_uses(
            'class X {String message = "please update fake_table";}', "X.java")
        self.assertEqual([], uses)

    def test_changed_table_finds_unchanged_consumer_and_reuses_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a, b, c = [setup(root / n) for n in ("adpts", "webapi", "other")]
            path = "src/main/resources/RecordMapper.xml"
            old = commit(a, path,
                         '<mapper><select id="get">SELECT * FROM trans_record WHERE state=0</select></mapper>')
            head = commit(a, path,
                          '<mapper><select id="get">SELECT * FROM trans_record WHERE state=1</select></mapper>')
            consumer = commit(b, "src/Read.java",
                              'class Read { @Select("SELECT * FROM trans_record") Object find() {return null;} }')
            other = commit(c, "dao.sql", "SELECT * FROM another_table;")
            entries = {"adpts": {"repo": a, "base": old, "head": head},
                       "webapi": {"repo": b, "base": None, "head": consumer},
                       "other": {"repo": c, "base": None, "head": other}}
            ledger = {"project": "adpts", "base": old, "head": head,
                      "final_changes": [{"id": "D-1", "path": path, "old_path": path,
                                         "old_range": {"start": 1, "count": 1},
                                         "new_range": {"start": 1, "count": 1}}]}
            result = analyze_table_impacts(entries, [ledger], root / "cache")
            self.assertEqual(1, len(result["impacts"]))
            impact = result["impacts"][0]
            self.assertEqual("trans_record", impact["table"])
            self.assertEqual("webapi", impact["consumer_project"])
            self.assertEqual(consumer, impact["consumer_sha"])
            self.assertEqual(["D-1"], impact["change_ids"])
            self.assertEqual("TABLE_NAME_ONLY_UNCONFIRMED", impact["status"])
            again = analyze_table_impacts(entries, [ledger], root / "cache")
            self.assertEqual(4, again["cache"]["hits"])
            self.assertEqual(result["impacts"], again["impacts"])

    def test_unrelated_sql_statement_not_treated_as_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a, b = setup(root / "a"), setup(root / "b")
            old = commit(a, "queries.sql",
                         "SELECT * FROM trans_record;\nSELECT * FROM other_table;\n")
            head = commit(a, "queries.sql",
                          "SELECT * FROM trans_record;\nSELECT * FROM other_table WHERE state=1;\n")
            sha = commit(b, "dao.sql", "SELECT * FROM trans_record;")
            entries = {"a": {"repo": a, "base": old, "head": head},
                       "b": {"repo": b, "base": None, "head": sha}}
            ledger = {"project": "a", "base": old, "head": head,
                      "final_changes": [{"id": "D-2", "path": "queries.sql",
                                         "old_path": "queries.sql",
                                         "old_range": {"start": 2, "count": 1},
                                         "new_range": {"start": 2, "count": 1}}]}
            result = analyze_table_impacts(entries, [ledger], root / "cache")
            self.assertEqual([], result["impacts"])

    def test_different_datasource_hint_does_not_confirm_shared_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a, b = setup(root / "a"), setup(root / "b")
            old = commit(a, "Record.java",
                         '@DS("one")\n@TableName("other_record")\nclass R {}')
            head = commit(a, "Record.java",
                          '@DS("one")\n@TableName("trans_record")\nclass R {}')
            sha = commit(b, "Record.java",
                         '@DS("two")\n@TableName("trans_record")\nclass R {}')
            entries = {"a": {"repo": a, "base": old, "head": head},
                       "b": {"repo": b, "base": None, "head": sha}}
            ledger = {"project": "a", "base": old, "head": head,
                      "final_changes": [{"id": "D-3", "path": "Record.java",
                                         "old_path": "Record.java",
                                         "old_range": {"start": 2, "count": 1},
                                         "new_range": {"start": 2, "count": 1}}]}
            result = analyze_table_impacts(entries, [ledger], root / "cache")
            self.assertEqual("DATASOURCE_HINTS_DIFFER_UNCONFIRMED",
                             result["impacts"][0]["status"])

    def test_evidence_role_is_not_a_change_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a, b = setup(root / "a"), setup(root / "b")
            old = commit(a, "dao.sql", "SELECT * FROM trans_record;")
            head = commit(a, "dao.sql", "UPDATE trans_record SET x=1;")
            sha = commit(b, "dao.sql", "SELECT * FROM trans_record;")
            entries = {"a": {"repo": a, "base": old, "head": head},
                       "b": {"repo": b, "base": None, "head": sha}}
            ledger = {"project": "a", "base": old, "head": head, "role": "EVIDENCE",
                      "final_changes": [{"id": "D-4", "path": "dao.sql", "old_path": "dao.sql",
                                         "old_range": {"start": 1, "count": 1},
                                         "new_range": {"start": 1, "count": 1}}]}
            self.assertEqual([], analyze_table_impacts(entries, [ledger], root / "cache")["impacts"])


if __name__ == "__main__":
    unittest.main()
