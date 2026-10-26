"""Recompute review questions and evidence requirements by revision dependencies.

Never mutate Git ledgers or previously exported supplements. Evidence already
exported can be reused as bytes; its adequacy for a revised claim is UNKNOWN.
"""
import json
from pathlib import Path
from collections import defaultdict
from .review_workflow import load_batch, _state, _hash, _write_json


def _group_for_diff(release, state):
    owner = defaultdict(set)
    if state["group_revisions"]:
        for did, units in state["group_revisions"][-1]["units"].items():
            for unit in units:
                if unit.get("primary_group") and not unit.get("unassigned"):
                    owner[did].add(unit["primary_group"])
    else:
        for g in release.get("groups", []):
            for did in g["change_ids"]:
                owner[did].add(g["id"])
    return owner


def update_review_package(run_dir):
    root, release = load_batch(run_dir)
    state = _state(root)
    original = json.loads((root / "questions.json").read_text("utf-8"))
    source = root / "recomputed_latest.json"
    questions = (json.loads(source.read_text("utf-8"))["questions"]
                 if source.is_file() and state["group_revisions"] else original["questions"])
    unified_file = root / "unified_impact_latest.json"
    unified = (json.loads(unified_file.read_text("utf-8"))
               if unified_file.is_file() else {})
    questions = list(questions) + unified.get("questions", [])
    group_impact = defaultdict(list)
    for hit in unified.get("findings", []):
        group_impact[hit["group_id"]].append(
            [hit["source_node"], hit["consumer_node"],
             hit["confidence"], hit["path_edge_ids"]])
    diff_owner = _group_for_diff(release, state)
    changes = {c["id"]: (l, c) for l in release["ledgers"]
               for c in l["final_changes"]}
    superseded = {e["old_goal_id"] for e in state.get("goal_revision_events", [])}
    # Bind target changes to the exact review objects that cited them. An
    # unlinked new goal cannot invalidate every unrelated group.
    goal_affected = set()
    for decision in state["decisions"]:
        if decision.get("expected_goal_id") not in superseded:
            continue
        if decision["object_type"] == "GROUP":
            goal_affected.add(decision["object_id"])
        elif decision["object_type"] == "DIFF":
            goal_affected.update(diff_owner.get(decision["object_id"], []))
        elif decision["object_type"] == "QUESTION":
            for q in original["questions"]:
                if q["id"] == decision["object_id"]:
                    goal_affected.update(q.get("group_ids", []))
    group_affected = (set(state["group_revisions"][-1]["affected_group_ids"])
                      if state["group_revisions"] else set())
    seen, dedup = set(), []
    for question in questions:
        if question["id"] not in seen:
            seen.add(question["id"])
            dedup.append(question)
    supplement_file = root / "supplement_state.json"
    supplements = (json.loads(supplement_file.read_text("utf-8")).get("evidence", [])
                   if supplement_file.is_file() else [])
    prev_file = root / "review_package_latest.json"
    previous = (json.loads(prev_file.read_text("utf-8"))
                if prev_file.is_file() else {})
    old = {q["id"]: q for q in previous.get("questions", [])}
    group_units = defaultdict(list)
    if state["group_revisions"]:
        for did, units in state["group_revisions"][-1]["units"].items():
            for unit in units:
                gid = unit.get("primary_group")
                if gid and not unit.get("unassigned"):
                    group_units[gid].append([
                        did, unit["id"], unit.get("revision"),
                        unit.get("old_range"), unit.get("new_range")])
    groups = defaultdict(list)
    for cid, owners in diff_owner.items():
        for gid in owners:
            groups[gid].append(cid)
    request_plan, updated, reused, decision_stale = [], 0, 0, []
    enriched = []
    for q in dedup:
        related = q.get("group_ids", [])
        goal_refs = sorted((e["old_goal_id"], e["new_goal_id"]) for e in
                           state.get("goal_revision_events", [])
                           if set(related) & goal_affected)
        membership = sorted((g, tuple(sorted(groups.get(g, []))),
                             sorted(group_units.get(g, []),
                                    key=lambda entry: (entry[0], entry[1])))
                            for g in related)
        fingerprint = _hash([q["id"], q.get("change_ids"), related, membership,
                             goal_refs, q.get("path_edge_ids", []),
                             sorted((g, group_impact[g]) for g in related)])
        last = old.get(q["id"])
        unchanged = bool(last and last.get("dependency_fingerprint") == fingerprint)
        if unchanged:
            reused += 1
        else:
            updated += 1
        q = {**q, "dependency_fingerprint": fingerprint,
             "revision_status": "REUSED" if unchanged else "REVISED",
             "evidence_adequacy": "NOT_ASSESSED"}
        enriched.append(q)
        for cid in q.get("change_ids", []) + [cid for g in related for cid in groups.get(g, [])]:
            if cid not in changes:
                continue
            ledger, change = changes[cid]
            for side, sha, symbol_key, path_key in (
                    ("BASE", ledger["base"], "symbols_old", "old_path"),
                    ("HEAD", ledger["head"], "symbols_new", "path")):
                for symbol in change.get(symbol_key, []):
                    request_plan.append({"question_id": q["id"],
                                         "kind": "METHOD", "project": ledger["project"],
                                         "side": side, "sha": sha, "symbol": symbol,
                                         "status": "NEEDS_EVIDENCE_OR_REVIEW"})
                p = change.get(path_key)
                if isinstance(p, str) and p.endswith((
                        "Api.java", "DTO.java", "Request.java", "Response.java",
                        ".sql", ".xml")):
                    request_plan.append({"question_id": q["id"],
                                         "kind": "CONTRACT", "project": ledger["project"],
                                         "side": side, "sha": sha, "path": p,
                                         "status": "NEEDS_EVIDENCE_OR_REVIEW"})
        node = q.get("consumer_node")
        if isinstance(node, str) and node.startswith("method|"):
            parts = node.split("|", 4)
            if len(parts) == 5:
                request_plan.append({"question_id": q["id"], "kind": "METHOD",
                                     "project": parts[2], "side": parts[1],
                                     "sha": parts[3], "symbol": parts[4],
                                     "status": "NEEDS_EVIDENCE_OR_REVIEW"})
    seen_req, filtered = set(), []
    for item in request_plan:
        key = _hash(item)
        if key not in seen_req:
            filtered.append(item)
            seen_req.add(key)
    for decision in state["decisions"]:
        if (decision["object_type"] == "QUESTION" and
                decision["object_id"] in {q["id"] for q in enriched
                                         if q["revision_status"] == "REVISED"}):
            decision_stale.append(decision["id"])
        if decision.get("expected_goal_id") in superseded:
            decision_stale.append(decision["id"])
    result = {
        "schema_version": "review-package-v1",
        "group_revision": (state["group_revisions"][-1]["revision"]
                           if state["group_revisions"] else None),
        "goal_revision_count": len(state.get("goal_revision_events", [])),
        "questions": enriched, "evidence_plan": filtered[:200],
        "plan_omissions": max(0, len(filtered) - 200),
        "available_evidence_ids": [x["id"] for x in supplements],
        "questions_reused": reused, "questions_recomputed": updated,
        "decisions_to_revalidate": sorted(set(decision_stale)),
        "warning": "Previously exported bytes remain available, but may not "
                   "support new questions or superseding expected goals"}
    folder = root / "revisions"
    folder.mkdir(exist_ok=True)
    snapshot = _hash([result["questions"], result["evidence_plan"],
                      result["decisions_to_revalidate"]])[:12]
    version = "review-%03d-%03d-%s.json" % (
        result["group_revision"] or 0, result["goal_revision_count"], snapshot)
    _write_json(folder / version, result)
    _write_json(root / "review_package_latest.json", result)
    return {"questions": len(enriched), "questions_reused": reused,
            "questions_recomputed": updated,
            "decisions_to_revalidate": len(result["decisions_to_revalidate"]),
            "planned_evidence": len(result["evidence_plan"])}
