"""Problem-driven offline review, bounded code supplements and decision journal.

Only user-supplied, pinned Git objects are read. Never executes analyzed code
or a model. Imported assessments are claims, not automatically verified truth.
"""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .gitops import git, check_sha, safe_source_path


def _hash(value):
    if not isinstance(value, (bytes, bytearray)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _write_json(path, data):
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def load_batch(run_dir):
    run_dir = Path(run_dir).expanduser().resolve()
    data = json.loads((run_dir / "analysis.json").read_text(encoding="utf-8"))
    if data.get("schema_version") != "release-evidence-v1":
        raise ValueError("Unsupported run schema")
    return run_dir, data


def _question(kind, key, text, group_ids, change_ids, requirements, condition):
    return {
        "id": "Q-" + _hash([kind, key, sorted(group_ids), sorted(change_ids)])[:16],
        "revision": 1, "kind": kind, "question": text,
        "group_ids": sorted(set(group_ids)),
        "change_ids": sorted(set(change_ids)),
        "necessary_evidence": requirements,
        "condition": condition, "status": "NEEDS_REVIEW",
        "human_status": "NOT_REVIEWED", "ai_status": "NOT_IMPORTED",
        "binding_status": "UNCONFIRMED",
        "limitation": "Question is not a defect finding or requirement confirmation"
    }


def build_questions(release):
    change_index = {c["id"]: c for led in release["ledgers"]
                    for c in led["final_changes"]}
    questions, suggestions = [], []
    for group in release["groups"]:
        changes = group["change_ids"]
        q = _question(
            "GROUP_BEHAVIOR", group["id"],
            "本组最终变化的旧、新行为分别是什么，是否与同一个目标一致？",
            [group["id"]], changes,
            ["DIFF", "METHOD", "EXPECTED_GOAL"],
            "完整方法与权威预期目标都存在后，才能判断需求符合性")
        questions.append(q)
        if group.get("issue_key") is not None:
            suggestions.append({
                "id": "S-" + _hash([group["id"], "KEEP"])[:16],
                "action": "KEEP", "group_ids": [group["id"]],
                "for_evidence": ["唯一任务号: " + group["issue_key"]],
                "against_evidence": ["任务号不保证业务目标相同"],
                "status": "SUGGESTED", "confidence": "WEAK"})
    for edge in release.get("cross_group_checks", []):
        group_ids = [g for g in (edge.get("from_group"), edge.get("to_group")) if g]
        changes = []
        for item in edge.get("evidence", []):
            changes.extend(item.get("change_ids", []))
        questions.append(_question(
            "CROSS_GROUP", edge["id"],
            "关联变化需要哪些配套处理？当前调用／结构证据是否实际成立？",
            group_ids, changes,
            ["DIFF", "RELATION", "CONTRACT", "METHOD"],
            "关系绑定、双端行为和预期约束仍需人工核实"))
        if len(group_ids) == 2:
            suggestions.append({
                "id": "S-" + _hash([edge["id"], "JOINT_REVIEW"])[:16],
                "action": "JOINT_REVIEW", "group_ids": group_ids,
                "for_evidence": ["静态关联：" + edge["id"]],
                "against_evidence": ["存在联系不代表属于同一需求"],
                "status": "SUGGESTED", "confidence": "WEAK"})
    for item in release.get("consumer_candidates", []):
        q = _question(
            "CONSUMER", item.get("id", "") + str(item.get("consumer_symbol")),
            "该消费者是否依赖修改前的行为？新版是否仍能正确处理？",
            [item["group_id"]],
            [item["change_id"]] if item.get("change_id") else [],
            ["DIFF", "METHOD", "CONSUMER", "CONTRACT", "RELATION"],
            "消费者固定版本、方法代码和服务绑定需可核对")
        q["consumer"] = {
            "project": item.get("consumer_project"),
            "symbol": item.get("consumer_symbol"),
            "evidence": item.get("evidence"),
            "source": item.get("source")}
        questions.append(q)
    # Evidence is present only when actually exported; code windows are not
    # equivalent to complete methods or externally supplied contracts.
    manifest = {}
    diff_ids = sorted(change_index)
    manifest["DIFF"] = {"total_known": len(diff_ids), "provided_ids": diff_ids,
                        "partial_ids": [], "omitted_ids": [],
                        "coverage": "GIT_WINDOW_ONLY"}
    for name in ("METHOD", "CONTRACT", "CONSUMER", "RELATION",
                 "EXPECTED_GOAL", "AI_READING"):
        manifest[name] = {
            "total_known": None, "provided_ids": [], "partial_ids": [],
            "omitted_ids": [], "coverage": "NOT_VERIFIED"}
    manifest["RELATION"]["total_known"] = sum(
        len(g["edges"]) for g in release.get("relations", {}).get("graphs", []))
    manifest["RELATION"]["partial_ids"] = [
        e["id"] for g in release.get("relations", {}).get("graphs", [])
        for e in g["edges"]]
    manifest["CONSUMER"]["total_known"] = len(release.get("consumer_candidates", []))
    manifest["CONSUMER"]["partial_ids"] = [
        x.get("id") or ("consumer-" + str(i))
        for i, x in enumerate(release.get("consumer_candidates", []))]
    scope = [{"project": l["project"], "base": l["base"], "head": l["head"]}
             for l in release["ledgers"]]
    result = {
        "schema_version": "review-questions-v1",
        "scope": scope, "scope_fingerprint": _hash(scope),
        "questions": questions,
        "suggestions": suggestions,
        "evidence_manifest": manifest,
        "reading_scope": {"external_ai": "UNKNOWN", "human": "UNKNOWN",
                          "files_declared": []},
        "warnings": ["No authoritatively confirmed expected goal supplied",
                     "Full method/consumer/contract evidence not yet guaranteed"]
    }
    return result


def export_questions(root, release):
    output = build_questions(release)
    root = Path(root)
    _write_json(root / "questions.json", output)
    _write_json(root / "evidence_manifest.json", output["evidence_manifest"])
    index = {c["id"]: c for ledger in release["ledgers"]
             for c in ledger["final_changes"]}
    lines = ["# 问题驱动评审（待人工／工具外 AI 核实）", "",
             "这些问题由静态关联与候选组生成，不是缺陷结论。", ""]
    for q in output["questions"]:
        lines += ["## " + q["id"] + " · " + q["kind"], "",
                  q["question"], "",
                  "关联组：" + ", ".join(q["group_ids"]),
                  "变化：" + (", ".join(q["change_ids"]) or "间接关联／待绑定"),
                  "成立条件：" + q["condition"],
                  "必要证据：" + ", ".join(q["necessary_evidence"]), ""]
        for cid in q["change_ids"]:
            change = index.get(cid)
            lines += ["- 变化证据：" + "evidence/" + cid + ".md"]
            if not change:
                continue
            lines += ["  固定版本：" + change["base_sha"] + " → " +
                      change["head_sha"], "  文件：" + change["path"]]
            for side, title in (("before", "旧版"), ("after", "新版")):
                excerpt = change.get(side, [])
                lines += ["  " + title + "变化窗口：", "~~~text"]
                for line in excerpt:
                    lines.append("%d | %s" % (
                        line["line"], str(line["text"])[:2000]))
                lines.append("~~~")
    lines += ["", "## 覆盖限制", "",
              "DIFF 代码窗口不是完整方法；CONSUMER、CONTRACT 与 RELATION "
              "可能只有候选，离线 AI 的实际阅读范围保持 UNKNOWN。", ""]
    (root / "questions.md").write_text("\n".join(lines), encoding="utf-8")
    return output


def _state(root):
    target = root / "review_state.json"
    if not target.is_file():
        return {"schema_version": "review-state-v1",
                "expected_goals": [], "rule_points": [],
                "decisions": [], "basis_checks": [], "group_revisions": [],
                "goal_revision_events": []}
    data = json.loads(target.read_text(encoding="utf-8"))
    if data.get("schema_version") != "review-state-v1":
        raise ValueError("Unsupported review state")
    return data


def import_review(run_dir, payload):
    """Append scoped human/AI results and explicit goals; never overwrite history."""
    root, release = load_batch(run_dir)
    if payload.get("schema_version") != "review-import-v1":
        raise ValueError("Expected review-import-v1")
    state = _state(root)
    questions = json.loads((root / "questions.json").read_text("utf-8"))
    current_scope_fingerprint = questions["scope_fingerprint"]
    group_ids = {g["id"] for g in release["groups"]}
    if state["group_revisions"]:
        latest_group = state["group_revisions"][-1]
        group_ids.update(g["id"] for g in latest_group.get("created_groups", []))
        current_scope_fingerprint = _hash([
            current_scope_fingerprint, latest_group["id"]])
        derived = root / "recomputed_latest.json"
        if derived.is_file():
            extra = json.loads(derived.read_text("utf-8"))
            if extra.get("revision") == latest_group["revision"]:
                questions["questions"] = extra["questions"]
    latest_questions = root / "review_package_latest.json"
    if latest_questions.is_file():
        current = json.loads(latest_questions.read_text("utf-8"))
        questions["questions"] = current["questions"]
        current_scope_fingerprint = _hash([
            current_scope_fingerprint,
            [(q["id"], q.get("dependency_fingerprint")) for q in current["questions"]]])
    valid = {"QUESTION": {q["id"] for q in questions["questions"]},
             "GROUP": group_ids,
             "DIFF": {c["id"] for l in release["ledgers"]
                      for c in l["final_changes"]}}
    ev_ids = valid["DIFF"] | {
        x["id"] for x in _load_supplement_state(root)["evidence"]}
    new_goals = payload.get("expected_goals", [])
    new_rules = payload.get("rule_points", [])
    if (not isinstance(new_goals, list) or not isinstance(new_rules, list) or
            not isinstance(payload.get("decisions", []), list)):
        raise ValueError("Goals and decisions must be arrays")
    planned_goals, planned_decisions, planned_rules, goal_events = [], [], [], []
    goal_ids = {g["id"] for g in state["expected_goals"]}
    for g in new_goals:
        if (not isinstance(g, dict) or
                not isinstance(g.get("id"), str) or
                not re.fullmatch(r"GOAL-[A-Za-z0-9_-]{1,80}", g["id"]) or
                g["id"] in goal_ids or
                not isinstance(g.get("text"), str) or len(g["text"].strip()) < 5 or
                g.get("status") != "HUMAN_CONFIRMED" or
                not isinstance(g.get("author"), str) or not g["author"].strip()):
            raise ValueError("Expected goal requires unique ID, human confirmation and author")
        parent = g.get("supersedes")
        if parent is not None:
            if parent not in {x["id"] for x in state["expected_goals"]}:
                raise ValueError("Can only revise an existing confirmed goal")
            goal_events.append({"old_goal_id": parent, "new_goal_id": g["id"],
                                "created_at_utc": _utc()})
        goal_ids.add(g["id"])
        planned_goals.append({**g, "revision": 1 if parent is None else 2,
                              "created_at_utc": _utc()})
    rule_ids = {r["id"] for r in state["rule_points"]}
    for item in new_rules:
        if (not isinstance(item, dict) or
                not isinstance(item.get("id"), str) or
                not re.fullmatch(r"RULE-[A-Za-z0-9_-]{1,80}", item["id"]) or
                item["id"] in rule_ids or
                item.get("expected_goal_id") not in goal_ids or
                item.get("status") not in ("CANDIDATE", "HUMAN_CONFIRMED") or
                not all(isinstance(item.get(k), str) and len(item[k].strip()) >= 5
                        for k in ("condition", "expected_behavior", "invariant")) or
                not isinstance(item.get("author"), str) or not item["author"]):
            raise ValueError("Invalid rule-point goal binding, condition, or status")
        rule_ids.add(item["id"])
        planned_rules.append({**item, "created_at_utc": _utc()})
    known_ids = {d["id"] for d in state["decisions"]}
    for item in payload.get("decisions", []):
        if not isinstance(item, dict):
            raise ValueError("Each decision must be an object")
        obj = item.get("object_type")
        oid = item.get("object_id")
        actor = item.get("actor_kind")
        dim = item.get("dimension")
        verdict = item.get("verdict")
        references = item.get("evidence_ids")
        if (obj not in valid or oid not in valid[obj] or
                actor not in ("HUMAN", "AI") or
                dim not in ("GROUPING", "REQUIREMENT", "IMPACT", "CONFORMANCE") or
                verdict not in ("SUPPORT", "REJECT", "INSUFFICIENT_EVIDENCE", "PENDING") or
                not isinstance(item.get("actor_id"), str) or not item["actor_id"] or
                not isinstance(item.get("reason"), str) or len(item["reason"].strip()) < 5 or
                not isinstance(references, list) or
                not all(isinstance(x, str) and x in ev_ids for x in references)):
            raise ValueError("Invalid review decision, object, reason or evidence reference")
        if verdict == "SUPPORT" and not references:
            raise ValueError("Positive decision must cite evidence")
        goal_id = item.get("expected_goal_id")
        if goal_id is not None and goal_id not in goal_ids:
            raise ValueError("Unknown expected goal")
        if dim == "CONFORMANCE" and verdict == "SUPPORT" and goal_id is None:
            raise ValueError("Conformance requires confirmed ExpectedGoal")
        supersedes = item.get("supersedes")
        if supersedes is not None and supersedes not in known_ids:
            raise ValueError("Unknown superseded decision")
        did = "DEC-" + _hash([oid, actor, dim, len(state["decisions"]) +
                                      len(planned_decisions), _utc()])[:16]
        known_ids.add(did)
        question_fingerprint = next((
            q.get("dependency_fingerprint") for q in questions["questions"]
            if obj == "QUESTION" and q["id"] == oid), None)
        planned_decisions.append({
            **item, "id": did, "created_at_utc": _utc(),
            "question_dependency_fingerprint": question_fingerprint,
            "scope_fingerprint": current_scope_fingerprint,
            "structural_check": "REFERENCES_VALID",
            "basis_check": "NOT_REVIEWED",
            "actor_identity_verified": False if actor == "AI" else None,
            "reading_scope": item.get("reading_scope", "UNKNOWN"),
            "applicability": "CURRENT"})
    state["expected_goals"].extend(planned_goals)
    state["rule_points"].extend(planned_rules)
    state.setdefault("goal_revision_events", []).extend(goal_events)
    state["decisions"].extend(planned_decisions)
    _write_json(root / "review_state.json", state)
    package = None
    if goal_events:
        from .revision_pipeline import update_review_package
        package = update_review_package(root)
    return {"goals_added": len(planned_goals),
            "rules_added": len(planned_rules),
            "decisions_added": len(planned_decisions),
            "review_package": package}


def _load_supplement_state(root):
    path = root / "supplement_state.json"
    if not path.exists():
        return {"schema_version": "supplement-state-v1", "rounds": 0,
                "bytes_used": 0, "evidence": [], "requests": []}
    result = json.loads(path.read_text("utf-8"))
    if result.get("schema_version") != "supplement-state-v1":
        raise ValueError("Unsupported supplement state")
    return result


def refresh_evidence_manifest(root, supplement_state):
    """Rebuild a revisioned view without overwriting original export coverage.

    Unknown discovery totals stay unknown; providing a whole file is not
    proof that every required contract or consumer was discovered.
    """
    root = Path(root)
    original = root / "evidence_manifest.json"
    if not original.is_file():
        return
    manifest = json.loads(original.read_text(encoding="utf-8"))
    known = {"METHOD", "CONTRACT", "CONSUMER", "RELATION",
             "DIFF", "EXPECTED_GOAL", "AI_READING"}
    summary = {}
    for name in known | {"CODE_WINDOW"}:
        base = manifest.get(name, {
            "total_known": None, "provided_ids": [],
            "partial_ids": [], "omitted_ids": [], "coverage": "NOT_VERIFIED"})
        summary[name] = json.loads(json.dumps(base))
        summary[name].setdefault("bytes_provided", 0)
    for entry in supplement_state["evidence"]:
        category = entry.get("kind")
        if category not in ("METHOD", "CONTRACT"):
            category = "CODE_WINDOW"
        group = summary[category]
        if entry["id"] not in group["provided_ids"]:
            group["provided_ids"].append(entry["id"])
            group["bytes_provided"] += entry.get("bytes", 0)
        if group["coverage"] == "NOT_VERIFIED":
            group["coverage"] = "SUPPLEMENTED_NOT_EXHAUSTIVE"
    report = {"schema_version": "evidence-manifest-revision-v1",
              "round": supplement_state["rounds"],
              "categories": summary, "total_bytes_used": supplement_state["bytes_used"],
              "actual_ai_reading_scope": "UNKNOWN",
              "warning": "Exported evidence is not proof of actual AI reading or "
                         "complete discovery of contracts, methods or consumers"}
    directory = root / "supplements"
    directory.mkdir(exist_ok=True)
    _write_json(directory / ("manifest-%03d.json" % supplement_state["rounds"]), report)
    _write_json(root / "evidence_manifest_latest.json", report)


def supplement(run_dir, request, repos, max_total_bytes=1_000_000, max_rounds=3):
    """User-authorized pinned reads; no arbitrary paths outside Git objects.

    The same byte/round budgets persist across invocations; duplicates are
    reused without spending budget, and no progress ends the round.
    """
    root, release = load_batch(run_dir)
    if (request.get("schema_version") != "evidence-requests-v1" or
            not isinstance(request.get("selectors"), list) or
            len(request["selectors"]) > 50 or not request["selectors"]):
        raise ValueError("Invalid evidence-requests-v1 selectors (1..50)")
    known_questions = {q["id"] for q in json.loads(
        (root / "questions.json").read_text("utf-8"))["questions"]}
    for filename in ("recomputed_latest.json", "review_package_latest.json"):
        current_path = root / filename
        if current_path.is_file():
            content = json.loads(current_path.read_text(encoding="utf-8"))
            known_questions.update(q["id"] for q in content.get("questions", []))
    scope = {l["project"]: l for l in release["ledgers"]}
    state = _load_supplement_state(root)
    if state["rounds"] >= max_rounds:
        raise ValueError("Evidence round budget exhausted; manual policy revision required")
    items = []
    budget = max_total_bytes - state["bytes_used"]
    new_count = 0
    stage = []
    for selector in request["selectors"]:
        if not isinstance(selector, dict):
            raise ValueError("Selector must be an object")
        qid = selector.get("question_id")
        project, sha, side = (selector.get("project"), selector.get("sha"),
                              selector.get("side"))
        path, start, end = (selector.get("path"), selector.get("start"),
                            selector.get("end"))
        if qid not in known_questions or side not in ("BASE", "HEAD"):
            raise ValueError("Unknown question or invalid side")
        if (not isinstance(path, str) or not safe_source_path(path) or
                not isinstance(start, int) or isinstance(start, bool) or
                not isinstance(end, int) or isinstance(end, bool) or
                start < 1 or end < start or end - start + 1 > 500):
            raise ValueError("Invalid Git relative path or bounded line range")
        if (not isinstance(selector.get("reason"), str) or
                len(selector["reason"].strip()) < 5):
            raise ValueError("Supplement needs an explicit reason")
        spec = scope.get(project)
        expected = spec.get("base" if side == "BASE" else "head") if spec else None
        if sha != expected or project not in repos:
            items.append({"question_id": qid, "project": project, "status": "OUT_OF_SCOPE"})
            continue
        rid = "REQ-" + _hash([qid, project, sha, side, path, start, end])[:16]
        reused = next((x for x in state["evidence"] if x["request_id"] == rid), None)
        if reused:
            items.append({"request_id": rid, "status": "PROVIDED",
                          "evidence_id": reused["id"], "reused": True})
            continue
        if budget <= 0:
            items.append({"request_id": rid, "status": "BUDGET_EXHAUSTED"})
            continue
        try:
            repo = Path(repos[project]).resolve()
            if check_sha(repo, sha) != sha:
                raise ValueError("Pinned SHA did not resolve exactly")
            object_id = sha + ":" + path
            size = int(git(repo, "cat-file", "-s", object_id).decode("ascii").strip())
            if size > 2_000_000:
                items.append({"request_id": rid, "status": "PARTIAL",
                              "reason": "FILE_OVERSIZE"})
                continue
            raw = git(repo, "show", object_id)
            if b"\x00" in raw:
                items.append({"request_id": rid, "status": "PARTIAL",
                              "reason": "BINARY"})
                continue
            lines = raw.decode("utf-8", "replace").splitlines()
            if start > len(lines):
                items.append({"request_id": rid, "status": "NOT_FOUND"})
                continue
            content = "\n".join("%d | %s" % (i + 1, lines[i])
                                for i in range(start - 1, min(end, len(lines)))) + "\n"
            content_bytes = content.encode("utf-8")
            if len(content_bytes) > budget:
                items.append({"request_id": rid, "status": "BUDGET_EXHAUSTED"})
                continue
            ev_id = "EV-" + _hash([project, sha, side, path, start, end,
                                   _hash(content_bytes)])[:16]
            stage.append((ev_id, content, {"id": ev_id, "request_id": rid,
                "question_id": qid, "project": project, "sha": sha,
                "side": side, "path": path, "start": start, "end": end,
                "bytes": len(content_bytes), "sha256": _hash(content_bytes),
                "created_at_utc": _utc()}))
            items.append({"request_id": rid, "status": "PROVIDED",
                          "evidence_id": ev_id, "reused": False})
            budget -= len(content_bytes)
            new_count += 1
        except (RuntimeError, OSError, ValueError) as exc:
            items.append({"request_id": rid, "status": "NOT_FOUND",
                          "reason": str(exc)[:200]})
    # Do not mutate budget/journal until all selectors have been validated.
    store = root / "supplements"
    store.mkdir(exist_ok=True)
    for evidence_id, content, entry in stage:
        (store / (evidence_id + ".txt")).write_text(content, encoding="utf-8")
        state["evidence"].append(entry)
        state["bytes_used"] += entry["bytes"]
    state["rounds"] += 1
    outcome = {
        "schema_version": "evidence-supplement-v1",
        "round": state["rounds"], "results": items,
        "new_evidence": new_count,
        "stop_reason": "NO_NEW_RELEVANT_EVIDENCE" if not new_count else
                       "MAX_ROUNDS" if state["rounds"] >= max_rounds else None}
    state["requests"].append(outcome)
    _write_json(root / "supplement_state.json", state)
    refresh_evidence_manifest(root, state)
    _write_json(store / ("round-%03d.json" % state["rounds"]), outcome)
    return outcome
