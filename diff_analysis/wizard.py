"""Interactive Windows CMD/PowerShell setup and multi-project revision comparison.

Wizard gathers and confirms inputs before touching the catalog or producing reports.
"""
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from .baseline import prepare_baseline, snapshot_summary, target_branch_sha, git_tree
from .catalog import CatalogStore, BaselineError, validate_project, validate_version
from .gitlab import get_mr, clone_mr
from .gitops import check_sha, GitError
from .scanner import ScannerError
from .java_classpath import load_classpaths, for_project
from .review import review_repo, stitch_cross_project
from .release_report import (write_release_bundle, start_ledger_checkpoint,
                             resolve_run_root)
from .architecture_sources import isolate_specs, mr_copy_path, SourceError


class WizardCancelled(Exception):
    pass


def _ask(message, default=None, required=False, input_fn=input):
    while True:
        hint = " [%s]" % default if default is not None else ""
        raw = input_fn(message + hint + ": ").strip()
        if raw.lower() in ("q", "quit", "exit"):
            raise WizardCancelled()
        value = raw if raw else default
        if value is not None and (not required or bool(value)):
            return value
        print("请填写此项；输入 q 可退出。")


def resolve_revision(repo, revision):
    """Convert a user-entered tag/branch/revision to an immutable Git commit SHA."""
    revision = revision.strip()
    if not revision or len(revision) > 200 or revision[0] == "-" or "\n" in revision or "\r" in revision:
        raise BaselineError("Git 修订号无效")
    try:
        p = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--end-of-options",
             revision + "^{commit}"],
            capture_output=True, check=False)
    except OSError as exc:
        raise BaselineError("无法执行 git：请检查 Git for Windows") from exc
    if p.returncode:
        raise BaselineError("找不到 Git 修订号 %s（请检查 branch/tag/SHA 是否存在）" % revision)
    sha = p.stdout.decode("ascii", "replace").strip()
    return check_sha(repo, sha)


def _default_version(revision, sha):
    if (re.fullmatch(r"[vV][0-9][A-Za-z0-9._-]{0,75}", revision)
            and not re.fullmatch(r"[a-fA-F0-9]{40,64}", revision)):
        return revision
    return "sha-" + sha[:12]


def _local_project(input_fn, evidence=False):
    location = _ask("本地 Git 项目路径", required=True, input_fn=input_fn)
    repo = Path(location.strip('"')).expanduser().resolve()
    if not repo.is_dir():
        raise BaselineError("目录不存在：" + str(repo))
    default_project = "local/" + repo.name
    project = validate_project(_ask(
        "项目标识（例如 rhf/spms-back）", default_project, input_fn=input_fn))
    old_ref = _ask("旧版 Git 修订号（分支 / Tag / SHA，如 V1.0）",
                   required=True, input_fn=input_fn)
    new_ref = _ask("新版 Git 修订号（分支 / Tag / SHA，如 V1.1）",
                   required=True, input_fn=input_fn)
    old_sha = resolve_revision(repo, old_ref)
    new_sha = resolve_revision(repo, new_ref)
    if old_sha == new_sha and not evidence:
        raise BaselineError("旧版和新版指向同一个 Commit，无变化可比较")
    old_v = validate_version(_ask(
        "旧版本标签（用于后续基线复用）", _default_version(old_ref, old_sha),
        input_fn=input_fn))
    new_v = validate_version(_ask(
        "新版本标签（仅用于此次比较展示）", _default_version(new_ref, new_sha),
        input_fn=input_fn))
    if old_v == new_v and not evidence:
        raise BaselineError("旧版本和新版本的标签不能相同")
    owner = _ask("项目责任人/团队备注（可选，无权限含义）", "", input_fn=input_fn)
    return {"kind": "local", "role": "EVIDENCE" if evidence else "CHANGE",
            "repo": repo, "project": project,
            "old_ref": old_ref, "new_ref": new_ref, "base": old_sha, "head": new_sha,
            "old_version": old_v, "new_version": new_v, "owner": owner}


def _mr_project(input_fn, token):
    url = _ask("未合并的 GitLab MR URL", required=True, input_fn=input_fn)
    host = urlsplit(url).hostname or ""
    allowed_host = _ask(
        "确认允许访问的 GitLab 主机名", host, required=True, input_fn=input_fn)
    mr = get_mr(url, allowed_host, token)
    if mr.get("state") != "opened" or not mr.get("start"):
        raise BaselineError(
            "当前只支持尚未合并且包含 diff_refs.start_sha 的 MR；"
            "已合并 MR 请改用本地 Git 仓库与准确的旧/新 SHA")
    old_ref = mr.get("target_branch") or ""
    new_ref = mr.get("source_branch") or ""
    old_v = validate_version(_ask(
        "旧版本标签", (old_ref if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", old_ref)
                     else "sha-" + mr["start"][:12]), input_fn=input_fn))
    new_v = validate_version(_ask(
        "新版本标签", (new_ref if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", new_ref)
                     else "sha-" + mr["head"][:12]), input_fn=input_fn))
    if old_v == new_v:
        raise BaselineError("旧版与新版标签不能相同")
    owner = _ask("项目责任人/团队备注（可选，无权限含义）", "", input_fn=input_fn)
    return {"kind": "mr", "project": mr["project"], "mr_url": url,
            "host": allowed_host, "mr": mr,
            "base": mr["start"], "head": mr["head"],
            "old_version": old_v, "new_version": new_v, "owner": owner}


def _register_project(store, item):
    project = item["project"]
    existing = next((p for p in store.list_projects()
                     if p["project_path"] == project), None)
    owner = item["owner"] or (existing["owner"] if existing else "")
    if existing is None or not existing["active"] or (item["owner"] and owner != existing["owner"]):
        store.add_project(project, owner)
    else:
        store.require_project(project)


def run_wizard(args, input_fn=input, output_fn=print):
    """Interactive path; reports may be rerun from the pinned SHA in JSON."""
    run_root = resolve_run_root(getattr(args, 'run_root', None))
    output_fn("=== diff-analysis 多项目版本比较向导 ===")
    output_fn("支持本地 Git 分支/Tag/SHA，也支持未合并的 GitLab MR。输入 q 随时取消。")
    items = []
    token = None
    try:
        while True:
            kind = _ask("添加项目：1=本地变更，2=GitLab MR，3=本地证据项目，0=完成输入",
                        "1" if not items else "0", input_fn=input_fn)
            if kind == "0":
                if not items:
                    output_fn("至少添加一个项目。")
                    continue
                break
            if kind not in ("1", "2", "3"):
                output_fn("请选择 0、1、2 或 3。")
                continue
            try:
                if kind in ("1", "3"):
                    item = _local_project(input_fn, evidence=(kind == "3"))
                else:
                    token = token or os.environ.get("GITLAB_TOKEN")
                    if not token:
                        raise BaselineError(
                            "GitLab 模式需要 GITLAB_TOKEN；请在当前 CMD/PowerShell "
                            "设置环境变量后重新执行（不会将 Token 保存到 SQLite）")
                    item = _mr_project(input_fn, token)
                    item["role"] = "CHANGE"
                if any(r["project"] == item["project"] for r in items):
                    raise BaselineError(
                        "此向导每个项目只能输入一组修订号；多个 MR 合并候选功能仍待开发")
                items.append(item)
                output_fn("已添加：%s  %s (%s) → %s (%s)" %
                          (item["project"], item["old_version"], item["base"][:12],
                           item["new_version"], item["head"][:12]))
            except (BaselineError, GitError, ValueError, OSError) as exc:
                output_fn("输入失败：" + str(exc))
                continue

        output_fn("\n--- 比较清单（按以下固定 Git SHA 执行） ---")
        for i, item in enumerate(items, 1):
            output_fn("%d. [%s] %s: %s (%s) → %s (%s)" %
                      (i, item["kind"], item["project"], item["old_version"],
                       item["base"][:12], item["new_version"], item["head"][:12]))
        answer = _ask("确认开始扫描并生成报告？y/N", "N", input_fn=input_fn).lower()
        if answer not in ("y", "yes"):
            output_fn("已取消；项目清单和报告均未修改。")
            return False
    except (WizardCancelled, EOFError, KeyboardInterrupt):
        output_fn("\n已取消。")
        return False

    with CatalogStore(args.db) as store:
        with tempfile.TemporaryDirectory(prefix="diff-wizard-") as tmp:
            root = Path(tmp)
            cache = root / "javac"
            results, specs, comparisons, scan_failures = [], [], [], []
            usable_items = []
            # Prepare Git project identities independently. No Java scanner runs yet.
            for i, item in enumerate(items):
                project = item["project"]
                try:
                    _register_project(store, item)
                    if item["kind"] == "local":
                        repo = item["repo"]
                        base, head = item["base"], item["head"]
                        isolated, failures, active = isolate_specs([(repo, project, base, head)])
                        if active:
                            if failures:
                                raise SourceError(failures[0]['reason'])
                            repo = isolated[0][0]
                    else:
                        live = get_mr(item["mr_url"], item["host"], token)
                        if (live["head"] != item["head"] or
                                live.get("start") != item["base"] or
                                live.get("state") != "opened"):
                            raise BaselineError(
                                "MR_CHANGED: confirmation invalidated by MR update")
                        if target_branch_sha(live, token) != item["base"].lower():
                            raise BaselineError("TARGET_MOVED: MR target version changed")
                        repo = clone_mr(live, mr_copy_path(project), token)
                        base, head = live["start"], live["head"]
                        isolated, failures, active = isolate_specs([(repo, project, base, head)])
                        if active:
                            if failures:
                                raise SourceError(failures[0]['reason'])
                            repo = isolated[0][0]
                        item["_diff_base"] = live["base"]
                    usable_items.append((i, item, repo, base, head))
                    specs.append((repo, project, base, head))
                except (BaselineError, GitError, OSError, ValueError, SourceError) as exc:
                    scan_failures.append({
                        "project": project, "base": item["base"],
                        "head": item["head"], "stage": "PREPARE",
                        "error_type": type(exc).__name__, "reason": str(exc)[:500]})
                    output_fn("项目准备失败 %s：%s" % (project, exc))
            roles = {(i["project"], base, head): i.get("role", "CHANGE")
                     for _, i, _, base, head in usable_items}
            bundle_root, checkpoint = start_ledger_checkpoint(specs, run_root, roles)
            usable = {(l["project"], l["base"], l["head"])
                      for l in checkpoint["ledgers"]}
            try:
                java_classpaths = load_classpaths(
                    getattr(args, "java_classpath_manifest", None))
                classpath_error = None
            except (ValueError, OSError) as exc:
                java_classpaths = {}
                classpath_error = str(exc)
            for i, item, repo, base, head in usable_items:
                project = item["project"]
                if (project, base, head) not in usable:
                    continue
                output_fn("[%d/%d] 分析 %s ..." % (i + 1, len(items), project))
                created, snapshot = False, None
                try:
                    if item.get("role") != "EVIDENCE":
                        snapshot, created = prepare_baseline(
                            store, project, item["old_version"], repo, base)
                    if item["kind"] == "mr":
                        store.save_candidate({
                            "project_path": project, "mr_url": item["mr_url"],
                            "old_version": item["old_version"],
                            "new_version": item["new_version"],
                            "target_sha": base, "diff_base_sha": item["_diff_base"],
                            "source_head_sha": head,
                            "source_tree_sha": git_tree(repo, head),
                            "state": "SOURCE_HEAD_ONLY"
                        })
                except (BaselineError, GitError, ScannerError, OSError, ValueError) as exc:
                    scan_failures.append({
                        "project": project, "base": base, "head": head,
                        "stage": "BASELINE", "error_type": type(exc).__name__,
                        "reason": str(exc)[:500]})
                try:
                    if classpath_error:
                        raise ScannerError("Classpath manifest invalid: " + classpath_error)
                    selected_cp = for_project(java_classpaths, project, base, head)
                    if not any(selected_cp.values()):
                        result = review_repo(repo, project, base, head,
                                             root / ("source-%d" % i), cache)
                    else:
                        result = review_repo(repo, project, base, head,
                                             root / ("source-%d" % i), cache,
                                             classpaths=selected_cp)
                    results.append(result)
                except (ScannerError, GitError, OSError, ValueError) as exc:
                    scan_failures.append({
                        "project": project, "base": base, "head": head,
                        "stage": "STATIC_SCAN", "error_type": type(exc).__name__,
                        "reason": str(exc)[:500]})
                    output_fn("静态扫描失败 %s：Git 底账仍保留" % project)
                comparisons.append({
                    "project": project, "origin": item["kind"],
                    "old_version": item["old_version"],
                    "new_version": item["new_version"],
                    "base_sha": base, "head_sha": head,
                    "baseline_created": created,
                    "baseline_status": snapshot["status"] if snapshot else (
                        "NOT_APPLICABLE" if item.get("role") == "EVIDENCE" else "FAILED"),
                    "candidate_status": ("EVIDENCE_ONLY" if item.get("role") == "EVIDENCE"
                                         else "SOURCE_HEAD_ONLY" if item["kind"] == "mr"
                                         else "LOCAL_COMMIT_COMPARE_NOT_PROMOTED")
                })
            candidates = stitch_cross_project(results)
            bundle, _ = write_release_bundle(
                specs, results, candidates, run_root, args.db,
                refactoringminer_bin=getattr(args, "refactoringminer_bin", None),
                codetracker_evidence=getattr(args, "codetracker_evidence", None),
                attribution_rules=getattr(args, "attribution_rules", None),
                checkpoint={"root": bundle_root, "manifest": checkpoint},
                scan_failures=scan_failures,
                project_registry=getattr(args, 'project_registry', None),
                comparisons=comparisons)
    output_fn("\n新版候选需求与证据包：" + str(bundle))
    if (bundle / 'report.html').exists():
        output_fn("人工审核 HTML 报告：" + str(bundle / 'report.html'))
    output_fn("可交给本地 AI 的完整报告：" + str(bundle / "ai_review.md"))
    output_fn("\n完成。Markdown 报告：" + str(bundle / 'report.md'))
    output_fn("JSON 报告：" + str(bundle / 'analysis.json'))
    output_fn("跨项目候选关联：%d 条（不是已证实业务依赖）" % len(candidates))
    return True
