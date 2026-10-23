"""P1-B: direct multi-change edge and independently pinned BASE/HEAD reachability."""
import unittest

from diff_analysis.relation_graph import side_graph, propagate_impacts, analyze_relations


def method(name):
    return {"class": "com.demo.Pay", "name": name, "arity": 0,
            "path": "Pay.java", "start": 1, "end": 9}


def call(owner, callee, line):
    return {"owner": owner, "name": callee, "receiver": "this",
            "receiver_type": "com.demo.Pay", "arity": 0,
            "path": "Pay.java", "line": line}


A, B, C, D = ("com.demo.Pay#" + n + "()" for n in ("a", "b", "c", "d"))
SHA0, SHA1 = ("a" * 40, "b" * 40)


def review():
    methods = {A: method("a"), B: method("b"), C: method("c"),
               D: method("d")}
    return {
        "project": "demo/pay", "base": SHA0, "head": SHA1,
        "old_methods": methods, "methods": methods,
        # Both changed groups are directly linked: a calls b.
        # c and a both flow through b; d calls both a and c.
        "old_calls": [call(A, B, 4), call(D, A, 5),
                      call(D, C, 6)],
        "calls": [call(A, B, 4), call(C, B, 7),
                  call(D, A, 5), call(D, C, 6)],
        "old_unresolved": [], "unresolved": [],
        "incomplete": False
    }


class RelationGraphTests(unittest.TestCase):
    def test_direct_changed_to_changed_edges_not_lost(self):
        ledgers = [{"project": "demo/pay", "final_changes": [
            {"id": "D-a", "project": "demo/pay", "symbols": [A],
             "symbols_old": [A], "symbols_new": [A]},
            {"id": "D-b", "project": "demo/pay", "symbols": [B],
             "symbols_old": [B], "symbols_new": [B]}
        ]}]
        data = analyze_relations([review()], ledgers,
                                 {"D-a": "G-a", "D-b": "G-b"})
        checks = data["direct_group_checks"]
        self.assertTrue(any(c["kind"] == "DIRECT_JAVA_CALL_BASE" for c in checks))
        self.assertTrue(any(c["kind"] == "DIRECT_JAVA_CALL_HEAD" for c in checks))
        self.assertTrue(all(c["evidence"][0]["edge_id"] for c in checks))

    def test_two_change_sources_can_reach_same_consumer(self):
        graph = side_graph(review(), "HEAD")
        spread = propagate_impacts(graph, {"G-a": {A}, "G-c": {C}})
        hits = {(h["source_group"], h["consumer_symbol"])
                for h in spread["findings"]}
        self.assertIn(("G-a", D), hits)
        self.assertIn(("G-c", D), hits)
        self.assertFalse(spread["coverage"]["truncated"])

    def test_base_only_call_is_preserved_not_joined_with_new_path(self):
        data = review()
        data["old_calls"] = [call(D, A, 5)]
        data["calls"] = []
        old, new = side_graph(data, "BASE"), side_graph(data, "HEAD")
        self.assertEqual(1, len(old["edges"]))
        self.assertEqual([], new["edges"])
        self.assertEqual(SHA0, old["edges"][0]["snapshot_sha"])
        self.assertEqual(SHA1, new["snapshot_sha"])

    def test_ambiguous_receiver_kept_unresolved(self):
        data = review()
        data["calls"] = [{
            "owner": A, "name": "missing", "receiver": "remote",
            "receiver_type": "Unknown", "arity": 2, "path": "Pay.java",
            "line": 8}]
        graph = side_graph(data, "HEAD")
        self.assertEqual([], graph["edges"])
        self.assertEqual("TARGET_NOT_FOUND",
                         graph["unresolved_calls"][0]["reason"])

    def test_propagation_budget_reports_incompleteness(self):
        graph = side_graph(review(), "HEAD")
        spread = propagate_impacts(graph, {"G-a": {A}}, max_states=1)
        self.assertTrue(spread["coverage"]["truncated"])
        self.assertEqual(1, spread["coverage"]["states"])


if __name__ == "__main__":
    unittest.main()
