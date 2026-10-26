"""Build bounded semantic evidence requests from review questions automatically.

No model, dynamic repository discovery, file execution or network.
Only exact pinned version symbols and explicitly recognizable contract files.
"""
import json
from pathlib import Path

from .semantic_evidence import collect_semantic_evidence
from .review_workflow import load_batch, _write_json

MAX_SELECTORS = 50
CONTRACT_SUFFIXES = ("Api.java", "DTO.java", "Dto.java", "VO.java",
                     "Request.java", "Response.java", "Enum.java")


def auto_evidence(run_dir, repos):
    root, release = load_batch(run_dir)
    questions = json.loads((root / "questions.json").read_text("utf-8"))
    revised = root / "recomputed_latest.json"
    if revised.is_file():
        revision = json.loads(revised.read_text("utf-8"))
        questions["questions"] = revision["questions"]
    by_diff = {change["id"]: (ledger, change)
               for ledger in release["ledgers"]
               for change in ledger["final_changes"]}
    graphs = {(g["project"], g["side"], g["snapshot_sha"]): g
              for g in release.get("relations", {}).get("graphs", [])}
    selectors, omissions, seen = [], [], set()

    def register(qid, kind, project, sha, side, symbol=None, path=None):
        key = (kind, project, sha, side, symbol if kind == "METHOD" else path)
        if key in seen:
            return
        seen.add(key)
        if len(selectors) >= MAX_SELECTORS:
            omissions.append({"question_id": qid, "kind": kind,
                              "project": project, "reason": "SELECTOR_LIMIT"})
            return
        item = {"question_id": qid, "kind": kind, "project": project,
                "sha": sha, "side": side}
        if kind == "METHOD":
            item["symbol"] = symbol
        else:
            item["path"] = path
        selectors.append(item)

    for question in questions.get("questions", []):
        qid = question["id"]
        for diff_id in question.get("change_ids", []):
            entry = by_diff.get(diff_id)
            if not entry:
                omissions.append({"question_id": qid, "diff_id": diff_id,
                                  "reason": "RAW_CHANGE_NOT_FOUND"})
                continue
            ledger, change = entry
            project = ledger["project"]
            for side, sha, key, path_key in (
                    ("BASE", ledger["base"], "symbols_old", "old_path"),
                    ("HEAD", ledger["head"], "symbols_new", "path")):
                graph = graphs.get((project, side, sha))
                symbols = change.get(key, change.get("symbols", []))
                if not graph:
                    omissions.append({"question_id": qid, "diff_id": diff_id,
                                      "project": project, "side": side,
                                      "reason": "METHOD_GRAPH_UNAVAILABLE"})
                else:
                    for symbol in symbols:
                        if symbol in graph.get("method_index", {}):
                            register(qid, "METHOD", project, sha, side, symbol=symbol)
                        else:
                            omissions.append({"question_id": qid, "symbol": symbol,
                                              "side": side,
                                              "reason": "METHOD_NOT_INDEXED"})
                path = change.get(path_key)
                if path and path.endswith(CONTRACT_SUFFIXES):
                    register(qid, "CONTRACT", project, sha, side, path=path)
        consumer = question.get("consumer", {})
        if consumer and consumer.get("project") and consumer.get("symbol"):
            project = consumer["project"]
            finding = consumer.get("evidence") or {}
            side, sha = finding.get("side"), finding.get("sha")
            graph = graphs.get((project, side, sha))
            if graph and consumer["symbol"] in graph.get("method_index", {}):
                register(qid, "METHOD", project, sha, side, symbol=consumer["symbol"])
            else:
                omissions.append({"question_id": qid, "project": project,
                                  "reason": "CONSUMER_METHOD_OR_VERSION_UNCONFIRMED"})
    # Scan only pinned methods connected to the changed groups; lexical
    # branch/field matches are hints, not a new dependency proof.
    from .contract_context import discover_contract_context
    context_stats = discover_contract_context(root, repos)
    ctx = json.loads((root / "contract_context_latest.json").read_text("utf-8"))
    questions_by_group = {}
    for question in questions.get("questions", []):
        for group in question.get("group_ids", []):
            questions_by_group.setdefault(group, []).append(question["id"])
    for hit in ctx["facts"]:
        if hit["kind"] not in ("DECISION_BRANCH", "FIELD_DECLARATION",
                               "SQL_REFERENCE", "ENUM_OR_CONSTANT_USE"):
            continue
        graph = graphs.get((hit["project"], hit["side"], hit["sha"]))
        if not graph or hit["symbol"] not in graph.get("method_index", {}):
            continue
        for qid in questions_by_group.get(hit["group_id"], []):
            register(qid, "METHOD", hit["project"], hit["sha"],
                     hit["side"], symbol=hit["symbol"])
    plan = {"schema_version": "semantic-evidence-v1", "selectors": selectors,
            "omissions": omissions + ctx["omissions"],
            "context_facts": context_stats["facts"],
            "policy": "Only AST-indexed methods and known API/DTO file suffixes; "
                      "max 50 selectors and shared supplement budget"}
    _write_json(root / "auto_evidence_plan.json", plan)
    if not selectors:
        return {"planned": 0, "omitted": len(omissions),
                "status": "NO_PROVEN_SEMANTIC_TARGETS",
                "warning": "No inferred code; inspect question and graph omissions"}
    result = collect_semantic_evidence(root, {
        "schema_version": "semantic-evidence-v1",
        "selectors": selectors}, repos)
    return {"planned": len(selectors), "omitted": len(omissions),
            "new_evidence": result["new_evidence"],
            "results": result["results"], "round": result["round"],
            "stop_reason": result["stop_reason"]}
