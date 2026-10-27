"""Stage 3 regression fixtures: Redis names, DTOs, Spring and reflection."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from diff_analysis.shared_resources import (
    extract_resources, analyze_shared_resources, shared_snapshot)


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


def delta(path, base, head, diff_id="D-1", line=1):
    return {"project": "spms", "base": base, "head": head,
            "final_changes": [{"id": diff_id, "path": path, "old_path": path,
                               "old_range": {"start": line, "count": 1},
                               "new_range": {"start": line, "count": 1}}]}


class SharedResourcesTests(unittest.TestCase):
    def test_redis_literal_constant_prefix_and_dynamic_gaps(self):
        java = '''class App {
  static final String PREFIX = "policy:";
  void x(String id) {
    redisTemplate.opsForValue().set(PREFIX + id, "value");
    redisTemplate.opsForValue().get("policy:" + id);
    redisTemplate.opsForValue().get(calculateKey(id));
  }
}'''
        uses, gaps = extract_resources(java, "App.java")
        items = [u for u in uses if u["kind"] == "REDIS_KEY"]
        self.assertEqual(2, len(items))
        self.assertEqual(["policy:", "policy:"], [x["resource"] for x in items])
        self.assertTrue(all(not x["exact"] for x in items))
        self.assertIn("DYNAMIC_REDIS_KEY_UNRESOLVED",
                      [g["reason"] for g in gaps])

    def test_literals_are_not_fake_redis_calls_and_url_keys_survive(self):
        java = '''class Example {
  String sample = "ctx.getBean(\\\"wrongBean\\\")";
  String fake = "redisTemplate.opsForValue().get(\\\"fake:key\\\")";
  void real() {
    redisTemplate.opsForValue().get("http://cache/real");
  }
}'''
        observations, _ = extract_resources(java, "Example.java")
        keys = [x["resource"] for x in observations
                if x["kind"] == "REDIS_KEY"]
        self.assertEqual(["http://cache/real"], keys)
        self.assertFalse(any(x["kind"] == "SPRING_BEAN" and
                             x["resource"] == "wrongBean"
                             for x in observations))

    def test_spring_reflection_class_and_xml(self):
        java = '''package com.example;
@Service("paymentAgent")
class Agent {
  public void send() {}
  void test() {
    Class.forName("com.example.Agent");
    ctx.getBean("paymentAgent");
    type.getMethod("send");
  }
}'''
        uses, gaps = extract_resources(java, "Agent.java")
        def matches(kind, role, name):
            return [u for u in uses if u["kind"] == kind and
                    u["role"] == role and u["resource"] == name]
        self.assertTrue(matches("REFLECT_CLASS", "DECLARATION", "com.example.Agent"))
        self.assertTrue(matches("REFLECT_CLASS", "CONSUMER", "com.example.Agent"))
        self.assertTrue(matches("SPRING_BEAN", "DECLARATION", "paymentAgent"))
        self.assertTrue(matches("SPRING_BEAN", "CONSUMER", "paymentAgent"))
        self.assertTrue(matches("REFLECT_METHOD", "CONSUMER", "send"))
        xml, _ = extract_resources(
            '<beans><bean id="paymentAgent" class="com.example.Agent"/></beans>', "spring.xml")
        self.assertEqual(["paymentAgent"], [x["resource"] for x in xml])

    def test_changed_redis_key_find_unchanged_consumer(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            a, b = setup(root / "a"), setup(root / "b")
            old = commit(a, "Cache.java", 'class C { void run() { redisTemplate.opsForValue().get("policy:"); } }')
            head = commit(a, "Cache.java", 'class C { void run() { redisTemplate.opsForValue().get("policy:v2"); } }')
            consumer = commit(b, "Read.java", 'class R { void run() { redisTemplate.opsForValue().get("policy:"); } }')
            entries = {"spms": {"repo": a, "base": old, "head": head},
                       "webapi": {"repo": b, "base": None, "head": consumer}}
            result = analyze_shared_resources(entries, [delta("Cache.java", old, head)],
                                              root / "cache")
            candidates = [x for x in result["impacts"] if x["kind"] == "REDIS_KEY"]
            self.assertEqual(1, len(candidates))
            self.assertEqual("policy:", candidates[0]["resource"])
            self.assertEqual(consumer, candidates[0]["consumer_sha"])
            again = analyze_shared_resources(entries, [delta("Cache.java", old, head)],
                                             root / "cache")
            self.assertEqual(3, again["cache"]["hits"])
            self.assertEqual(result["impacts"], again["impacts"])

    def test_changed_spring_bean_and_reflection_from_old_version(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            a, b = setup(root / "a"), setup(root / "b")
            old = commit(a, "Agent.java", 'package p;\n@Service("paymentAgent")\npublic class Agent {}')
            head = commit(a, "Agent.java", 'package p;\n@Service("modernAgent")\npublic class Modern {}')
            consumer = commit(b, "Use.java",
                              'class C { void x() { ctx.getBean("paymentAgent"); Class.forName("p.Agent"); } }')
            entries = {"spms": {"repo": a, "base": old, "head": head},
                       "webapi": {"repo": b, "base": None, "head": consumer}}
            ledger = {"project": "spms", "base": old, "head": head,
                      "final_changes": [{"id": "D-2", "path": "Agent.java",
                                         "old_path": "Agent.java",
                                         "old_range": {"start": 2, "count": 2},
                                         "new_range": {"start": 2, "count": 2}}]}
            result = analyze_shared_resources(entries, [ledger], root / "cache")
            self.assertTrue(any(x["kind"] == "SPRING_BEAN" and
                                x["resource"] == "paymentAgent" for x in result["impacts"]))
            self.assertTrue(any(x["kind"] == "REFLECT_CLASS" and
                                x["resource"] == "p.Agent" for x in result["impacts"]))

    def test_changed_cache_dto_field_with_typed_write(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            a, b = setup(root / "a"), setup(root / "b")
            commit(a, "Store.java", '''class Store {
  void save(String id) {
    PolicyCache dto = null;
    redisTemplate.opsForValue().set("policy:" + id, dto);
  }
}''')
            old = commit(a, "PolicyCache.java",
                         'public class PolicyCache {\n private String state;\n}')
            head = commit(a, "PolicyCache.java",
                          'public class PolicyCache {\n private int state;\n}')
            consumer = commit(b, "Read.java",
                              'class R { void x(String id) { redisTemplate.opsForValue().get("policy:" + id); } }')
            entries = {"spms": {"repo": a, "base": old, "head": head},
                       "webapi": {"repo": b, "base": None, "head": consumer}}
            result = analyze_shared_resources(
                entries, [delta("PolicyCache.java", old, head, line=2)], root / "cache")
            dto = [x for x in result["impacts"] if x["kind"] == "REDIS_VALUE_DTO"]
            self.assertEqual(1, len(dto))
            self.assertIn("PolicyCache.state", dto[0]["resource"])
            self.assertEqual("DTO_FIELD_AND_TYPED_KEY_LEXICAL_ONLY", dto[0]["status"])


if __name__ == "__main__":
    unittest.main()
