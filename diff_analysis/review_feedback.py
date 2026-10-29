"""Local group feedback and non-overwriting evidence-basis checks.

ReviewUnit is an overlay on RawHunk; original D-* and group facts stay immutable.
A split of added lines cannot infer exact old-line ownership without semantics.
"""
import json
from pathlib import Path

from .review_workflow import (_hash, _utc, _state, _write_json, load_batch,
                              _load_supplement_state)


def _units(release):
    by_diff = {}
    owner = {}
    for group in release["groups"]:
        for did in group["change_ids"]:
            owner[did] = group["id"]
    for ledger in release["ledgers"]:
        for change in ledger["final_changes"]:
            did = change["id"]
            uid = "U-" + _hash([did, "whole"])[:16]
            by_diff[did] = [{
                "id": uid, "raw_change_id": did, "new_range": change["new_range"],
                "old_range": change["old_range"], "primary_group": owner.get(did),
                "range_basis": "WHOLE_RAW_CHANGE",
                "unassigned": False,
                "revision": 1}]
    return by_diff


def apply_group_feedback(run_dir, payload):
    root, release = load_batch(run_dir)
    if (payload.get("schema_version") != "group-feedback-v1" or
            not isinstance(payload.get("actions"), list) or
            not payload["actions"]):
        raise ValueError("Expected nonempty group-feedback-v1")
    state = _state(root)
    latest = state["group_revisions"][-1] if state["group_revisions"] else None
    by_diff = latest["units"] if latest else _units(release)
    # Deep copy: a failed validation cannot modify the previously stored revision.
    by_diff = json.loads(json.dumps(by_diff))
    known_groups = {g["id"] for g in release["groups"]}
    created_groups = list(latest.get("created_groups", [])) if latest else []
    known_groups.update(g["id"] for g in created_groups)
    known_groups.update(u["primary_group"] for units in by_diff.values()
                        for u in units if u["primary_group"])
    changes = {c["id"]: c for l in release["ledgers"]
               for c in l["final_changes"]}
    touched_diff, touched_groups = set(), set()
    actions = []
    for item in payload["actions"]:
        if not isinstance(item, dict) or item.get("action") not in (
                "SPLIT_HUNK", "MOVE_UNIT", "MERGE_GROUPS", "SPLIT_GROUP"):
            raise ValueError("Unsupported group-feedback action")
        if (not isinstance(item.get("reason"), str) or
                len(item["reason"].strip()) < 5 or
                not isinstance(item.get("actor"), str) or
                not item["actor"].strip()):
            raise ValueError("Feedback requires actor and meaningful reason")
        action = item["action"]
        if action == "SPLIT_HUNK":
            did = item.get("diff_id")
            change = changes.get(did)
            if change is None or not isinstance(item.get("segments"), list):
                raise ValueError("Unknown Hunk or missing segments")
            r = change["new_range"]
            if r["start"] < 1 or r["count"] < 1:
                raise ValueError("Cannot split a pure-deletion or file event by new lines")
            if len(item["segments"]) > 100 or not item["segments"]:
                raise ValueError("Hunk split limited to 1..100 segments")
            # Only split a whole Hunk once per revision history; avoid
            # accidentally discarding earlier human units.
            if len(by_diff[did]) != 1 or by_diff[did][0]["range_basis"] != "WHOLE_RAW_CHANGE":
                raise ValueError("Hunk already split; revise units rather than re-splitting")
            start, end = r["start"], r["start"] + r["count"] - 1
            spans = []
            for part in item["segments"]:
                if (not isinstance(part, dict) or
                        not isinstance(part.get("start"), int) or
                        isinstance(part["start"], bool) or
                        not isinstance(part.get("count"), int) or
                        isinstance(part["count"], bool) or
                        part["count"] <= 0):
                    raise ValueError("Invalid positive segment range")
                left, right = part["start"], part["start"] + part["count"] - 1
                if left < start or right > end:
                    raise ValueError("Segment outside original new-line range")
                gid = part.get("group_id")
                if gid not in known_groups:
                    raise ValueError("Segment group ID must already exist")
                spans.append((left, right, gid))
            spans.sort()
            if any(spans[i][0] <= spans[i - 1][1]
                   for i in range(1, len(spans))):
                raise ValueError("Overlapping segments cannot be separately owned")
            original_group = by_diff[did][0]["primary_group"]
            touched_groups.add(original_group)
            result, cursor = [], start
            for left, right, gid in spans:
                if cursor < left:
                    result.append(_segment(did, cursor, left - cursor, None, True))
                result.append(_segment(did, left, right - left + 1, gid, False))
                cursor = right + 1
                touched_groups.add(gid)
            if cursor <= end:
                result.append(_segment(did, cursor, end - cursor + 1, None, True))
            by_diff[did] = result
            touched_diff.add(did)
        elif action == "SPLIT_GROUP":
            source = item.get("source_group_id")
            selected = item.get("diff_ids")
            label = item.get("new_group_label")
            if (source not in known_groups or
                    not isinstance(label, str) or len(label.strip()) < 3 or
                    not isinstance(selected, list) or not selected or
                    len(set(selected)) != len(selected) or
                    not all(did in by_diff for did in selected)):
                raise ValueError("SPLIT_GROUP needs existing source, change IDs and label")
            if not all(any(u["primary_group"] == source for u in by_diff[did])
                       for did in selected):
                raise ValueError("SPLIT_GROUP must move units owned by source group")
            gid = "G-MAN-" + _hash([source, sorted(selected), label.strip()])[:12]
            if gid in known_groups:
                raise ValueError("Duplicate manual group")
            known_groups.add(gid)
            created_groups.append({"id": gid, "label": label.strip(),
                                   "source_group": source,
                                   "created_at_utc": _utc()})
            for did in selected:
                for unit in by_diff[did]:
                    if unit["primary_group"] == source:
                        unit["primary_group"] = gid
                        unit["revision"] += 1
                        touched_diff.add(did)
            touched_groups.update([source, gid])
        elif action == "MOVE_UNIT":
            uid, gid = item.get("unit_id"), item.get("target_group_id")
            if gid not in known_groups:
                raise ValueError("Unknown target group")
            matches = [(did, u) for did, units in by_diff.items()
                       for u in units if u["id"] == uid]
            if len(matches) != 1:
                raise ValueError("Unknown/ambiguous review unit")
            did, unit = matches[0]
            touched_groups.update([unit["primary_group"], gid])
            unit["primary_group"] = gid
            unit["unassigned"] = False
            unit["revision"] += 1
            touched_diff.add(did)
        else:
            source = item.get("source_group_ids")
            target = item.get("target_group_id")
            if (not isinstance(source, list) or not source or
                    len(source) != len(set(source)) or
                    not set(source).issubset(known_groups) or
                    target not in known_groups):
                raise ValueError("Invalid merge groups")
            for did, units in by_diff.items():
                for unit in units:
                    if unit["primary_group"] in source:
                        touched_groups.update([unit["primary_group"], target])
                        unit["primary_group"] = target
                        unit["revision"] += 1
                        touched_diff.add(did)
        actions.append({**item, "recorded_at_utc": _utc()})
    revision = len(state["group_revisions"]) + 1
    old_questions = json.loads((root / "questions.json").read_text("utf-8"))
    affected_qids = sorted(q["id"] for q in old_questions["questions"]
                           if set(q["group_ids"]) & touched_groups or
                           set(q["change_ids"]) & touched_diff)
    invalidated = sorted(d["id"] for d in state["decisions"]
                         if ((d["object_type"] == "GROUP" and
                              d["object_id"] in touched_groups) or
                             (d["object_type"] == "DIFF" and
                              d["object_id"] in touched_diff) or
                             (d["object_type"] == "QUESTION" and
                              d["object_id"] in affected_qids)))
    rev = {
        "id": "GRV-" + _hash([revision, actions, _utc()])[:16],
        "revision": revision, "created_at_utc": _utc(),
        "actions": actions, "units": by_diff,
        "created_groups": created_groups,
        "affected_change_ids": sorted(touched_diff),
        "affected_group_ids": sorted(x for x in touched_groups if x),
        "affected_question_ids": affected_qids,
        "decisions_needing_review": invalidated,
        "limitation": "Overlay only; direct/code graph facts not altered. "
                      "Old line subranges cannot be precisely attributed from new-line splits."
    }
    state["group_revisions"].append(rev)
    destination = root / "revisions"
    destination.mkdir(exist_ok=True)
    _write_json(destination / ("groups-%03d.json" % revision), rev)
    _write_json(root / "review_state.json", state)
    # Derived analysis is isolated: even an unavailable method graph cannot
    # roll back a valid, auditable grouping decision.
    try:
        from .incremental_review import recompute_review
        recomputed = recompute_review(root)
        recompute_error = None
    except (ValueError, KeyError, OSError, TypeError) as exc:
        recomputed = None
        recompute_error = type(exc).__name__ + ": " + str(exc)[:300]
    return {"recomputed": recomputed,
            "recompute_error": recompute_error,
            "revision": revision, "units": sum(len(x) for x in by_diff.values()),
            "created_groups": created_groups,
            "affected_questions": affected_qids,
            "decisions_needing_review": invalidated}


def _segment(diff_id, start, count, group_id, unassigned):
    return {
        "id": "U-" + _hash([diff_id, start, count])[:16],
        "raw_change_id": diff_id, "new_range": {"start": start, "count": count},
        "old_range": None, "primary_group": group_id,
        "range_basis": "NEW_LINE_SEGMENT_ONLY",
        "old_range_status": "NOT_INFERRED",
        "unassigned": unassigned, "revision": 1}


def import_basis_checks(run_dir, payload):
    root, release = load_batch(run_dir)
    if (payload.get("schema_version") != "basis-checks-v1" or
            not isinstance(payload.get("checks"), list)):
        raise ValueError("Expected basis-checks-v1")
    state = _state(root)
    known = {d["id"] for d in state["decisions"]}
    valid_evidence = {c["id"] for l in release["ledgers"]
                      for c in l["final_changes"]}
    valid_evidence.update(x["id"] for x in _load_supplement_state(root)["evidence"])
    records = []
    for item in payload["checks"]:
        if (not isinstance(item, dict) or item.get("decision_id") not in known or
                item.get("status") not in ("SUPPORTED", "NOT_SUPPORTED", "UNCERTAIN") or
                item.get("actor_kind") not in ("HUMAN", "AI") or
                not isinstance(item.get("actor_id"), str) or not item["actor_id"] or
                not isinstance(item.get("reason"), str) or
                len(item["reason"].strip()) < 5 or
                not isinstance(item.get("evidence_ids"), list) or
                not set(item["evidence_ids"]).issubset(valid_evidence)):
            raise ValueError("Invalid evidence-basis check or out-of-scope reference")
        records.append({
            **item, "id": "CHK-" + _hash([item, _utc()])[:16],
            "created_at_utc": _utc(),
            "limitation": "Human/AI basis review is not a proof of business correctness"})
    state["basis_checks"].extend(records)
    _write_json(root / "review_state.json", state)
    return {"checks_added": len(records)}


def review_status(run_dir):
    root, release = load_batch(run_dir)
    from .revision_pipeline import update_review_package
    update_review_package(root)
    state = _state(root)
    latest = {}
    affected = set()
    review_package = root / "review_package_latest.json"
    if review_package.is_file():
        package = json.loads(review_package.read_text(encoding="utf-8"))
        affected.update(package.get("decisions_to_revalidate", []))
    for d in state["decisions"]:
        if d["id"] in affected:
            # Applicability is derived, not overwritten in original journal.
            status = "NEEDS_REVIEW"
        else:
            status = "CURRENT"
        key = (d["object_type"], d["object_id"], d["dimension"], d["actor_kind"])
        latest[key] = {**d, "current_applicability": status}
    by_dimension = {}
    for key, record in latest.items():
        k = key[:3]
        by_dimension.setdefault(k, {})[key[3]] = record
    divergent = []
    for key, actors in by_dimension.items():
        a, h = actors.get("AI"), actors.get("HUMAN")
        if (a and h and a["verdict"] != h["verdict"] and
                a["current_applicability"] == "CURRENT" and
                h["current_applicability"] == "CURRENT"):
            divergent.append({"object_type": key[0], "object_id": key[1],
                              "dimension": key[2], "ai": a["verdict"],
                              "human": h["verdict"]})
    result = {"schema_version": "review-status-v1",
              'coverage': json.loads((root / 'coverage_latest.json').read_text('utf-8')),
              'report_snapshot_notice': 'HTML 是生成时快照，最新覆盖及适用性以本状态为准',
              "decisions_total": len(state["decisions"]),
              "basis_checks_total": len(state["basis_checks"]),
              "current_latest": list(latest.values()),
              "disagreements": divergent,
              "unreviewed_count": max(0, len(release.get("groups", [])) -
                  len({k[1] for k in latest if k[0] == "GROUP"})),
              "warning": "Structural validation and independent basis review "
                         "do not prove business correctness"}
    _write_json(root / "review_status.json", result)
    return result
