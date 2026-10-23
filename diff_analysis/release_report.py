"""Human-first release reports, per-change code evidence, model-ready group inputs.

No AI requests are made by this module. All descriptions are evidence-bound.
"""
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .release_analysis import assemble_release
from .llm_adapter import run_model, ModelError


MODEL_BUDGET_CHARS = 24000


def _safe_label(value):
    return re.sub(r"[^\w.-]+", "_", str(value))[:100]


def _code_lines(lines):
    if not lines:
        return "_此侧没有可展示的文本片段（删除、新增、二进制或文件不可读）。_\n"
    # Evidence contains source text. Nothing is sent to a remote model by default.
    return "~~~text\n" + "\n".join(
        "%6d | %s" % (line["line"], line["text"].replace("\x00", "")[:2000])
        for line in lines) + "\n~~~\n"


def _write_evidence(root, change):
    path = root / (change["id"] + ".md")
    rows = ["# " + change["id"], "",
            "项目：" + change["project"],
            "文件：" + change["path"],
            "旧文件：" + change["old_path"],
            "旧版：" + change["base_sha"],
            "新版：" + change["head_sha"],
            "性质：" + change["file_category"] + "，" + change["status"],
            "符号（候选）：" + (", ".join(change["symbols"]) or "未知"),
            "提交来源：" + change["provenance_status"],
            "候选提交：" + (", ".join(change["commit_candidates"]) or "无法确认"),
            "",
            "## 旧版（变化位置前后 30 行）", "",
            _code_lines(change["before"]), "",
            "## 新版（变化位置前后 30 行）", "",
            _code_lines(change["after"]), "",
            "> 代码摘录由原始 Git 对象生成；省略范围之外的代码不代表已分析。",
            ""]
    path.write_text("\n".join(rows), encoding="utf-8")
    return path


def _model_input(group, changes, intersections, ledgers):
    citations = {c["id"]: c for c in changes}
    history = {(l["project"], l["head"]): l["history"] for l in ledgers}
    compact = []
    used = 0
    dropped = []
    for change_id in group["change_ids"]:
        c = citations[change_id]
        item = {
            "diff_id": change_id, "project": c["project"], "category": c["file_category"],
            "status": c["status"], "symbols": c["symbols"],
            "old_range": c["old_range"], "new_range": c["new_range"],
            "commit_candidates": c["commit_candidates"],
            "old_context": c["before"], "new_context": c["after"]
        }
        serialized = json.dumps(item, ensure_ascii=False)
        if used + len(serialized) > MODEL_BUDGET_CHARS:
            dropped.append(change_id)
        else:
            compact.append(item)
            used += len(serialized)
    related = [r for r in intersections if group["id"] in (r["from_group"], r["to_group"])]
    commit_info = []
    for (project, head), timeline in history.items():
        hashes = {sha for x in compact if x["project"] == project
                  and citations[x["diff_id"]]["head_sha"] == head
                  for sha in x["commit_candidates"]}
        commit_info.extend({
            "project": project, "sha": c["sha"], "subject": c["subject"],
            "issue_keys": c["issue_keys"]}
            for c in timeline["commits"] if c["sha"] in hashes)
    return {
        "schema_version": "model-group-input-v1",
        "group_id": group["id"], "group_basis": group["group_basis"],
        "group_status": "CANDIDATE", "source": "STATIC_ANALYSIS",
        "facts": compact, "commit_messages": commit_info[:80],
        "related_group_checks": related[:30],
        "unprovided_change_ids": dropped,
        "budget": {"max_chars": MODEL_BUDGET_CHARS, "truncated": bool(dropped)},
        "task": ("以中文提出可能的需求目标、V1/V2 变化、业务影响及人工核实问题；"
                 "所有代码事实必须标注 diff_id；不能编造调用链、业务契约或确定缺陷；"
                 "明确区分最终差异、提交候选来源与推断。"
                 "若内容截断只能指出未分析范围。"),
        "expected_output": {
            "possible_requirement": "string",
            "old_vs_new_behavior": "string",
            "possible_impacts": [{"description": "string", "diff_ids": ["D-id"],
                                  "confidence": "HYPOTHESIS"}],
            "questions_for_reviewer": ["string"],
            "needs_human_confirmation": True
        }
    }


def write_release_bundle(specs, reviews, cross_candidates, run_root, db=None,
                         model_endpoint=None, model_name=None,
                         allow_remote_model=False, model_key_env='DIFF_ANALYSIS_MODEL_API_KEY',
                         max_model_groups=20):
    """Write immutable per-run evidence; optional SQLite run index, never baseline facts."""
    release = assemble_release(specs, reviews, cross_candidates)
    timeid = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = timeid + "-" + uuid.uuid4().hex[:8]
    root = Path(run_root).expanduser().resolve() / run_id
    evidence = root / "evidence"
    model_dir = root / "model_inputs"
    evidence.mkdir(parents=True, exist_ok=False)
    model_dir.mkdir()
    changes = [c for l in release["ledgers"] for c in l["final_changes"]]
    for change in changes:
        _write_evidence(evidence, change)
    model_results = {}
    for index, group in enumerate(release["groups"]):
        payload = _model_input(group, changes, release["cross_group_checks"],
                               release["ledgers"])
        (model_dir / (group["id"] + ".json")).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if model_endpoint and index < max_model_groups:
            try:
                result = run_model(payload, model_endpoint, model_name,
                                   key_env=model_key_env,
                                   allow_remote=allow_remote_model)
            except ModelError as exc:
                result = {"status": "MODEL_FAILED", "group_id": group["id"],
                          "error": str(exc)[:250]}
            model_results[group["id"]] = result
            (model_dir / (group["id"] + ".result.json")).write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        elif model_endpoint:
            model_results[group["id"]] = {"status": "NOT_ANALYZED_BUDGET",
                                           "group_id": group["id"]}
    release["model_results"] = model_results
    release["run_id"] = run_id
    release["created_at_utc"] = datetime.now(timezone.utc).isoformat()
    if not model_endpoint:
        release["model_status"] = "NOT_INVOKED"
    elif any(r["status"] != "MODEL_HYPOTHESIS" for r in model_results.values()):
        release["model_status"] = "PARTIAL"
    else:
        release["model_status"] = "COMPLETED_WITH_UNVERIFIED_HYPOTHESES"
    (root / "analysis.json").write_text(
        json.dumps(release, ensure_ascii=False, indent=2), encoding="utf-8")
    report = [
        "# 版本候选需求与交叉影响评审", "",
        "> 根据最终 Git 差异生成；需求分组、历史归因和影响均为候选，不代表业务已验证。",
        "> 模型状态：" + release["model_status"] + "。输入位于 model_inputs/；所有模型结论都需要人工确认。", "",
        "## 版本与分析范围", "",
        "| 项目 | 旧版 SHA | 新版 SHA | 最终差异项 | 来源历史 |",
        "| --- | --- | --- | ---: | --- |"]
    for led in release["ledgers"]:
        report.append("| %s | %s | %s | %d | %s |" %
                      (led["project"], led["base"][:12], led["head"][:12],
                       len(led["final_changes"]), led["history"]["status"]))
    report += ["", "## 需求候选组（按最终差异归属，不代表已确认需求）", ""]
    by_id = {c["id"]: c for c in changes}
    for group in release["groups"]:
        group_changes = [by_id[i] for i in group["change_ids"]]
        report += ["### " + group["id"] + " · " + group["label"],
                   "", "**分组依据**：" + group["group_basis"],
                   "**说明**：" + group["summary"],
                   "**涉及项目**：" + ", ".join(group["projects"]), "",
                   "**最终变化与代码证据**：", ""]
        for c in group_changes:
            display = c["file_category"] + " / " + c["status"]
            report.append("- [%s](evidence/%s.md) · %s · %s" %
                          (c["id"], c["id"], display,
                           "、".join(c["symbols"][:2]) if c["symbols"] else "代码元素尚未映射"))
        report += ["", "**需要人工确认**：这些变化是否属于同一需求？"
                   "哪些是行为改变、哪些只是重构？是否存在未纳管关联程序？", ""]
        response = model_results.get(group["id"])
        if response and response["status"] == "MODEL_HYPOTHESIS":
            obj = response["output"]
            report += ["**模型推断（未经人工确认）**：",
                       "", "可能需求：" + str(obj.get("possible_requirement", "未知"))[:1500],
                       "", "新旧行为：" + str(obj.get("old_vs_new_behavior", "未知"))[:1500],
                       "", "待核实问题："]
            for q in obj.get("questions_for_reviewer", [])[:10]:
                report.append("- " + str(q)[:500])
            for impact in obj.get("possible_impacts", [])[:15]:
                report.append("- 可能影响：" + str(impact.get("description", ""))[:600] +
                              "（证据：" + ", ".join(impact["diff_ids"]) + "）")
        elif response:
            report.append("**模型状态**：" + response["status"] + "；"
                          + response.get("error", "未分析"))
        report += ["", "模型输入：[查看 JSON](model_inputs/" + group["id"] + ".json)", ""]

    report += ["## 组与组之间的检查线索", ""]
    if not release["cross_group_checks"]:
        report.append("未发现当前规则支持的交叉候选；这不代表不同需求之间没有交叉影响。")
    for edge in release["cross_group_checks"]:
        report.append("- %s ↔ %s：%s（%s，非缺陷认定）" %
                      (edge["from_group"], edge["to_group"],
                       edge["kind"], edge["status"]))
    report += ["", "## 证据不足与覆盖边界", ""]
    total = len(changes)
    mapped = sum(c["status"] == "SEMANTIC_MAPPED" for c in changes)
    report.append("- 已登记 %d 个最终差异 Hunk/文件事件，其中 %d 个初步映射 Java 方法；"
                  "剩余 %d 个需要人工或后续解析器处理。" % (total, mapped, total - mapped))
    for led in release["ledgers"]:
        if led["history"]["status"] != "COMPLETE":
            report.append("- %s：提交演进状态 %s，历史说明可能不完整。" %
                          (led["project"], led["history"]["status"]))
        for warning in led["warnings"]:
            report.append("- %s：%s" % (led["project"], warning["reason"]))
    report += ["- 没有强制调用 SmartCommitCore、CodeTracker、RefactoringMiner 或 Code Maat。",
               "- 文件级共同出现/相同方法不作为已证实需求分组依据；跨组线索仅供复核。",
               "- 未配置模型时不运行模型；配置模型时结果仅作为需要人工核实的假设。",
               "- 模型输入有长度预算，若截断请查看各组 unprovided_change_ids。",
               "- 代码证据为前后各 30 行上下文，源自固定 Git SHA；内容可能包含内部源码，"
               "本地报告应按公司代码保密规范管理。", ""]
    (root / "report.md").write_text("\n".join(report), encoding="utf-8")
    if db is not None:
        _register_run(db, run_id, root, release)
    return root, release


def _register_run(db, run_id, root, release):
    # Independent from catalog/baseline tables; model conclusions never mutate version facts.
    import sqlite3
    target = Path(db).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(target)) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS analysis_run(
            run_id TEXT PRIMARY KEY, created_at_utc TEXT NOT NULL,
            report_path TEXT NOT NULL, summary_json TEXT NOT NULL)""")
        summary = {"status": release["status"], "model_status": release["model_status"],
                   "project_scope": release["project_scope"],
                   "group_count": len(release["groups"]),
                   "cross_check_count": len(release["cross_group_checks"])}
        conn.execute("INSERT INTO analysis_run VALUES (?,?,?,?)",
                     (run_id, release["created_at_utc"], str(root),
                      json.dumps(summary, ensure_ascii=False)))
