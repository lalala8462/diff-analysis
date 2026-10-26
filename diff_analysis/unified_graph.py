"""Version-pinned Java / Dubbo / HTTP / MQ / SQL impact graph.

Cross-project edges require the separately reviewed connection-binding report.
A source match or an attestation is *not* runtime verification. Every reverse
path carries the weakest evidence status; BASE/HEAD never connect implicitly.
"""
from collections import defaultdict, deque
from .relation_graph import stable_id


def _node(kind, side, project, sha, identity):
    return "|".join((kind, side, project, sha, identity))


def _intersects(a, b):
    if not a or not b:
        return False
    x, n = a.get("start"), a.get("count")
    y, end = b.get("start"), b.get("end")
    return all(isinstance(v, int) for v in (x, n, y, end)) and (
        x > 0 and n > 0 and x <= end and x + n - 1 >= y)


def _review_sources(release, graphs, units=None):
    changed = {c["id"]: (l, c) for l in release.get("ledgers", [])
               for c in l["final_changes"]}
    owners = {cid: g["id"] for g in release.get("groups", [])
              for cid in g["change_ids"]}
    sources, omissions = defaultdict(set), []
    for cid, (ledger, change) in changed.items():
        segments = units.get(cid, []) if units is not None else []
        if units is None:
            segments = [{"primary_group": owners.get(cid),
                         "range_basis": "WHOLE_RAW_CHANGE",
                         "old_range": change.get("old_range"),
                         "new_range": change.get("new_range"),
                         "id": cid}]
        for unit in segments:
            group = unit.get("primary_group")
            if not group or unit.get("unassigned"):
                continue
            for side in ("BASE", "HEAD"):
                sha = ledger["base" if side == "BASE" else "head"]
                graph = graphs.get((ledger["project"], side, sha))
                if graph is None:
                    omissions.append({"unit_id": unit.get("id"), "side": side,
                                      "reason": "GRAPH_NOT_AVAILABLE"})
                    continue
                range_key = "old_range" if side == "BASE" else "new_range"
                region = unit.get(range_key)
                if unit.get("range_basis") != "WHOLE_RAW_CHANGE" and not region:
                    omissions.append({"unit_id": unit.get("id"), "side": side,
                                      "reason": "UNIT_RANGE_NOT_PROVEN"})
                    continue
                if unit.get("range_basis") == "WHOLE_RAW_CHANGE":
                    symbols = change.get("symbols_old" if side == "BASE" else
                                         "symbols_new", change.get("symbols", []))
                else:
                    path = change.get("old_path" if side == "BASE" else "path")
                    symbols = [symbol for symbol, method in graph.get("method_index", {}).items()
                               if method.get("path") == path and _intersects(region, method)]
                for symbol in symbols:
                    if symbol in graph.get("method_index", {}):
                        sources[group].add(_node("method", side, ledger["project"], sha, symbol))
                    else:
                        omissions.append({"unit_id": unit.get("id"), "side": side,
                                          "reason": "METHOD_NOT_INDEXED", "symbol": symbol})
    return sources, omissions


def build_unified_graph(release, binding_report=None, units=None):
    """Build immutable source topology and separately keyed group source labels."""
    graphs = {(g["project"], g["side"], g["snapshot_sha"]): g for g in
              release.get("relations", {}).get("graphs", [])}
    scope = {(l["project"], side): l["base" if side == "BASE" else "head"]
             for l in release.get("ledgers", []) for side in ("BASE", "HEAD")}
    edges, omissions, endpoints = [], [], {}
    for (project, side, sha), graph in sorted(graphs.items()):
        for edge in graph.get("edges", []):
            edges.append({
                "id": "UE-" + stable_id("", "JAVA", edge["id"], side),
                "caller": _node("method", side, project, sha, edge["caller"]),
                "callee": _node("method", side, project, sha, edge["callee"]),
                "side": side, "kind": "JAVA_CALL", "evidence_ids": [edge["id"]],
                "binding_status": edge.get("binding", "NOT_TYPE_CHECKED"),
                "confidence": ("ATTRIBUTED" if edge.get("binding") == "JAVAC_ATTRIBUTED"
                               else "CANDIDATE")})
    report = binding_report or {}
    for ep in report.get("endpoints", []):
        project, side, sha = ep["project"], ep["side"], ep["sha"]
        if scope.get((project, side)) != sha:
            omissions.append({"endpoint_id": ep["id"], "reason": "ENDPOINT_SHA_MISMATCH"})
            continue
        node = _node("endpoint", side, project, sha, ep["id"])
        endpoints[ep["id"]] = (ep, node)
        graph = graphs.get((project, side, sha))
        if not graph:
            omissions.append({"endpoint_id": ep["id"], "reason": "ENDPOINT_METHOD_GRAPH_MISSING"})
            continue
        evidence = ep["evidence"]
        anchored = [(s, m) for s, m in graph.get("method_index", {}).items()
                    if m.get("path") == evidence["path"] and
                    _intersects({"start": evidence["start"],
                                 "count": evidence["end"] - evidence["start"] + 1}, m)]
        if len(anchored) != 1:
            omissions.append({"endpoint_id": ep["id"],
                              "reason": ("ENDPOINT_METHOD_AMBIGUOUS" if anchored else
                                         "ENDPOINT_METHOD_NOT_LOCATED"),
                              "candidate_count": len(anchored)})
            continue
        method_symbol = anchored[0][0]
        method_node = _node("method", side, project, sha, method_symbol)
        consumer = ep["role"] in ("CONSUMER", "CLIENT", "READER")
        # Client method calls its protocol endpoint; provider endpoint calls
        # the implementation method. Both are direction caller -> callee.
        edges.append({
            "id": "UE-" + stable_id("", ep["id"], "ANCHOR", method_symbol),
            "caller": method_node if consumer else node,
            "callee": node if consumer else method_node,
            "side": side, "kind": "ENDPOINT_METHOD_ANCHOR",
            "evidence_ids": [ep["id"]],
            "binding_status": "PINNED_SOURCE_RANGE",
            "confidence": "CANDIDATE"})
    for connection in report.get("edges", []):
        left = endpoints.get(connection["caller_endpoint"])
        right = endpoints.get(connection["callee_endpoint"])
        if not left or not right:
            omissions.append({"connection_id": connection["id"],
                              "reason": "ENDPOINT_NOT_IN_BATCH"})
            continue
        caller, callee = left[0], right[0]
        if (caller["side"] != callee["side"] or caller["side"] != connection["side"] or
                caller["sha"] != connection["caller_sha"] or
                callee["sha"] != connection["callee_sha"]):
            omissions.append({"connection_id": connection["id"],
                              "reason": "CROSS_SIDE_OR_SHA_MISMATCH"})
            continue
        status = connection.get("binding_status", "BINDING_UNCONFIRMED")
        edges.append({
            "id": "UE-" + stable_id("", connection["id"]),
            "caller": left[1], "callee": right[1],
            "side": connection["side"], "kind": connection["kind"],
            "evidence_ids": connection.get("evidence_refs", []) + [connection["id"]],
            "binding_status": status, "confidence": (
                "ATTESTED_NOT_RUNTIME_VERIFIED" if status == "USER_ATTESTED_MATCH"
                else "UNCONFIRMED")})
    sources, source_gaps = _review_sources(release, graphs, units)
    omissions.extend(source_gaps)
    # A modified contract or config can be associated with its own endpoint
    # only on exact file/range overlap, without guessing a method.
    for cid, group in sorted({cid: g["id"] for g in release.get("groups", [])
                              for cid in g["change_ids"]}.items()):
        if units is not None:
            members = units.get(cid, [])
        else:
            members = [{"primary_group": group, "range_basis": "WHOLE_RAW_CHANGE"}]
        found = next(((l, c) for l in release["ledgers"]
                      for c in l["final_changes"] if c["id"] == cid), None)
        if not found:
            continue
        ledger, change = found
        for unit in members:
            owner = unit.get("primary_group")
            if not owner or unit.get("unassigned"):
                continue
            for ep, node in endpoints.values():
                if ep["project"] != ledger["project"]:
                    continue
                side = ep["side"]
                sha = ledger["base" if side == "BASE" else "head"]
                if sha != ep["sha"]:
                    continue
                region = (unit.get("old_range" if side == "BASE" else "new_range")
                          if units is not None and unit.get("range_basis") != "WHOLE_RAW_CHANGE"
                          else change.get("old_range" if side == "BASE" else "new_range"))
                if not region:
                    continue
                path = change.get("old_path" if side == "BASE" else "path")
                if path == ep["evidence"]["path"] and _intersects(region, ep["evidence"]):
                    sources[owner].add(node)
    edges.sort(key=lambda e: e["id"])
    return {"schema_version": "unified-graph-v1", "edges": edges,
            "sources": {g: sorted(nodes) for g, nodes in sorted(sources.items())},
            "omissions": omissions, "edge_count": len(edges),
            "limits": {"java_graphs": len(graphs),
                       "cross_project_links": sum(e["kind"] in
                           ("DUBBO", "HTTP", "MQ", "DATABASE") for e in edges)},
            "warning": "Attested edges are not verified runtime links; missing endpoints "
                       "and ambiguous method anchors remain omissions"}


def propagate_unified(graph, groups=None, max_states=100000, max_hops=24):
    """Reverse traversal through methods and version-bound protocol endpoints."""
    reverse = defaultdict(list)
    for edge in graph["edges"]:
        reverse[edge["callee"]].append(edge)
    for entries in reverse.values():
        entries.sort(key=lambda e: e["id"])
    chosen = groups if groups is not None else sorted(graph["sources"])
    results, coverage = [], {"states": 0, "truncated": False}
    for group in chosen:
        queue = deque((origin, origin, [], "ATTRIBUTED", 0)
                      for origin in graph["sources"].get(group, []))
        seen = set()
        while queue:
            origin, current, path, strength, hops = queue.popleft()
            key = (origin, current)
            if key in seen:
                continue
            if coverage["states"] >= max_states:
                coverage["truncated"] = True
                break
            seen.add(key)
            coverage["states"] += 1
            if current != origin:
                components = current.split("|", 4)
                results.append({
                    "id": stable_id("UI-", group, origin, current),
                    "group_id": group, "source_node": origin, "consumer_node": current,
                    "consumer_project": components[2], "consumer_sha": components[3],
                    "side": components[1], "consumer_kind": components[0],
                    "path_edge_ids": path, "hops": hops,
                    "confidence": strength,
                    "status": "REVIEW_REQUIRED"})
            if hops >= max_hops:
                if reverse[current]:
                    coverage["truncated"] = True
                continue
            for edge in reverse[current]:
                if edge["caller"].split("|", 2)[1] != current.split("|", 2)[1]:
                    continue
                nxt = strength
                if edge["confidence"] != "ATTRIBUTED":
                    nxt = ("UNCONFIRMED" if edge["confidence"] == "UNCONFIRMED" or
                           strength == "UNCONFIRMED" else "CANDIDATE")
                queue.append((origin, edge["caller"], path + [edge["id"]], nxt, hops + 1))
    return {"findings": results, "coverage": coverage}
