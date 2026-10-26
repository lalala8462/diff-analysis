"""Targeted field, enum, SQL and consumer-branch clues from versioned code.

Uses only changed-line tokens and version graph's selected method/consumer
identities. A token reference is never treated as a proved data-flow edge.
"""
import re
import json
from collections import defaultdict
from pathlib import Path
from .gitops import git, check_sha, safe_source_path
from .review_workflow import load_batch, _hash, _write_json

WORDS = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
STOP = {"class", "public", "private", "protected", "static", "final", "void",
        "return", "new", "null", "true", "false", "this", "super", "if", "else",
        "case", "switch", "break", "default", "String", "int", "long",
        "boolean", "throw", "try", "catch", "import", "package", "extends",
        "implements", "Override", "get", "set"}
BRANCH = re.compile(r"\b(if|else\s+if|switch|case|return)\b|\?")
FIELD = re.compile(r"(?:private|public|protected)\s+(?:static\s+|final\s+)*"
                   r"[A-Za-z0-9_<>,.?\[\] ]+\s+[A-Za-z_$][\w$]*\s*[;=]")
SQL = re.compile(r"\b(SELECT|WHERE|JOIN|SET|INSERT|UPDATE|DELETE)\b", re.I)
ENUM = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")
MAX_SELECTED_TOKENS = 32
MAX_CONTEXTS = 250
MAX_BYTES = 4_000_000


def _tokens(change, side):
    field = "before" if side == "BASE" else "after"
    region = change.get("old_range" if side == "BASE" else "new_range", {})
    start = region.get("start", 0)
    end = start + region.get("count", 0)
    lines = [str(x.get("text", "")) for x in change.get(field, [])
             if start <= x.get("line", -1) < end]
    found = {t for line in lines for t in WORDS.findall(line)
             if t not in STOP and len(t) >= 3}
    return sorted(found)[:MAX_SELECTED_TOKENS]


def _selected_methods(release, groups):
    graph_by_key = {(g["project"], g["side"], g["snapshot_sha"]): g
                    for g in release.get("relations", {}).get("graphs", [])}
    selected = defaultdict(set)
    for group, changes in groups.items():
        for change, ledger in changes:
            for side, sha, symbol_key in (
                    ("BASE", ledger["base"], "symbols_old"),
                    ("HEAD", ledger["head"], "symbols_new")):
                key = (ledger["project"], side, sha)
                graph = graph_by_key.get(key)
                if graph:
                    for symbol in change.get(symbol_key, change.get("symbols", [])):
                        if symbol in graph.get("method_index", {}):
                            selected[(group,) + key].add(symbol)
    return graph_by_key, selected


def discover_contract_context(run_dir, repos):
    root, release = load_batch(run_dir)
    groups = defaultdict(list)
    change_lookup = {c["id"]: (c, ledger)
                     for ledger in release["ledgers"]
                     for c in ledger["final_changes"]}
    state_file = root / "review_state.json"
    latest = None
    if state_file.is_file():
        s = json.loads(state_file.read_text("utf-8"))
        if s.get("group_revisions"):
            latest = s["group_revisions"][-1]["units"]
    if latest is not None:
        for did, units in latest.items():
            if did in change_lookup:
                for gid in {u.get("primary_group") for u in units if
                            u.get("primary_group") and not u.get("unassigned")}:
                    groups[gid].append(change_lookup[did])
    else:
        for group in release.get("groups", []):
            for did in group["change_ids"]:
                if did in change_lookup:
                    groups[group["id"]].append(change_lookup[did])
    graphs, selected = _selected_methods(release, groups)
    # Existing unified reverse paths identify in-scope consumers, including
    # other projects. No global grep through unrelated repositories.
    unified_path = root / "unified_impact_latest.json"
    if unified_path.is_file():
        unified = json.loads(unified_path.read_text("utf-8"))
        for hit in unified.get("findings", []):
            node = hit["consumer_node"].split("|", 4)
            if len(node) != 5 or node[0] != "method":
                continue
            group, _, side, project, sha, symbol = (
                hit["group_id"], node[0], node[1], node[2], node[3], node[4])
            key = (project, side, sha)
            if symbol in graphs.get(key, {}).get("method_index", {}):
                selected[(group,) + key].add(symbol)
    repo_roots = {p: Path(path).expanduser().resolve() for p, path in repos.items()}
    cached, omissions, facts = {}, [], []
    byte_count = 0
    for (group, project, side, sha), symbols in sorted(selected.items()):
        terms = set()
        for c, ledger in groups[group]:
            terms.update(_tokens(c, side))
        terms = sorted(terms)[:MAX_SELECTED_TOKENS]
        if not terms:
            omissions.append({"group_id": group, "side": side,
                              "reason": "NO_CHANGED_IDENTIFIERS"})
            continue
        if project not in repo_roots:
            omissions.append({"group_id": group, "project": project,
                              "reason": "REPOSITORY_NOT_SUPPLIED"})
            continue
        repo = repo_roots[project]
        if check_sha(repo, sha) != sha:
            omissions.append({"group_id": group, "project": project,
                              "reason": "PINNED_SHA_NOT_AVAILABLE"})
            continue
        graph = graphs[(project, side, sha)]
        for symbol in sorted(symbols):
            method = graph["method_index"][symbol]
            path = method.get("path")
            if not isinstance(path, str) or not safe_source_path(path):
                omissions.append({"symbol": symbol, "reason": "INVALID_PATH"})
                continue
            key = (project, sha, path)
            if key not in cached:
                size = int(git(repo, "cat-file", "-s", sha + ":" + path).decode("ascii").strip())
                if size > 1_000_000 or size + byte_count > MAX_BYTES:
                    omissions.append({"path": path, "reason": "SOURCE_BUDGET"})
                    continue
                raw = git(repo, "show", sha + ":" + path)
                if b"\x00" in raw:
                    omissions.append({"path": path, "reason": "NON_TEXT"})
                    continue
                byte_count += len(raw)
                cached[key] = raw.decode("utf-8", "replace").splitlines()
            lines = cached.get(key)
            if not lines:
                continue
            start, end = method.get("start"), method.get("end")
            if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end > len(lines):
                omissions.append({"symbol": symbol, "reason": "METHOD_RANGE_MISSING"})
                continue
            for index in range(start - 1, end):
                line = lines[index]
                overlap = [term for term in terms if re.search(
                    r"(?<![A-Za-z0-9_$])" + re.escape(term) + r"(?![A-Za-z0-9_$])", line)]
                if not overlap:
                    continue
                if BRANCH.search(line):
                    kind = "DECISION_BRANCH"
                elif FIELD.search(line):
                    kind = "FIELD_DECLARATION"
                elif SQL.search(line):
                    kind = "SQL_REFERENCE"
                elif ENUM.search(line):
                    kind = "ENUM_OR_CONSTANT_USE"
                else:
                    kind = "IDENTIFIER_USE"
                facts.append({
                    "id": "CF-" + _hash([group, project, sha, symbol, index + 1, overlap])[:16],
                    "group_id": group, "project": project, "side": side,
                    "sha": sha, "symbol": symbol, "path": path,
                    "line": index + 1, "kind": kind, "identifiers": overlap,
                    "status": "LEXICAL_CANDIDATE",
                    "limitation": "Identifier co-occurrence is not verified type or data flow"})
                if len(facts) >= MAX_CONTEXTS:
                    omissions.append({"reason": "CONTEXT_LIMIT", "max": MAX_CONTEXTS})
                    break
            if len(facts) >= MAX_CONTEXTS:
                break
        if len(facts) >= MAX_CONTEXTS:
            break
    # Contract declarations often live outside Java methods (DTO fields,
    # enums and MyBatis XML/SQL). Only inspect files already in the final
    # changed-file inventory at pinned SHAs, never the entire source tree.
    declaration_pattern = re.compile(r"<(?:result|id|association|collection)\\b", re.I)
    for group, entries in sorted(groups.items()):
        if len(facts) >= MAX_CONTEXTS:
            break
        for change, ledger in entries:
            if len(facts) >= MAX_CONTEXTS:
                break
            for side, sha, path_key in (
                    ("BASE", ledger["base"], "old_path"),
                    ("HEAD", ledger["head"], "path")):
                path = change.get(path_key)
                if (not isinstance(path, str) or not safe_source_path(path) or
                        Path(path).suffix.lower() not in (".java", ".xml", ".sql")):
                    continue
                terms = _tokens(change, side)
                if not terms:
                    continue
                project = ledger["project"]
                if project not in repo_roots:
                    omissions.append({"group_id": group, "project": project,
                                      "reason": "CONTRACT_REPO_NOT_SUPPLIED"})
                    continue
                repo = repo_roots[project]
                key = (project, sha, path)
                if key not in cached:
                    if check_sha(repo, sha) != sha:
                        omissions.append({"path": path, "reason": "CONTRACT_SHA_MISMATCH"})
                        continue
                    try:
                        size = int(git(repo, "cat-file", "-s", sha + ":" + path)
                                   .decode("ascii").strip())
                        if size > 1_000_000 or size + byte_count > MAX_BYTES:
                            omissions.append({"path": path, "reason": "CONTRACT_BUDGET"})
                            continue
                        data = git(repo, "show", sha + ":" + path)
                    except RuntimeError:
                        omissions.append({"path": path, "side": side,
                                          "reason": "CONTRACT_NOT_FOUND"})
                        continue
                    if b"\\x00" in data:
                        omissions.append({"path": path, "reason": "NON_TEXT_CONTRACT"})
                        continue
                    byte_count += len(data)
                    cached[key] = data.decode("utf-8", "replace").splitlines()
                for index, line in enumerate(cached.get(key, [])):
                    overlap = [t for t in terms if re.search(
                        r"(?<![A-Za-z0-9_$])" + re.escape(t) +
                        r"(?![A-Za-z0-9_$])", line)]
                    if not overlap:
                        continue
                    if path.endswith(".java") and FIELD.search(line):
                        kind = "CONTRACT_FIELD_DECLARATION"
                    elif path.endswith(".java") and ENUM.search(line) and (
                            re.search(r"\\benum\\b", "\\n".join(cached[key][:index+1]))):
                        kind = "ENUM_DECLARATION_CANDIDATE"
                    elif path.endswith(".xml") and declaration_pattern.search(line):
                        kind = "MYBATIS_MAPPING_CANDIDATE"
                    elif path.endswith((".xml", ".sql")) and SQL.search(line):
                        kind = "SQL_FIELD_REFERENCE"
                    else:
                        continue
                    facts.append({
                        "id": "CF-" + _hash([group, project, sha, path, index + 1,
                                            kind, overlap])[:16],
                        "group_id": group, "project": project,
                        "side": side, "sha": sha,
                        "path": path, "line": index + 1, "kind": kind,
                        "identifiers": overlap, "status": "LEXICAL_CANDIDATE",
                        "limitation": "Contract declaration location is grounded in Git; "
                                      "field/schema binding is not type-checked"})
                    if len(facts) >= MAX_CONTEXTS:
                        omissions.append({"reason": "CONTEXT_LIMIT", "max": MAX_CONTEXTS})
                        break
    result = {"schema_version": "contract-context-v1",
              "facts": facts, "omissions": omissions,
              "coverage": {"facts": len(facts), "bytes_read": byte_count,
                           "truncated": len(facts) >= MAX_CONTEXTS},
              "warning": "Only changed-token matches in indexed impacted methods "
                         "are shown; fields, enums, SQL and decisions need semantic review"}
    _write_json(root / "contract_context_latest.json", result)
    return {"facts": len(facts), "omissions": len(omissions)}
