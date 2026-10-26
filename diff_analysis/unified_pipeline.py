"""Derived unified impact outputs independent of immutable Git facts."""
import json
from collections import defaultdict, deque
from pathlib import Path
from .unified_graph import build_unified_graph, propagate_unified
from .review_workflow import _write_json, _hash, load_batch, _state


def _bindings(root):
    path = Path(root) / "bindings_latest.json"
    return json.loads(path.read_text("utf-8")) if path.is_file() else None


def _topology_affected(previous_graph, current_graph, sources, max_nodes=100000):
    """Conservatively detect whose reverse-reachable call edges could change.

    A changed caller -> callee link affects a source when its callee can be
    reached from that source in either topology. If graph traversal is capped,
    force recomputation instead of incorrectly reusing stale results.
    """
    if not previous_graph:
        return set(sources)
    before = {e["id"]: e for e in previous_graph.get("edges", [])}
    after = {e["id"]: e for e in current_graph["edges"]}
    changed_ids = {eid for eid in before.keys() | after.keys()
                   if before.get(eid) != after.get(eid)}
    if not changed_ids:
        return set()
    changed_callees = {e["callee"] for eid in changed_ids
                       for e in (before.get(eid), after.get(eid)) if e}
    reverse = defaultdict(set)
    for edge in list(before.values()) + list(after.values()):
        reverse[edge["callee"]].add(edge["caller"])
    affected = set()
    for group, starts in sources.items():
        seen, queue = set(), deque(starts)
        while queue and len(seen) < max_nodes:
            node = queue.popleft()
            if node in seen:
                continue
            seen.add(node)
            if node in changed_callees:
                affected.add(group)
                break
            queue.extend(reverse[node] - seen)
        if queue and len(seen) >= max_nodes:
            affected.add(group)
    return affected


def rebuild_unified(run_dir, binding_report=None, units=None, affected=None):
    root, release = load_batch(run_dir)
    if binding_report is None:
        binding_report = _bindings(root)
    state = _state(root)
    latest = state["group_revisions"][-1] if state["group_revisions"] else None
    revision = latest["revision"] if latest else None
    if units is None and latest is not None:
        units = latest["units"]
    graph = build_unified_graph(release, binding_report, units=units)
    previous_graph_file = root / "unified_graph_latest.json"
    previous_graph = (json.loads(previous_graph_file.read_text("utf-8"))
                      if previous_graph_file.is_file() else None)
    previous_file = root / "unified_impact_latest.json"
    previous = (json.loads(previous_file.read_text("utf-8"))
                if previous_file.is_file() else None)
    fingerprint = _hash(graph["edges"])
    previous_cache = previous.get("cache", {}) if previous else {}
    topology_changed_groups = _topology_affected(
        previous_graph, graph, graph["sources"])
    changed_groups = set(affected or []) | topology_changed_groups
    groups = set(graph["sources"])
    if previous_cache:
        groups.update(previous_cache)
    cache, reused, computed = {}, 0, 0
    for group in sorted(groups):
        source = graph["sources"].get(group, [])
        existing = previous_cache.get(group)
        if (existing and existing.get("sources") == source and
                group not in changed_groups and
                not existing.get("coverage", {}).get("truncated")):
            cache[group] = existing
            reused += 1
        elif source:
            findings = propagate_unified(graph, [group])
            cache[group] = {"sources": source, **findings}
            computed += 1
    results = sorted((f for v in cache.values() for f in v["findings"]),
                     key=lambda x: x["id"])
    questions = []
    for item in results:
        if item["consumer_project"] == item["source_node"].split("|", 4)[2]:
            continue
        questions.append({
            "id": "Q-" + _hash(["UNIFIED_CROSS_CONSUMER", item["id"]])[:16],
            "kind": "CROSS_PROJECT_CONSUMER", "group_ids": [item["group_id"]],
            "change_ids": [], "consumer_project": item["consumer_project"],
            "consumer_node": item["consumer_node"], "side": item["side"],
            "sha": item["consumer_sha"], "path_edge_ids": item["path_edge_ids"],
            "confidence": item["confidence"], "status": "NEEDS_REVIEW",
            "revision": 1,
            "question": "跨项目消费者是否依赖该变化，当前固定版本是否兼容？",
            "condition": "双方方法、契约、环境及版本绑定均需人工核实",
            "human_status": "NOT_REVIEWED", "ai_status": "NOT_IMPORTED",
            "binding_status": "UNCONFIRMED",
            "necessary_evidence": ["CONSUMER_METHOD", "CONTRACT", "BINDING"],
            "limitation": "Potential cross-project impact, not proven deployed behavior"})
    outcome = {
        "schema_version": "unified-impact-v1", "revision": revision,
        "graph_fingerprint": fingerprint,
        "edge_fingerprint": fingerprint,
        "binding_fingerprint": _hash(binding_report or {}),
        "cache": cache, "findings": results, "questions": questions,
        "reused_groups": reused, "recomputed_groups": computed,
        "coverage": {"truncated": any(c["coverage"]["truncated"] for c in cache.values()),
                     "omissions": len(graph["omissions"]),
                     "affected_groups": sorted(changed_groups)},
        "warning": "No cross-project edge is runtime verified"}
    _write_json(root / "unified_graph_latest.json", graph)
    _write_json(root / "unified_impact_latest.json", outcome)
    if revision is not None:
        folder = root / "revisions"
        folder.mkdir(exist_ok=True)
        snapshot = _hash([
            outcome["edge_fingerprint"], outcome["binding_fingerprint"],
            graph["sources"], outcome["findings"]])[:12]
        _write_json(folder / ("unified-impact-%03d-%s.json" %
                               (revision, snapshot)), outcome)
    return {"edges": len(graph["edges"]), "findings": len(results),
            "cross_project_questions": len(questions),
            "reused_groups": reused, "recomputed_groups": computed,
            "omissions": len(graph["omissions"])}
