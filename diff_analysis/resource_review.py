"""Resource impact normalization and revision-scoped review overlay.

Resource facts come from pinned-SHA evidence and never change when a user
moves/splits a ReviewUnit. Only source attribution and dependent questions
are recomputed. Splits without a matching HEAD source range stay unassigned.
"""
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from .review_workflow import _state, _write_json, _question


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def normalized_candidates(dependency_scope):
    """Combine SDK, table and shared-name impacts under one durable identity."""
    result = []
    for row in dependency_scope.get("contract_impacts", []):
        sdk = row["sdk"]
        by_contract = defaultdict(list)
        for use in row["uses"]:
            contract = use["contract"]
            key = (contract["path"], contract["identity"])
            by_contract[key].append(use)
        for (path, member), uses in sorted(by_contract.items()):
            contract = uses[0]["contract"]
            locations = []
            for side in ("old", "new"):
                observation = contract.get(side)
                if observation:
                    locations.append({"path": path, "start": observation["line"],
                                      "end": observation["line"],
                                      "side": "base" if side == "old" else "head"})
            result.append({
                "kind": "SDK_CONTRACT", "resource": contract["type"] + "#" + member,
                "producer_project": sdk["project"],
                "consumer_project": row["consumer"],
                "producer_base_sha": sdk.get("base_sha"),
                "producer_head_sha": sdk.get("head_sha"),
                "consumer_sha": row["consumer_head_sha"],
                "change_ids": sorted(set(contract.get("change_ids", []))),
                "source_uses": locations,
                "consumer_uses": [
                    {"path": u["consumer_path"], "start": u["line"],
                     "end": u["line"], "sha": u["consumer_sha"],
                     "status": u.get("binding_status")}
                    for u in uses],
                "status": "TYPE_SCOPED_LEXICAL_UNCONFIRMED",
                "dependency_version": row.get("dependency_version")})
    for row in dependency_scope.get("database", {}).get("impacts", []):
        result.append({
            "kind": "DATABASE_TABLE", "resource": row["table"],
            "producer_project": row["producer_project"],
            "consumer_project": row["consumer_project"],
            "producer_base_sha": row["producer_base_sha"],
            "producer_head_sha": row["producer_head_sha"],
            "consumer_sha": row["consumer_sha"],
            "change_ids": row["change_ids"],
            "source_uses": row["producer_uses"],
            "consumer_uses": row["consumer_uses"],
            "status": row["status"]})
    for row in dependency_scope.get("shared_resources", {}).get("impacts", []):
        result.append({
            "kind": row["kind"], "resource": row["resource"],
            "producer_project": row["producer_project"],
            "consumer_project": row["consumer_project"],
            "producer_base_sha": row["base_sha"],
            "producer_head_sha": row["head_sha"],
            "consumer_sha": row["consumer_sha"],
            "change_ids": row["change_ids"],
            "source_uses": row["source_uses"],
            "consumer_uses": row["consumer_uses"],
            "status": row["status"]})
    for item in result:
        item["id"] = "R-" + _hash([
            item["kind"], item["producer_project"], item["consumer_project"],
            item["resource"], item["producer_base_sha"],
            item["producer_head_sha"], item["consumer_sha"]])[:20]
    return {"schema_version": "resource-impact-index-v1",
            "candidates": sorted(result, key=lambda x: x["id"]),
            "coverage_gaps": (dependency_scope.get("database", {}).get("omissions", []) +
                              dependency_scope.get("shared_resources", {}).get("omissions", [])),
            "limitation": "Static project/resource/name candidates, never verified runtime bindings"}


def _intersect(a, b):
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    lo, count = a.get("start"), a.get("count")
    begin, end = b.get("start"), b.get("end")
    return (isinstance(lo, int) and isinstance(count, int) and count > 0
            and isinstance(begin, int) and isinstance(end, int)
            and lo <= end and lo + count - 1 >= begin)


def _owners(release, units=None):
    if units is not None:
        return units
    initial = {}
    by_diff = {cid: g["id"] for g in release.get("groups", [])
               for cid in g.get("change_ids", [])}
    for ledger in release["ledgers"]:
        for change in ledger["final_changes"]:
            cid = change["id"]
            initial[cid] = [{"id": "U-" + _hash([cid, "whole"])[:16],
                             "raw_change_id": cid, "range_basis": "WHOLE_RAW_CHANGE",
                             "primary_group": by_diff.get(cid),
                             "old_range": change.get("old_range"),
                             "new_range": change.get("new_range"),
                             "unassigned": cid not in by_diff}]
    return initial


def refresh_resource_review(run_dir, release, units=None, affected=None):
    """Reattribute immutable resource candidates after manual group changes."""
    root = Path(run_dir)
    filename = root / "resource_impact_latest.json"
    if not filename.is_file():
        return {"available": False, "reason": "RESOURCE_FACTS_NOT_YET_EXPORTED"}
    raw = json.loads(filename.read_text("utf-8"))
    if raw.get("schema_version") != "resource-impact-index-v1":
        raise ValueError("Unsupported resource impact version")
    state = _state(root)
    current_revision = (state["group_revisions"][-1]["revision"]
                        if state["group_revisions"] else 0)
    if units is None and state["group_revisions"]:
        units = state["group_revisions"][-1]["units"]
    units = _owners(release, units)
    affected = set(affected or [])
    old_file = root / "resource_review_latest.json"
    previous = (json.loads(old_file.read_text("utf-8"))
                if old_file.is_file() else {})
    by_group, gaps = defaultdict(dict), []
    changes = {c["id"]: c for l in release["ledgers"]
               for c in l["final_changes"]}
    valid_facts = {x["id"]: x for x in raw["candidates"]}
    for candidate in valid_facts.values():
        for cid in candidate.get("change_ids", []):
            change = changes.get(cid)
            if change is None:
                gaps.append({"candidate_id": candidate["id"], "diff_id": cid,
                             "reason": "SOURCE_DIFF_NOT_IN_RELEASE"})
                continue
            matching_uses = [
                u for u in candidate.get("source_uses", [])
                if u.get("path") in (change.get("path"), change.get("old_path"))]
            for unit in units.get(cid, []):
                gid = unit.get("primary_group")
                if not gid or unit.get("unassigned"):
                    continue
                if unit.get("range_basis") == "WHOLE_RAW_CHANGE":
                    matched = True
                else:
                    matched = any(
                        u.get("side", "").upper() == "HEAD" and
                        u.get("path") == change.get("path") and
                        _intersect(unit.get("new_range"), u)
                        for u in matching_uses)
                if not matched:
                    gaps.append({"candidate_id": candidate["id"], "diff_id": cid,
                                 "unit_id": unit.get("id"),
                                 "reason": "SOURCE_NOT_IN_EXACT_UNIT_RANGE"})
                    continue
                item = by_group[gid].setdefault(candidate["id"],
                                                {"candidate_id": candidate["id"],
                                                 "source_change_ids": [],
                                                 "consumer_project": candidate["consumer_project"]})
                if cid not in item["source_change_ids"]:
                    item["source_change_ids"].append(cid)
    prev_groups = previous.get("groups", {})
    grouped, questions, reused, recomputed = {}, [], 0, 0
    for gid, impacts in sorted(by_group.items()):
        entries = sorted(impacts.values(), key=lambda x: x["candidate_id"])
        for entry in entries:
            entry["source_change_ids"].sort()
        fingerprint = _hash([raw["schema_version"],
                             sorted((e["candidate_id"], e["source_change_ids"])
                                    for e in entries),
                             [(cid, valid_facts[cid]) for cid in sorted(impacts)]])
        previous_group = prev_groups.get(gid, {})
        unchanged = (previous_group.get("fingerprint") == fingerprint
                     and gid not in affected)
        grouped[gid] = {"fingerprint": fingerprint,
                        "candidates": entries,
                        "recompute_status": "REUSED" if unchanged else "RECOMPUTED"}
        reused += int(unchanged)
        recomputed += int(not unchanged)
        for entry in entries:
            resource = valid_facts[entry["candidate_id"]]
            question = _question(
                "RESOURCE_CONSUMER", gid + ":" + resource["id"],
                "变更资源 %s（%s）是否被 %s 的固定版本用法依赖？" %
                (resource["resource"], resource["kind"], resource["consumer_project"]),
                [gid], entry["source_change_ids"],
                ["DIFF", "RESOURCE_LOCATION", "CONSUMER_CODE", "INSTANCE_BINDING"],
                "必须核实目标版本、资源身份/实例与实际兼容性；仅有源码名称不能确证")
            question.update({
                "resource_candidate_id": resource["id"],
                "resource_kind": resource["kind"],
                "consumer_project": resource["consumer_project"],
                "consumer_sha": resource["consumer_sha"],
                "consumer_locations": resource["consumer_uses"][:30]})
            questions.append(question)
    outcome = {
        "schema_version": "resource-review-v1",
        "group_revision": current_revision,
        "resource_facts_fingerprint": _hash(raw),
        "groups": grouped, "questions": questions,
        "coverage_gaps": gaps,
        "reused_groups": reused, "recomputed_groups": recomputed,
        "limitation": "Reassignment never changes fixed-SHA resource facts; "
                      "split old-side locations are not inferred as new lines"}
    _write_json(root / "resource_review_latest.json", outcome)
    folder = root / "revisions"
    folder.mkdir(exist_ok=True)
    _write_json(folder / ("resource-review-%03d-%s.json" %
                           (current_revision, _hash(outcome)[:12])), outcome)
    return {"available": True, "groups": len(grouped),
            "questions": len(questions),
            "recomputed_groups": recomputed,
            "reused_groups": reused, "coverage_gaps": len(gaps)}
