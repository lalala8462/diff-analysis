"""Versioned Java + protocol reverse-impact regression scenarios."""
import unittest

from diff_analysis.unified_graph import build_unified_graph, propagate_unified


OLD, NEW = "a" * 40, "b" * 40
S1 = "p.A#pay()"
S2 = "p.B#consume()"
S3 = "p.B#notify()"
D1, G1, G2 = "D-aaaaaaaaaaaaaaaa", "G-1111111111111111", "G-2222222222222222"


def method(name, start):
    return {name: {"path": "Service.java", "start": start, "end": start + 4}}


def graph(project, side, sha, methods, calls=None):
    return {"project": project, "side": side, "snapshot_sha": sha,
            "method_index": methods, "edges": calls or []}


def endpoint(epid, project, sha, side, role, start):
    return {"id": epid, "project": project, "sha": sha,
            "side": side, "role": role,
            "evidence": {"path": "Service.java", "start": start, "end": start}}


def fixture():
    change = {"id": D1, "project": "svc/a", "path": "Service.java",
              "old_path": "Service.java",
              "old_range": {"start": 10, "count": 2},
              "new_range": {"start": 10, "count": 2},
              "symbols_old": [S1], "symbols_new": [S1]}
    ledgers = [
        {"project": "svc/a", "base": OLD, "head": NEW,
         "final_changes": [change]},
        {"project": "svc/b", "base": OLD, "head": NEW, "final_changes": []}
    ]
    graphs = []
    for side, sha in (("BASE", OLD), ("HEAD", NEW)):
        graphs.append(graph("svc/a", side, sha, method(S1, 10)))
        graphs.append(graph("svc/b", side, sha,
                            {**method(S2, 30), **method(S3, 50)},
                            [{"id": "call-" + side, "caller": S3, "callee": S2,
                              "binding": "JAVAC_ATTRIBUTED"}]))
    release = {
        "schema_version": "release-evidence-v1", "ledgers": ledgers,
        "groups": [{"id": G1, "change_ids": [D1]}],
        "relations": {"graphs": graphs}}
    bind = {
        "endpoints": [
            endpoint("ep-provider", "svc/a", NEW, "HEAD", "PROVIDER", 10),
            endpoint("ep-consumer", "svc/b", NEW, "HEAD", "CONSUMER", 30)
        ],
        "edges": [{
            "id": "con1", "side": "HEAD", "kind": "DUBBO",
            "caller_endpoint": "ep-consumer",
            "callee_endpoint": "ep-provider",
            "caller_sha": NEW, "callee_sha": NEW,
            "binding_status": "USER_ATTESTED_MATCH",
            "evidence_refs": ["ep-provider", "ep-consumer"]
        }]
    }
    return release, bind


class UnifiedGraphTests(unittest.TestCase):
    def test_multi_hop_cross_project_consumer_reachability(self):
        release, bindings = fixture()
        unified = build_unified_graph(release, bindings)
        self.assertTrue(any(x["kind"] == "DUBBO" for x in unified["edges"]))
        result = propagate_unified(unified)
        head_hits = [x for x in result["findings"]
                     if x["side"] == "HEAD" and x["consumer_project"] == "svc/b"]
        self.assertTrue(any(x["consumer_node"].endswith(S2) for x in head_hits))
        self.assertTrue(any(x["consumer_node"].endswith(S3) for x in head_hits))
        self.assertTrue(all(x["confidence"] != "RUNTIME_VERIFIED" for x in head_hits))

    def test_old_and_new_snapshots_never_cross(self):
        release, bindings = fixture()
        unified = build_unified_graph(release, bindings)
        impacts = propagate_unified(unified)["findings"]
        self.assertFalse(any(x["side"] == "BASE" and
                             x["consumer_project"] == "svc/b" for x in impacts))
        self.assertTrue(any(x["side"] == "HEAD" and
                            x["consumer_project"] == "svc/b" for x in impacts))

    def test_modified_contract_file_can_seed_protocol_endpoint(self):
        release, bindings = fixture()
        entry = release["ledgers"][0]["final_changes"][0]
        entry["path"] = "resources/service.xml"
        entry["old_path"] = "resources/service.xml"
        entry["symbols_new"] = []
        entry["symbols_old"] = []
        bindings["endpoints"][0]["evidence"]["path"] = "resources/service.xml"
        unified = build_unified_graph(release, bindings)
        impact = propagate_unified(unified)["findings"]
        self.assertTrue(any(x["consumer_project"] == "svc/b" for x in impact))

    def test_manual_split_does_not_invent_old_version_ownership(self):
        release, bindings = fixture()
        units = {D1: [{
            "id": "U-1", "primary_group": G2, "range_basis": "NEW_LINE_SEGMENT_ONLY",
            "new_range": {"start": 10, "count": 1}, "old_range": None,
            "unassigned": False}]}
        graph_result = build_unified_graph(release, bindings, units=units)
        self.assertIn(G2, graph_result["sources"])
        self.assertNotIn(G1, graph_result["sources"])
        self.assertTrue(any(o["reason"] == "UNIT_RANGE_NOT_PROVEN" for o in
                            graph_result["omissions"]))
        self.assertTrue(all("|HEAD|" in n for n in graph_result["sources"][G2]))

    def test_unconfirmed_connection_has_weaker_evidence(self):
        release, bindings = fixture()
        bindings["edges"][0]["binding_status"] = "BINDING_UNCONFIRMED"
        graph_result = build_unified_graph(release, bindings)
        paths = propagate_unified(graph_result)["findings"]
        self.assertTrue(any(x["confidence"] == "UNCONFIRMED"
                            for x in paths if x["consumer_project"] == "svc/b"))


if __name__ == "__main__":
    unittest.main()
