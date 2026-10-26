"""Incremental re-evaluation of manual ReviewUnit assignments on immutable graphs.

Raw Hunk records are untouched. Change groups determine source tags, not
where a code relation exists. Old lines without a validated subrange are
omitted with explicit uncertainty rather than attributed by guesswork.
"""
import json
from collections import defaultdict
from pathlib import Path

from .relation_graph import propagate_impacts, stable_id
from .review_workflow import load_batch, _state, _hash, _write_json, _question


def _overlap(a, b):
    if not a or not b:
        return False
    x, y = a.get("start"), a.get("count")
    if not isinstance(x, int) or not isinstance(y, int) or x < 1 or y < 1:
        return False
    lo, hi = b.get("start"), b.get("end")
    return isinstance(lo, int) and isinstance(hi, int) and (
        x <= hi and x + y - 1 >= lo)


def _sources(release, by_diff):
    changes = {c["id"]: c for l in release["ledgers"]
               for c in l["final_changes"]}
    graphs = release.get("relations", {}).get("graphs", [])
    by_graph = {}
    gaps = []
    for g in graphs:
        method_index = g.get("method_index")
        if method_index is None:
            gaps.append({"project": g["project"], "side": g["side"],
                         "status": "GRAPH_METHOD_LOCATIONS_MISSING"})
            continue
        sources = defaultdict(set)
        members = defaultdict(set)
        for diff_id, units in by_diff.items():
            change = changes.get(diff_id)
            if not change or change["project"] != g["project"]:
                continue
            side = g["side"]
            field = "new_range" if side == "HEAD" else "old_range"
            symbol_key = "symbols_new" if side == "HEAD" else "symbols_old"
            for unit in units:
                group = unit.get("primary_group")
                if not group or unit.get("unassigned"):
                    continue
                region = unit.get(field)
                if (unit.get("range_basis") != "WHOLE_RAW_CHANGE" and
                        side == "BASE" and not region):
                    gaps.append({"unit_id": unit["id"], "diff_id": diff_id,
                                 "group_id": group, "side": side,
                                 "status": "OLD_UNIT_RANGE_NOT_PROVEN"})
                    continue
                if unit.get("range_basis") == "WHOLE_RAW_CHANGE":
                    syms = change.get(symbol_key, change.get("symbols", []))
                else:
                    syms = [name for name, info in method_index.items()
                            if info.get("path") == change.get(
                                "path" if side == "HEAD" else "old_path") and
                            _overlap(region, info)]
                if not syms:
                    gaps.append({"unit_id": unit["id"], "diff_id": diff_id,
                                 "group_id": group, "side": side,
                                 "status": "NO_EXACT_METHOD_MAPPING"})
                for symbol in syms:
                    if symbol not in method_index:
                        gaps.append({"unit_id": unit["id"], "side": side,
                                     "status": "SYMBOL_NOT_IN_VERSION"})
                        continue
                    sources[group].add(symbol)
                    members[symbol].add(group)
        by_graph[(g["project"], g["side"], g["snapshot_sha"])] = (
            sources, members)
    return by_graph, gaps


def recompute_review(run_dir):
    root, release = load_batch(run_dir)
    state = _state(root)
    if not state["group_revisions"]:
        raise ValueError("Run has no manual grouping revisions")
    revision = state["group_revisions"][-1]
    revnum = revision["revision"]
    by_diff = revision["units"]
    sources, gaps = _sources(release, by_diff)
    folder = root / "revisions"
    folder.mkdir(exist_ok=True)
    prior_path = folder / ("recomputed-%03d.json" % (revnum - 1))
    prior = (json.loads(prior_path.read_text("utf-8"))
             if revnum > 1 and prior_path.is_file() else None)
    affected = set(revision["affected_group_ids"])
    reused, recomputed = 0, 0
    impacts, checks, consumers = [], [], []
    impact_cache = {}
    for graph in release.get("relations", {}).get("graphs", []):
        key = (graph["project"], graph["side"], graph["snapshot_sha"])
        if key not in sources:
            continue
        source_groups, members = sources[key]
        previous = {}
        if prior:
            previous = {
                name: hits for name, hits in prior.get("impact_cache", {}).get(
                    "|".join(key), {}).items()}
        current_cache = {}
        for group, symbols in sorted(source_groups.items()):
            stable_symbols = sorted(symbols)
            hit = previous.get(group)
            if hit and hit.get("source_symbols") == stable_symbols and group not in affected:
                findings = hit["findings"]
                reused += 1
            else:
                result = propagate_impacts(graph, {group: symbols})
                findings = result["findings"]
                recomputed += 1
                if result["coverage"]["truncated"]:
                    gaps.append({"group_id": group, "side": graph["side"],
                                 "status": "PROPAGATION_BUDGET_EXHAUSTED"})
            current_cache[group] = {"source_symbols": stable_symbols,
                                    "findings": findings}
            impacts.extend(findings)
            for h in findings:
                if h["hop_count"] == 0 or h["consumer_symbol"] in members:
                    continue
                consumers.append({
                    "id": h["id"], "group_id": group,
                    "consumer_symbol": h["consumer_symbol"],
                    "consumer_project": graph["project"],
                    "side": graph["side"],
                    "status": "STATIC_CANDIDATE",
                    "source_symbol": h["source_symbol"],
                    "edge_ids": h["representative_edge_ids"]})
        for edge in graph["edges"]:
            a, b = members.get(edge["caller"], set()), members.get(edge["callee"], set())
            for ga in sorted(a):
                for gb in sorted(b):
                    if ga == gb:
                        continue
                    checks.append({
                        "id": stable_id("X-", graph["side"], edge["id"], ga, gb),
                        "side": graph["side"],
                        "kind": "DIRECT_JAVA_CALL", "edge_id": edge["id"],
                        "from_group": ga, "to_group": gb,
                        "binding_status": edge["binding"],
                        "status": "CHECK_TOGETHER"})
        impact_cache["|".join(key)] = current_cache
    old_questions = json.loads((root / "questions.json").read_text("utf-8"))
    questions = [q for q in old_questions["questions"]
                 if not set(q["group_ids"]) & affected and
                    not set(q["change_ids"]) & set(
                        revision["affected_change_ids"])]
    for group in sorted(affected):
        members = sorted(did for did, units in by_diff.items()
                         if any(u["primary_group"] == group for u in units))
        if not members:
            continue
        questions.append(_question(
            "REVISED_GROUP_BEHAVIOR", group + "-r" + str(revnum),
            "人工修订后的本组行为及消费者是否仍符合对应目标？",
            [group], members, ["DIFF", "METHOD", "RELATION", "EXPECTED_GOAL"],
            "必须按修订单元核对；旧版范围未知时不作确定判断"))
    # Connection facts do not change when assigning a different group.
    result = {
        "schema_version": "recomputed-review-v1",
        "revision": revnum,
        "source_group_revision_id": revision["id"],
        "created_from_run": release["run_id"] if "run_id" in release else None,
        "raw_change_fingerprint": _hash([
            (l["project"], l["base"], l["head"],
             [c["id"] for c in l["final_changes"]])
            for l in release["ledgers"]]),
        "affected_groups": sorted(affected),
        "impact_findings": impacts, "direct_group_checks": checks,
        "consumer_candidates": consumers, "questions": questions,
        "impact_cache": impact_cache,
        "coverage_gaps": gaps,
        "recomputed_groups": recomputed, "reused_groups": reused,
        "invalidated_decision_ids": revision["decisions_needing_review"],
        "limitation": "Full edge topology reused from fixed scans; only group source "
                      "propagation is recomputed. Unmapped old subranges remain unknown."
    }
    _write_json(folder / ("recomputed-%03d.json" % revnum), result)
    _write_json(root / "recomputed_latest.json", result)
    # Recompute unified cross-project links from the same ReviewUnit overlay.
    # Do not assume a particular protocol binding is deployed.
    from .unified_pipeline import rebuild_unified
    unified = rebuild_unified(root, units=by_diff,
                              affected=set(revision["affected_group_ids"]))
    from .revision_pipeline import update_review_package
    package = update_review_package(root)
    return {"unified": unified, "review_package": package, "revision": revnum, "impact_findings": len(impacts),
            "direct_group_checks": len(checks),
            "recomputed_groups": recomputed,
            "reused_groups": reused, "coverage_gaps": len(gaps),
            "questions": len(questions)}
