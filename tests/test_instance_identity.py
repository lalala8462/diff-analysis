"""Pinned JDBC/Redis configuration evidence: matching is not deployment proof."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from diff_analysis.instance_identity import (
    _config, _endpoints, attach_instance_evidence, project_instances)


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], stderr=subprocess.STDOUT).decode().strip()


def init(path):
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.com")
    return path


def commit(repo, path, content):
    dst = repo / path
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(content, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "config")
    return git(repo, "rev-parse", "HEAD")


class InstanceIdentityTests(unittest.TestCase):
    def test_yaml_and_dynamic_routing(self):
        text = ("spring:\n  datasource:\n    dynamic:\n      datasource:\n"
                "        master:\n          url: jdbc:mysql://db:3306/billing\n"
                "  redis:\n    host: cache\n    port: 6379\n    database: 2\n")
        records, gaps = _endpoints(_config(text, "application.yml"),
                                  "default", "application.yml")
        self.assertEqual([], gaps)
        self.assertEqual({"DATABASE", "REDIS"}, {x["kind"] for x in records})
        self.assertEqual("master", next(x["route"] for x in records
                                        if x["kind"] == "DATABASE"))

    def test_redis_sentinel_identity_without_live_connection(self):
        content = ("spring.redis.sentinel.master=mymaster\n"
                   "spring.redis.sentinel.nodes=cache-b:26379,cache-a:26379\n"
                   "spring.redis.database=0\n")
        records, gaps = _endpoints(
            _config(content, "application.properties"),
            "default", "application.properties")
        self.assertEqual([], gaps)
        self.assertEqual(1, len(records))
        self.assertEqual("SENTINEL", records[0]["topology"])
        self.assertNotIn("cache-a", str(records))
        self.assertTrue(records[0]["configuration_only"])

    def test_opaque_secrets_and_dynamic_placeholders(self):
        records, gaps = _endpoints(_config(
            "spring.datasource.url=jdbc:mysql://db:3306/insure?password=secret\n"
            "spring.redis.host=${REDIS_HOST}\n"
            "spring.redis.port=6379\nspring.redis.database=0\n",
            "application.properties"), "default", "application.properties")
        self.assertEqual(1, len(records))
        self.assertNotIn("secret", str(records))
        self.assertNotIn("db:3306", str(records))
        self.assertTrue(any(g["reason"] == "DYNAMIC_REDIS_ENDPOINT" for g in gaps))

    def test_distinct_endpoints_match_and_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            producer, consumer = init(root / "producer"), init(root / "consumer")
            props = ("spring.datasource.url=jdbc:mysql://db:3306/pay\n"
                     "spring.redis.host=redis.internal\nspring.redis.port=6379\n"
                     "spring.redis.database=0\n")
            a = commit(producer, "src/main/resources/application.properties", props)
            b = commit(consumer, "src/main/resources/application.properties", props)
            entries = {"a": {"repo": producer, "head": a},
                       "b": {"repo": consumer, "head": b}}
            db = {"impacts": [{
                "table": "trans_record", "producer_project": "a",
                "consumer_project": "b", "producer_uses": [],
                "consumer_uses": [], "status": "TABLE_NAME_ONLY_UNCONFIRMED"}]}
            redis = {"impacts": [{
                "kind": "REDIS_KEY", "resource": "policy:",
                "producer_project": "a", "consumer_project": "b",
                "source_uses": [], "consumer_uses": [], "status": "STATIC_RESOURCE_NAME_ONLY_UNCONFIRMED"}]}
            scope = attach_instance_evidence(entries, db, redis, root / "cache")
            self.assertEqual(0, len(scope["omissions"]))
            self.assertEqual("CONFIG_ENDPOINT_MATCH_NOT_DEPLOYMENT_PROOF",
                             db["impacts"][0]["instance_evidence"]["status"])
            self.assertEqual("CONFIG_ENDPOINT_MATCH_NOT_DEPLOYMENT_PROOF",
                             redis["impacts"][0]["instance_evidence"]["status"])
            self.assertIn("src/main/resources/application.properties",
                          db["impacts"][0]["instance_evidence"]["producer_source_paths"])
            self.assertTrue(scope["configuration_evidence"]["a"])
            self.assertNotIn("password", str(scope["configuration_evidence"]))
            c = commit(consumer, "src/main/resources/application.properties",
                       props.replace("jdbc:mysql://db:3306/pay",
                                     "jdbc:mysql://other:3306/pay")
                            .replace("database=0", "database=1"))
            entries["b"]["head"] = c
            attach_instance_evidence(entries, db, redis, root / "cache")
            self.assertEqual("CONFIG_ENDPOINT_MISMATCH_UNCONFIRMED",
                             db["impacts"][0]["instance_evidence"]["status"])
            self.assertEqual("CONFIG_ENDPOINT_MISMATCH_UNCONFIRMED",
                             redis["impacts"][0]["instance_evidence"]["status"])

    def test_profiles_are_not_assumed_same_environment(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = init(root / "project")
            sha = commit(repo, "application-dev.properties",
                         "spring.datasource.url=jdbc:mysql://db:3306/pay\n")
            result = project_instances(repo, "p", sha, root / "cache")
            self.assertEqual("dev", result["endpoints"][0]["profile"])


if __name__ == "__main__":
    unittest.main()
