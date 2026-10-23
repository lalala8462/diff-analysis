"""Release evidence: net Git changes are the source of truth, history only context.

No project code is executed. Relations and historical attribution are candidates.
"""
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

from .gitops import git, diff_files
from .scanner import method_changes
from .refactoring_enrichment import run_refactoringminer, load_codetracker_bridge, attach_evolution
from .element_history import build_identity_chains, attach_chains
from .extract_assessment import assess_extractions
from .attribution_rules import apply_attribution_rules


HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*$", re.M)
ISSUE = re.compile(r"(?<![\w])([A-Z][A-Z0-9]{1,15}-[1-9]\d{0,8})(?![\w])")
MAX_FILE_BYTES = 2_000_000
MAX_HUNKS_PER_FILE = 1000
MAX_COMMITS = 300
SOURCE_CONTEXT = 30


def identifier(*parts):
    raw = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _decode(data):
    return data.decode("utf-8", "replace")


def _show_lines(repo, sha, path):
    if not path:
        return None, "ABSENT"
    try:
        size = int(_decode(git(repo, "cat-file", "-s", sha + ":" + path)).strip())
        if size > MAX_FILE_BYTES:
            return None, "OVERSIZE"
        data = git(repo, "show", sha + ":" + path)
        if b"\x00" in data:
            return None, "BINARY"
        return _decode(data).splitlines(), None
    except (ValueError, RuntimeError):
        return None, "UNAVAILABLE"


def _slice_context(lines, start, count, radius=SOURCE_CONTEXT):
    if lines is None:
        return []
    at = min(len(lines), max(0, start - 1))
    left = max(0, at - radius)
    right = min(len(lines), at + max(count, 1) + radius)
    return [{"line": i + 1, "text": lines[i]} for i in range(left, right)]


def _category(path):
    ext = Path(path).suffix.lower()
    if ext == ".java":
        return "JAVA"
    if ext in (".sql", ".xml"):
        return "SQL_OR_XML"
    if ext in (".yml", ".yaml", ".properties", ".json", ".toml", ".ini"):
        return "CONFIG"
    if ext in (".js", ".jsx", ".ts", ".tsx", ".vue", ".css", ".scss"):
        return "FRONTEND"
    if ext in (".md", ".txt", ".adoc"):
        return "DOCUMENT"
    return "OTHER"


def collect_commits(repo, base, head):
    """Collect ancestry/topology separately; never reconstruct net diff from commits."""
    if base == head:
        return {"status": "SAME_COMMIT", "commits": [], "total": 0}
    # If base is not an ancestor, ancestry attribution is unsafe even if git diff works.
    try:
        p = git(repo, "merge-base", "--is-ancestor", base, head)
        del p
    except RuntimeError:
        return {"status": "BASE_NOT_ANCESTOR", "commits": [], "total": None}
    raw = _decode(git(repo, "rev-list", "--topo-order", "--reverse",
                      "--parents", base + ".." + head))
    rows = raw.splitlines()
    truncated = len(rows) > MAX_COMMITS
    ids = [line.split()[0] for line in rows[:MAX_COMMITS]]
    commits = []
    for sha in ids:
        text = _decode(git(repo, "show", "-s",
                           "--format=%s%x00%b%x00%P", sha)).rstrip("\n")
        fields = text.split("\x00")
        subject = (fields[0] if fields else "")[:400]
        body = (fields[1] if len(fields) > 1 else "")[:800]
        parents = (fields[2] if len(fields) > 2 else "").split()
        commits.append({"sha": sha, "parents": parents,
                        "subject": subject, "body": body,
                        "issue_keys": sorted(set(ISSUE.findall(subject + "\n" + body)))})
    return {"status": "TRUNCATED" if truncated else "COMPLETE",
            "commits": commits, "total": len(rows)}


def _file_commit_candidates(repo, base, head, paths, commit_lookup):
    if not commit_lookup:
        return []
    try:
        raw = _decode(git(repo, "log", "--format=%H", base + ".." + head,
                          "--", *list(dict.fromkeys(paths))))
    except RuntimeError:
        return []
    return [sha for sha in dict.fromkeys(raw.splitlines()) if sha in commit_lookup][:50]


def _line_blame(repo, head, path, start, count, commits):
    """Best-effort Hunk->last-touch attribution for surviving new lines.

    Blame does not prove task identity or explain deleted lines. Never replace
    the file-level evidence when a Hunk is ambiguous or contains multiple issues.
    """
    if not start or not count or count > 120 or not path:
        return [], []
    try:
        raw = _decode(git(repo, "blame", "--line-porcelain", "-L",
                          "%d,+%d" % (start, count), head, "--", path))
    except RuntimeError:
        return [], []
    touched = []
    for line in raw.splitlines():
        match = re.match(r"^([0-9a-f]{40,64}) (\d+) (\d+) (\d+)$", line)
        if match and match.group(1) in commits and match.group(1) not in touched:
            touched.append(match.group(1))
    keys = sorted({key for sha in touched for key in commits[sha]["issue_keys"]})
    return touched, keys


def _method_overlap(methods, path, start, count):
    upper = start + max(count, 1) - 1
    return sorted(sym for sym, m in methods.items()
                  if m.get("path") == path
                  and m.get("start", 0) <= upper and m.get("end", 0) >= start)


def final_diff_ledger(repo, project, base, head, review=None):
    """Inventory every final change. Unknown analysis never drops a Git hunk."""
    files = diff_files(repo, base, head)
    timeline = collect_commits(repo, base, head)
    commits = {c["sha"]: c for c in timeline["commits"]}
    items, file_inventory, warnings = [], [], []
    old_methods = (review or {}).get("old_methods", {})
    new_methods = (review or {}).get("methods", {})
    for file in files:
        path, old_path = file["path"], file.get("old_path", file["path"])
        code_kind = _category(path)
        added = file["status"].startswith("A")
        deleted = file["status"].startswith("D")
        before, before_err = (None, "ABSENT") if added else _show_lines(repo, base, old_path)
        after, after_err = (None, "ABSENT") if deleted else _show_lines(repo, head, path)
        candidates = _file_commit_candidates(repo, base, head,
                                             [old_path, path], commits)
        all_issues = sorted(set(issue for sha in candidates
                                for issue in commits[sha]["issue_keys"]))
        # File-level candidates do not establish which commit produced a given hunk.
        file_data = {"path": path, "old_path": old_path, "status": file["status"],
                     "category": code_kind, "commit_candidates": candidates,
                     "issue_candidates": all_issues,
                     "commit_evidence": "FILE_TOUCHED_CANDIDATE",
                     "before_status": before_err, "after_status": after_err}
        file_inventory.append(file_data)
        try:
            patch = _decode(git(repo, "diff", "--no-ext-diff", "--no-color",
                                "--unified=0", "--find-renames",
                                base, head, "--", *list(dict.fromkeys([old_path, path]))))
        except RuntimeError as exc:
            patch = ""
            warnings.append({"path": path, "reason": "DIFF_FAILED", "detail": str(exc)[:300]})
        ranges = [(int(m.group(1)), int(m.group(2) or 1),
                   int(m.group(3)), int(m.group(4) or 1))
                  for m in HUNK.finditer(patch)]
        if not ranges:
            # Binary, renames without text changes, unavailable patch, submodule, etc.
            ranges = [(0, 0, 0, 0)]
        if len(ranges) > MAX_HUNKS_PER_FILE:
            warnings.append({"path": path, "reason": "HUNK_LIMIT",
                             "total": len(ranges)})
            # Keep a file-level overflow entry: do not silently lose the remaining hunks.
            ranges = ranges[:MAX_HUNKS_PER_FILE] + [(0, 0, 0, 0)]
        for index, (old_start, old_count, new_start, new_count) in enumerate(ranges):
            synthetic = not (old_start or old_count or new_start or new_count)
            symbols_old = _method_overlap(old_methods, old_path, old_start, old_count) if old_start else []
            symbols_new = _method_overlap(new_methods, path, new_start, new_count) if new_start else []
            symbols = sorted(set(symbols_old + symbols_new))
            is_binary = before_err in ("BINARY", "OVERSIZE") or after_err in ("BINARY", "OVERSIZE")
            if synthetic or is_binary:
                state = "UNSUPPORTED"
            elif symbols and code_kind == "JAVA":
                state = "SEMANTIC_MAPPED"
            elif code_kind == "DOCUMENT":
                state = "NEEDS_REVIEW"
            else:
                state = "NEEDS_REVIEW"
            line_commits, line_issues = _line_blame(
                repo, head, path, new_start, new_count, commits) if not synthetic and not deleted else ([], [])
            changes = {
                "id": "D-" + identifier(project, base, head, old_path, path, index,
                                       old_start, old_count, new_start, new_count),
                "project": project, "base_sha": base, "head_sha": head,
                "path": path, "old_path": old_path, "file_status": file["status"],
                "file_category": code_kind, "status": state,
                "old_range": {"start": old_start, "count": old_count},
                "new_range": {"start": new_start, "count": new_count},
                "symbols": symbols, "commit_candidates": candidates,
                "issue_candidates": all_issues,
                "line_commit_candidates": line_commits,
                "line_issue_candidates": line_issues,
                "provenance_status": (
                    "LINE_BLAME_CANDIDATE" if line_commits else
                    "FILE_ONLY_CANDIDATE" if candidates else "UNKNOWN"),
                "before": _slice_context(before, old_start, old_count) if old_start else [],
                "after": _slice_context(after, new_start, new_count) if new_start else [],
                "context_status": {"old": before_err, "new": after_err},
                "limitation": "UNRESOLVED_FILE_OPERATION" if synthetic else None
            }
            items.append(changes)
    return {"project": project, "base": base, "head": head,
            "final_files": file_inventory, "final_changes": items,
            "history": timeline, "warnings": warnings,
            "coverage": {"files": len(files), "hunks_or_file_events": len(items),
                         "unresolved": sum(x["status"] != "SEMANTIC_MAPPED" for x in items),
                         "claim": "Git inventory only; no claim of complete business impact"}}


def candidate_groups(ledgers):
    """Conservative grouping: a unique explicit issue key is the only automatic merge."""
    grouped = defaultdict(list)
    group_meta = defaultdict(set)
    for ledger in ledgers:
        for change in ledger["final_changes"]:
            # Prefer last-touch evidence for this final Hunk, which can
            # distinguish unrelated edits to the same file. If blame finds
            # commits without a unique issue, DO NOT fall back to a misleading
            # whole-file label.
            manual = change.get("manual_attribution")
            line_commits = change.get("line_commit_candidates", [])
            keys = (change.get("line_issue_candidates", [])
                    if line_commits else change["issue_candidates"])
            if manual:
                keys = manual["issue_keys"]
            key = ("ISSUE", keys[0]) if len(keys) == 1 else ("SINGLE", change["id"])
            grouped[key].append(change)
            group_meta[key].add(
                "HUMAN_EXACT_SHA_DIFF" if manual and len(keys) == 1
                else "MIXED_HUNK_REVIEW" if manual and len(keys) > 1
                else "LINE_BLAME_ISSUE_CANDIDATE" if key[0] == "ISSUE" and line_commits
                else "COMMIT_MESSAGE_ISSUE_CANDIDATE" if key[0] == "ISSUE"
                else "AMBIGUOUS_OR_UNASSIGNED")
    groups = []
    by_change = {}
    for key, changes in sorted(grouped.items(), key=lambda kv: str(kv[0])):
        gid = "G-" + identifier(key[0], key[1], sorted(c["id"] for c in changes))
        groups.append({
            "id": gid, "label": ("候选任务 " + key[1]) if key[0] == "ISSUE" else "未归属变化",
            "status": "CANDIDATE",
            "group_basis": (next(iter(group_meta[key])) if len(group_meta[key]) == 1
                            else "MIXED_ATTRIBUTION_EVIDENCE"),
            "evidence_bases": sorted(group_meta[key]),
            "issue_key": key[1] if key[0] == "ISSUE" else None,
            "change_ids": [c["id"] for c in changes],
            "projects": sorted(set(c["project"] for c in changes)),
            "summary": ("存在单一任务编号的归属证据（逐行提交或人工），需确认是否同一需求"
                        if key[0] == "ISSUE" else
                        "无法唯一归属，保留单独候选；一个 Hunk 可能包含多个任务")
        })
        for change in changes:
            by_change[change["id"]] = gid
    return groups, by_change


def group_intersections(ledgers, groups, by_change, reviews=None, cross_candidates=None):
    """Keep within-group membership distinct from weak cross-group review relations."""
    edges = {}
    changes = [c for ledger in ledgers for c in ledger["final_changes"]]
    by_path = defaultdict(list)
    by_symbol = defaultdict(list)
    for c in changes:
        gid = by_change[c["id"]]
        by_path[(c["project"], c["path"])].append((gid, c))
        for s in c["symbols"]:
            by_symbol[(c["project"], s)].append((gid, c))
    def add(left, right, kind, evidence):
        if left == right:
            return
        a, b = sorted([left, right])
        key = (a, b, kind)
        if key not in edges:
            edges[key] = {"id": "X-" + identifier(a, b, kind), "from_group": a,
                          "to_group": b, "kind": kind, "status": "CHECK_TOGETHER",
                          "evidence": [], "limitation": "Relation is not proof of a defect"}
        if evidence not in edges[key]["evidence"] and len(edges[key]["evidence"]) < 25:
            edges[key]["evidence"].append(evidence)
    def pair_group_representatives(entries, kind, evidence_key):
        # Group first: prevents O(hunks^2) when a common file contains many hunks.
        representatives = {}
        for group_id, change in entries:
            representatives.setdefault(group_id, change)
        ids = sorted(representatives)
        if len(ids) > 80:
            # Record explicit incompleteness; never pretend all group pairs were checked.
            return {"type": "ANALYSIS_TRUNCATED", "groups": len(ids),
                    "relation": kind, "source": evidence_key}
        for i, ga in enumerate(ids):
            for gb in ids[i+1:]:
                ca, cb = representatives[ga], representatives[gb]
                add(ga, gb, kind,
                    {"source": evidence_key,
                     "change_ids": [ca["id"], cb["id"]]})
        return None

    truncations = []
    for (project, path), entries in by_path.items():
        warning = pair_group_representatives(
            entries, "SHARED_FILE_WEAK", {"project": project, "path": path})
        if warning:
            truncations.append(warning)
    for (project, sym), entries in by_symbol.items():
        warning = pair_group_representatives(
            entries, "SHARED_SYMBOL_CANDIDATE",
            {"project": project, "symbol": sym})
        if warning:
            truncations.append(warning)

    # Version-specific call paths only: never join V1 and V2 into a fictitious chain.
    for review in (reviews or []):
        project = review["project"]
        for version, field in (("BASE", "old_impact_paths"),
                               ("HEAD", "new_impact_paths")):
            for _, path in review.get(field, {}).items():
                touched = {}
                for symbol in path:
                    for group_id, change in by_symbol.get((project, symbol), []):
                        touched.setdefault(group_id, change)
                ids = sorted(touched)
                if len(ids) > 80:
                    truncations.append({"type": "ANALYSIS_TRUNCATED",
                                        "groups": len(ids), "relation": field,
                                        "source": project})
                    continue
                for i, ga in enumerate(ids):
                    for gb in ids[i+1:]:
                        add(ga, gb, "CALL_PATH_" + version + "_CANDIDATE",
                            {"project": project, "change_ids":
                                [touched[ga]["id"], touched[gb]["id"]],
                             "version": version,
                             "note": "Static resolved calls; business impact unverified"})
    # P0 cross-project method-name matches are weak candidates, never confirmed RPCs.
    for candidate in (cross_candidates or []):
        left = by_symbol.get((candidate.get("callee_project"),
                              candidate.get("callee")), [])
        right = by_symbol.get((candidate.get("caller_project"),
                               candidate.get("caller")), [])
        for ga, ca in left[:20]:
            for gb, cb in right[:20]:
                add(ga, gb, "CROSS_PROJECT_CALL_CANDIDATE",
                    {"callee": candidate.get("callee"),
                     "caller": candidate.get("caller"),
                     "callee_project": candidate.get("callee_project"),
                     "caller_project": candidate.get("caller_project"),
                     "change_ids": [ca["id"], cb["id"]],
                     "note": "P0 name/arity match; not confirmed cross-service call"})
    for warning in truncations:
        key = ("TRUNCATED", str(warning))
        edges[key] = {"id": "X-" + identifier(key), "from_group": "",
                      "to_group": "", "kind": "ANALYSIS_TRUNCATED",
                      "status": "UNKNOWN", "evidence": [warning],
                      "limitation": "Group comparison budget exhausted"}
    return sorted(edges.values(), key=lambda x: x["id"])



def consumer_candidates(ledgers, by_change, cross_candidates):
    """Retain potentially affected unchanged consumers not represented by another group."""
    by_symbol = defaultdict(list)
    for l in ledgers:
        for c in l["final_changes"]:
            for symbol in c["symbols"]:
                by_symbol[(c["project"], symbol)].append(c)
    findings = []
    seen = set()
    for candidate in (cross_candidates or []):
        matching = by_symbol.get((candidate.get("callee_project"),
                                  candidate.get("callee")), [])
        for c in matching:
            key = (c["id"], candidate.get("caller_project"), candidate.get("caller"))
            if key in seen:
                continue
            seen.add(key)
            findings.append({
                "group_id": by_change[c["id"]],
                "change_id": c["id"],
                "consumer_project": candidate.get("caller_project"),
                "consumer_symbol": candidate.get("caller"),
                "status": "CANDIDATE",
                "source": "P0_NAME_ARITY_MATCH",
                "evidence": {"path": candidate.get("path"), "line": candidate.get("line")},
                "limitation": "Unmodified consumer or no group mapping; confirm actual type/RPC"})
    return findings

def attach_review_to_ledger(ledger, review):
    """Enrich a persisted Git inventory without changing Diff identities."""
    old_methods = review.get("old_methods", {})
    new_methods = review.get("methods", {})
    for change in ledger["final_changes"]:
        if change["file_category"] != "JAVA" or change["status"] == "UNSUPPORTED":
            continue
        old_range, new_range = change["old_range"], change["new_range"]
        old_symbols = (_method_overlap(old_methods, change["old_path"],
                                      old_range["start"], old_range["count"])
                       if old_range["start"] else [])
        new_symbols = (_method_overlap(new_methods, change["path"],
                                      new_range["start"], new_range["count"])
                       if new_range["start"] else [])
        change["symbols"] = sorted(set(old_symbols + new_symbols))
        change["status"] = ("SEMANTIC_MAPPED" if change["symbols"] else "NEEDS_REVIEW")
    ledger["coverage"]["unresolved"] = sum(
        c["status"] != "SEMANTIC_MAPPED" for c in ledger["final_changes"])


def assemble_release(specs, reviews=None, cross_candidates=None,
                     refactoringminer_bin=None, codetracker_evidence=None,
                     attribution_rules=None, precomputed_ledgers=None):
    """The optional tools only enrich evidence; net Diff always remains intact."""
    ledgers = []
    pinned_ledgers = {(l["project"], l["base"], l["head"]): l
                      for l in (precomputed_ledgers or [])}
    for repo, project, base, head in specs:
        review = next((r for r in (reviews or [])
                       if (r["project"], r["base"], r["head"]) ==
                       (project, base, head)), None)
        if precomputed_ledgers is None:
            ledger = final_diff_ledger(repo, project, base, head, review)
        else:
            ledger = pinned_ledgers[(project, base, head)]
            if review is not None:
                attach_review_to_ledger(ledger, review)
        ref_events, ref_warnings = run_refactoringminer(
            repo, base, head, refactoringminer_bin) if refactoringminer_bin else ([], [])
        tracker_events, tracker_warnings = load_codetracker_bridge(
            codetracker_evidence, project, base, head)
        extract_warnings = assess_extractions(repo, ref_events)
        chain_records, chain_warnings = build_identity_chains(
            repo, base, head, ref_events) if ref_events else ([], [])
        attach_evolution(ledger, ref_events + tracker_events)
        attach_chains(ledger, chain_records)
        attribution_warnings = apply_attribution_rules(ledger, attribution_rules)
        ledger["enrichment"] = {
            "refactoringminer_status": (
                "NOT_CONFIGURED" if not refactoringminer_bin else
                "PARTIAL" if ref_warnings else "COMPLETED"),
            "codetracker_status": (
                "NOT_CONFIGURED" if not codetracker_evidence else
                "PARTIAL" if tracker_warnings else "IMPORTED"),
            "warnings": (ref_warnings + tracker_warnings + extract_warnings
                         + chain_warnings + attribution_warnings),
            "element_chain_count": len(chain_records),
            "mixed_behavior_candidates": sum(
                1 for ev in ref_events
                if (ev.get("behavior_assessment") or {}).get("status")
                    == "POSSIBLE_MIXED_BEHAVIOR")
        }
        ledgers.append(ledger)
    groups, by_change = candidate_groups(ledgers)
    intersections = group_intersections(ledgers, groups, by_change, reviews, cross_candidates)
    existing_links = {(x.get("from_group"), x.get("to_group"), x.get("kind"))
                      for x in intersections}
    for ledger in ledgers:
        for chain in ledger.get("element_chains", []):
            gs = sorted({by_change[cid] for cid in
                         chain["old_change_ids"] + chain["new_change_ids"]
                         if cid in by_change})
            if len(gs) > 20:
                ledger["enrichment"]["warnings"].append({
                    "type": "ELEMENT_CHAIN_GROUP_LIMIT"})
                continue
            for i, ga in enumerate(gs):
                for gb in gs[i + 1:]:
                    key = (ga, gb, "ELEMENT_IDENTITY_CHAIN_CANDIDATE")
                    if key not in existing_links:
                        existing_links.add(key)
                        intersections.append({
                            "id": "X-" + identifier(*key), "from_group": ga,
                            "to_group": gb, "kind": key[2], "status": "CHECK_TOGETHER",
                            "evidence": [{"chain_id": chain["id"],
                                          "status": chain["status"]}],
                            "limitation": "Element identity candidate is not behavior equivalence"
                        })
        for ref in ledger.get("evolution_relations", []):
            gs = sorted({by_change[cid] for cid in
                         ref["old_change_ids"] + ref["new_change_ids"]
                         if cid in by_change})
            if len(gs) > 20:
                ledger["enrichment"]["warnings"].append({
                    "type": "EVOLUTION_GROUPS_TRUNCATED", "count": len(gs)})
                continue
            for i, ga in enumerate(gs):
                for gb in gs[i+1:]:
                    key = (ga, gb, "REFACTORING_EVOLUTION_CANDIDATE")
                    if key in existing_links:
                        continue
                    existing_links.add(key)
                    intersections.append({
                        "id": "X-" + identifier(*key), "from_group": ga,
                        "to_group": gb, "kind": key[2],
                        "status": "CHECK_TOGETHER", "evidence": [{
                            "refactoring_id": ref["id"], "type": ref["kind"],
                            "source": ref["source"],
                            "confidence": ref["confidence"]}],
                        "limitation": "Refactoring evidence does not prove shared requirement or behavior equivalence"
                    })
    intersections.sort(key=lambda item: item["id"])
    consumers = consumer_candidates(ledgers, by_change, cross_candidates)
    names = [l["project"] for l in ledgers]
    duplicate_warnings = (
        ["Same project appears in several comparisons: analyses are independent; "
         "no merged net release tree was constructed"]
        if len(names) != len(set(names)) else [])
    return {
        "schema_version": "release-evidence-v1",
        "status": "STATIC_CANDIDATES_ONLY",
        "project_scope": names,
        "ledgers": ledgers, "groups": groups,
        "cross_group_checks": intersections,
        "consumer_candidates": consumers,
        "warnings": duplicate_warnings + [
            "File-level commit attribution is only a candidate",
            "Group membership is not confirmed requirement identity",
            "Cross-group relations require human/code-level verification"]
    }
