"""Version-pinned call relations and multi-source impact propagation.

No invented cross-version paths: each side has independent graph identities.
Name/arity or simple-class matches are candidates, not proven Java bindings.
"""
import hashlib
import json
import re
from collections import defaultdict, deque


def stable_id(prefix, *parts):
    value = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return prefix + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def side_graph(review, side, max_edges=100000):
    project = review["project"]
    sha = review["base"] if side == "BASE" else review["head"]
    methods = review.get("old_methods", {}) if side == "BASE" else review.get("methods", {})
    calls = review.get("old_calls", []) if side == "BASE" else review.get("calls", [])
    by_full = defaultdict(list)
    by_simple = defaultdict(list)
    by_binding = defaultdict(list)
    for symbol, method in methods.items():
        if method.get("binding_id"):
            by_binding[method["binding_id"]].append(symbol)
        name, arity = method["name"], int(method["arity"])
        by_full[(method["class"], name, arity)].append(symbol)
        by_simple[(method["class"].split(".")[-1].split("$")[-1],
                   name, arity)].append(symbol)
    edges, unresolved = [], []
    limit = False
    for call in calls:
        owner = call.get("owner")
        if owner not in methods:
            unresolved.append({**call, "reason": "CALLER_NOT_INDEXED", "side": side})
            continue
        recv = call.get("receiver", "")
        typ = call.get("receiver_type", "")
        name, arity = call["name"], int(call["arity"])
        target = call.get("target_binding_id")
        if target:
            candidates = by_binding.get(target, [])
            resolution = "JAVAC_METHOD_SYMBOL"
            strength = "RESOLVED_CODE_RELATION"
        elif not recv or recv in ("this", "super"):
            candidates = by_full.get((methods[owner]["class"], name, arity), [])
            resolution = "SAME_CLASS_NAME_ARITY"
            strength = "STATIC_CANDIDATE"  # overloading and inheritance not fully resolved
        elif typ:
            simple = re.sub(r"<.*", "", typ).split(".")[-1].split("$")[-1].strip()
            candidates = by_simple.get((simple, name, arity), [])
            resolution = "RECEIVER_SIMPLE_TYPE_NAME_ARITY"
            strength = "STATIC_CANDIDATE"
        else:
            candidates = []
            resolution, strength = "TYPE_UNKNOWN", "BINDING_UNCONFIRMED"
        if len(candidates) != 1:
            unresolved.append({**call, "side": side, "reason": (
                "AMBIGUOUS_CALL" if candidates else
                "ATTRIBUTED_TARGET_NOT_INDEXED" if target else "TARGET_NOT_FOUND"),
                               "candidate_count": len(candidates)})
            continue
        if len(edges) >= max_edges:
            limit = True
            continue
        callee = candidates[0]
        edges.append({
            "id": stable_id("E-", project, sha, side, owner, callee,
                            call.get("path"), call.get("line")),
            "project": project, "snapshot_sha": sha, "side": side,
            "caller": owner, "callee": callee,
            "caller_project": project, "callee_project": project,
            "caller_sha": sha, "callee_sha": sha,
            "path": call.get("path"), "line": call.get("line"),
            "kind": "JAVA_CALL", "resolution": resolution,
            "status": strength,
            "binding": "JAVAC_ATTRIBUTED" if target else "NOT_TYPE_CHECKED",
            "limitation": ("" if target else
                           "AST name/arity candidate, not complete Java resolution")
        })
    return {"project": project, "snapshot_sha": sha, "side": side,
            "methods": sorted(methods),
            "method_index": {
                symbol: {"path": m.get("path"), "start": m.get("start"),
                         "end": m.get("end"),
                         "class": m.get("class"), "name": m.get("name"),
                         "signature": m.get("signature"),
                         "binding_id": m.get("binding_id", "")}
                for symbol, m in methods.items()},
            "edges": edges,
            "unresolved_calls": unresolved,
            "coverage": {"calls": len(calls), "edges": len(edges),
                         "unresolved": len(unresolved),
                         "truncated": limit,
                         "omitted_edges": max(0, len(calls) - len(edges) - len(unresolved)),
                         "graph_complete": not (limit or review.get("incomplete"))}}


def propagate_impacts(graph, sources, max_states=200000):
    """Reverse reachability keyed by (source_id, node), not only node.

    Includes changed-to-changed edges and multiple sources converging on
    one consumer. Stores at most one representative path for each pair.
    """
    reverse = defaultdict(list)
    for edge in graph["edges"]:
        reverse[edge["callee"]].append(edge)
    for edges in reverse.values():
        edges.sort(key=lambda e: (e["caller"], e["id"]))
    queue = deque()
    for group_id, symbols in sorted(sources.items()):
        for symbol in sorted(symbols):
            queue.append((group_id, symbol, symbol, [], 0))
    seen, findings = set(), {}
    exhausted = False
    while queue:
        group_id, origin, node, path, hops = queue.popleft()
        key = (group_id, origin, node)
        if key in seen:
            continue
        if len(seen) >= max_states:
            exhausted = True
            break
        seen.add(key)
        finding_id = stable_id("I-", graph["project"], graph["snapshot_sha"],
                               graph["side"], group_id, origin, node)
        findings[finding_id] = {
            "id": finding_id, "side": graph["side"],
            "snapshot_sha": graph["snapshot_sha"],
            "project": graph["project"], "source_group": group_id,
            "source_symbol": origin, "consumer_symbol": node,
            "hop_count": hops, "representative_edge_ids": path,
            "status": "STATIC_CANDIDATE",
            "limitation": "Path follows unverified AST candidate edges"}
        for edge in reverse[node]:
            queue.append((group_id, origin, edge["caller"],
                          path + [edge["id"]], hops + 1))
    return {"findings": sorted(findings.values(), key=lambda x: x["id"]),
            "coverage": {"states": len(seen), "truncated": exhausted,
                         "max_states": max_states}}


def analyze_relations(reviews, ledgers, by_change, cross_candidates=None):
    """Return complete known direct edges independent of representative paths."""
    changes = {}
    for ledger in ledgers:
        for change in ledger["final_changes"]:
            changes[(change["project"], change["id"])] = change
    graphs, impacts, direct_checks, consumer_checks = [], [], [], []
    for review in reviews:
        project = review["project"]
        for side in ("BASE", "HEAD"):
            graph = side_graph(review, side)
            graphs.append(graph)
            sources = defaultdict(set)
            members = defaultdict(set)
            for (pr, cid), change in changes.items():
                if pr != project:
                    continue
                # D-* is a final net difference; old/new identity can differ.
                symbols = change.get("symbols_old" if side == "BASE"
                                     else "symbols_new", change.get("symbols", []))
                gid = by_change.get(cid)
                if gid:
                    for symbol in symbols:
                        sources[gid].add(symbol)
                        members[symbol].add(gid)
            edge_ids = set()
            for edge in graph["edges"]:
                ga, gb = members.get(edge["caller"], set()), members.get(edge["callee"], set())
                for a in sorted(ga):
                    for b in sorted(gb):
                        if a == b:
                            continue
                        identifier = stable_id("X-", side, edge["id"], a, b)
                        if identifier in edge_ids:
                            continue
                        edge_ids.add(identifier)
                        direct_checks.append({
                            "id": identifier, "kind": "DIRECT_JAVA_CALL_" + side,
                            "from_group": a, "to_group": b,
                            "status": "CHECK_TOGETHER",
                            "evidence": [{"edge_id": edge["id"],
                                          "caller": edge["caller"],
                                          "callee": edge["callee"],
                                          "side": side, "binding": edge["binding"]}],
                            "limitation": "Candidate AST edge; no business binding proved"})
            spread = propagate_impacts(graph, sources)
            impacts.extend(spread["findings"])
            graph["propagation_coverage"] = spread["coverage"]
            for hit in spread["findings"]:
                if hit["hop_count"] == 0:
                    continue
                if hit["consumer_symbol"] in members:
                    continue
                consumer_checks.append({
                    "id": hit["id"], "group_id": hit["source_group"],
                    "consumer_project": project,
                    "consumer_symbol": hit["consumer_symbol"],
                    "change_id": None, "status": "CANDIDATE",
                    "source": "VERSION_PINNED_JAVA_CALL_" + side,
                    "evidence": {"side": side, "sha": graph["snapshot_sha"],
                                 "edge_ids": hit["representative_edge_ids"]},
                    "limitation": "Static unverified caller; not a confirmed runtime dependency"})
    # Existing name/arity cross-project matches stay weak and side-scoped.
    connections = []
    scope = {(g["project"], g["side"]): g["snapshot_sha"] for g in graphs}
    for c in cross_candidates or []:
        side = c.get("side", "HEAD")
        left = (c.get("caller_project"), side)
        right = (c.get("callee_project"), side)
        if left not in scope or right not in scope:
            continue
        connections.append({
            "id": stable_id("C-", side, c.get("caller_project"), c.get("caller"),
                            c.get("callee_project"), c.get("callee"), c.get("line")),
            "side": side, "kind": "JAVA_INTERFACE_CANDIDATE",
            "caller_project": left[0], "caller_sha": scope[left],
            "callee_project": right[0], "callee_sha": scope[right],
            "caller": c.get("caller"), "callee": c.get("callee"),
            "evidence": {"path": c.get("path"), "line": c.get("line")},
            "binding_status": "BINDING_UNCONFIRMED",
            "missing": ["DEPENDENCY_VERSION", "IMPLEMENTATION_OR_SERVICE_BINDING"],
            "limitation": "Name/arity match only; no runtime service identity"})
    return {"schema_version": "relation-graph-v1", "graphs": graphs,
            "impact_findings": impacts, "direct_group_checks": direct_checks,
            "internal_consumer_candidates": consumer_checks,
            "connection_points": connections}
