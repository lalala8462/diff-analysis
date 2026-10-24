"""Budgeted full-method and version-pinned contract evidence.

Method limits are derived only from the JDK AST index of that exact Git SHA.
Contracts are whole pinned files; no guessed brace ranges or silent truncation.
"""
import json
from pathlib import Path

from .gitops import git, check_sha, safe_source_path
from .review_workflow import (load_batch, _load_supplement_state, _hash, _utc,
                              _write_json, refresh_evidence_manifest)

MAX_FILE_SIZE = 1_000_000


def collect_semantic_evidence(run_dir, request, repos,
                              max_total_bytes=1_000_000, max_rounds=3):
    root, release = load_batch(run_dir)
    selectors = request.get("selectors")
    if (request.get("schema_version") != "semantic-evidence-v1" or
            not isinstance(selectors, list) or
            not selectors or len(selectors) > 50):
        raise ValueError("Expected semantic-evidence-v1, 1..50 selectors")
    question_file = json.loads((root / "questions.json").read_text("utf-8"))
    valid_questions = {q["id"] for q in question_file["questions"]}
    revised = root / "recomputed_latest.json"
    if revised.is_file():
        latest = json.loads(revised.read_text("utf-8"))
        valid_questions.update(q["id"] for q in latest.get("questions", []))
    scope = {l["project"]: l for l in release["ledgers"]}
    graph = {(g["project"], g["side"], g["snapshot_sha"]): g
             for g in release.get("relations", {}).get("graphs", [])}
    state = _load_supplement_state(root)
    if state["rounds"] >= max_rounds:
        raise ValueError("Evidence rounds exhausted")
    remaining = max_total_bytes - state["bytes_used"]
    actions, staged = [], []
    for item in selectors:
        if not isinstance(item, dict):
            raise ValueError("Selector must be object")
        project, side, sha, kind = (
            item.get("project"), item.get("side"),
            item.get("sha"), item.get("kind"))
        qid = item.get("question_id")
        spec = scope.get(project)
        if (qid not in valid_questions or side not in ("BASE", "HEAD") or
                kind not in ("METHOD", "CONTRACT")):
            raise ValueError("Unknown question, side or evidence kind")
        if spec is None or sha != spec["base" if side == "BASE" else "head"]:
            actions.append({"status": "OUT_OF_SCOPE", "question_id": qid})
            continue
        if project not in repos:
            actions.append({"status": "OUT_OF_SCOPE", "question_id": qid,
                            "reason": "No explicit Git repository binding"})
            continue
        method = None
        if kind == "METHOD":
            symbol = item.get("symbol")
            method = graph.get((project, side, sha), {}).get(
                "method_index", {}).get(symbol)
            if not method:
                actions.append({"status": "NOT_FOUND", "question_id": qid,
                                "reason": "No indexed method at pinned SHA"})
                continue
            path, start, end = (method.get("path"), method.get("start"),
                                method.get("end"))
        else:
            path, start, end = item.get("path"), None, None
        if not isinstance(path, str) or not safe_source_path(path):
            raise ValueError("Illegal source path")
        if kind == "METHOD" and (not isinstance(start, int) or
                                  not isinstance(end, int) or start < 1 or
                                  end < start):
            raise ValueError("Corrupt method index bounds")
        key = "REQ-" + _hash([qid, kind, project, side, sha, path,
                              start, end])[:16]
        old = next((x for x in state["evidence"]
                    if x["request_id"] == key), None)
        if old:
            actions.append({"status": "PROVIDED", "request_id": key,
                            "evidence_id": old["id"], "reused": True})
            continue
        try:
            repo = Path(repos[project]).expanduser().resolve()
            if check_sha(repo, sha) != sha:
                raise ValueError("Pinned SHA changed")
            blob = sha + ":" + path
            size = int(git(repo, "cat-file", "-s", blob).decode("ascii").strip())
            if size > MAX_FILE_SIZE:
                actions.append({"status": "PARTIAL", "request_id": key,
                                "reason": "OVERSIZE_SOURCE_NOT_SILENTLY_TRUNCATED"})
                continue
            raw = git(repo, "show", blob)
            if b"\x00" in raw:
                actions.append({"status": "PARTIAL", "request_id": key,
                                "reason": "NON_TEXT_SOURCE"})
                continue
            lines = raw.decode("utf-8", "replace").splitlines()
            if kind == "METHOD":
                if end > len(lines):
                    actions.append({"status": "PARTIAL", "request_id": key,
                                    "reason": "METHOD_INDEX_RANGE_OUTSIDE_SOURCE"})
                    continue
                excerpt = lines[start - 1:end]
                base_line = start
            else:
                excerpt = lines
                base_line = 1
            content = ("".join("%d | %s\n" % (base_line + i, line)
                               for i, line in enumerate(excerpt)))
            encoded = content.encode("utf-8")
            if len(encoded) > remaining:
                actions.append({"status": "BUDGET_EXHAUSTED",
                                "request_id": key,
                                "required_bytes": len(encoded)})
                continue
            ev_id = "EV-" + _hash([project, side, sha, path, start, end,
                                   _hash(encoded)])[:16]
            stage_item = {"id": ev_id, "request_id": key,
                          "question_id": qid, "kind": kind,
                          "project": project, "side": side, "sha": sha,
                          "path": path, "start": base_line,
                          "end": base_line + len(excerpt) - 1,
                          "symbol": item.get("symbol") if method else None,
                          "bytes": len(encoded), "sha256": _hash(encoded),
                          "status": "COMPLETE",
                          "created_at_utc": _utc()}
            staged.append((ev_id, content, stage_item))
            actions.append({"status": "PROVIDED", "request_id": key,
                            "evidence_id": ev_id, "reused": False,
                            "kind": kind})
            remaining -= len(encoded)
        except (OSError, RuntimeError, ValueError) as exc:
            actions.append({"status": "FAILED", "request_id": key,
                            "reason": str(exc)[:200]})
    folder = root / "supplements"
    folder.mkdir(exist_ok=True)
    for ev_id, content, entry in staged:
        (folder / (ev_id + ".txt")).write_text(content, encoding="utf-8")
        state["evidence"].append(entry)
        state["bytes_used"] += entry["bytes"]
    state["rounds"] += 1
    result = {"schema_version": "evidence-supplement-v1",
              "round": state["rounds"], "results": actions,
              "new_evidence": len(staged),
              "stop_reason": ("NO_NEW_RELEVANT_EVIDENCE" if not staged else
                              "MAX_ROUNDS" if state["rounds"] >= max_rounds else None)}
    state["requests"].append(result)
    _write_json(root / "supplement_state.json", state)
    refresh_evidence_manifest(root, state)
    _write_json(folder / ("round-%03d.json" % state["rounds"]), result)
    return result
