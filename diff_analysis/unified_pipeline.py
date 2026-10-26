"""Derived unified impact outputs independent of immutable Git facts."""
import json
from pathlib import Path
from .unified_graph import build_unified_graph, propagate_unified
from .review_workflow import _write_json, _hash, load_batch, _state


def _bindings(root):
    path = Path(root) / "bindings_latest.json"
    return json.loads(path.read_text("utf-8")) if path.is_file() else None


def rebuild_unified(run_dir, binding_report=None, units=None, affected=None):
    root, release = load_batch(run_dir)
    if binding_report is None:
        binding_report = _bindings(root)
    revision = None
    if units is None:
        state = _state(root)
        if state["group_revisions"]:
            entry = state["group_revisions"][-1]
            units, revision = entry["units"], entry["revision"]
    graph = build_unified_graph(release, binding_report, units=units)
    previous_file = root / "unified_impact_latest.json"
    previous = (json.loads(previous_file.read_text("utf-8"))
                if previous_file.is_file() else None)
    fingerprint = _hash(graph["edges"])
    previous_cache = (previous.get("cache", {}) if previous and
                      previous.get("edge_fingerprint") == fingerprint else {})
    groups = set(graph["sources"])
    if previous_cache:
        groups.update(previous_cache)
    cache, reused, computed = {}, 0, 0
    for group in sorted(groups):
        source = graph["sources"].get(group, [])
        existing = previous_cache.get(group)
        if (existing and existing.get("sources") == source and
                (affected is None or group not in affected)):
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
                     "affected_groups": sorted(affected or [])},
        "warning": "No cross-project edge is runtime verified"}
    _write_json(root / "unified_graph_latest.json", graph)
    _write_json(root / "unified_impact_latest.json", outcome)
    if revision is not None:
        folder = root / "revisions"
        folder.mkdir(exist_ok=True)
        _write_json(folder / ("unified-impact-%03d.json" % revision), outcome)
    return {"edges": len(graph["edges"]), "findings": len(results),
            "cross_project_questions": len(questions),
            "reused_groups": reused, "recomputed_groups": computed,
            "omissions": len(graph["omissions"])}
