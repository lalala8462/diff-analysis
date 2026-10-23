"""Interactive Windows CMD/PowerShell setup and multi-project revision comparison.

Wizard gathers and confirms inputs before touching the catalog or producing reports.
"""
import json
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
from .review import review_repo, stitch_cross_project
from .report import markdown
from .release_report import write_release_bundle


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


def _local_project(input_fn):
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
    if old_sha == new_sha:
        raise BaselineError("旧版和新版指向同一个 Commit，无变化可比较")
    old_v = validate_version(_ask(
        "旧版本标签（用于后续基线复用）", _default_version(old_ref, old_sha),
        input_fn=input_fn))
    new_v = validate_version(_ask(
        "新版本标签（仅用于此次比较展示）", _default_version(new_ref, new_sha),
        input_fn=input_fn))
    if old_v == new_v:
        raise BaselineError("旧版本和新版本的标签不能相同")
    owner = _ask("项目责任人/团队备注（可选，无权限含义）", "", input_fn=input_fn)
    return {"kind": "local", "repo": repo, "project": project,
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
    output_fn("=== diff-analysis 多项目版本比较向导 ===")
    output_fn("支持本地 Git 分支/Tag/SHA，也支持未合并的 GitLab MR。输入 q 随时取消。")
    items = []
    token = None
    try:
        while True:
            kind = _ask("添加项目：1=本地 Git，2=GitLab MR，0=完成输入",
                        "1" if not items else "0", input_fn=input_fn)
            if kind == "0":
                if not items:
                    output_fn("至少添加一个项目。")
                    continue
                break
            if kind not in ("1", "2"):
                output_fn("请选择 0、1 或 2。")
                continue
            try:
                if kind == "1":
                    item = _local_project(input_fn)
                else:
                    token = token or os.environ.get("GITLAB_TOKEN")
                    if not token:
                        raise BaselineError(
                            "GitLab 模式需要 GITLAB_TOKEN；请在当前 CMD/PowerShell "
                            "设置环境变量后重新执行（不会将 Token 保存到 SQLite）")
                    item = _mr_project(input_fn, token)
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
            results = []
            specs = []
            comparisons = []
            meta = {}
            for i, item in enumerate(items):
                project = item["project"]
                output_fn("[%d/%d] 分析 %s ..." % (i + 1, len(items), project))
                _register_project(store, item)
                if item["kind"] == "local":
                    repo = item["repo"]
                    base, head = item["base"], item["head"]
                else:
                    live = get_mr(item["mr_url"], item["host"], token)
                    if (live["head"] != item["head"] or
                            live.get("start") != item["base"] or
                            live.get("state") != "opened"):
                        raise BaselineError(
                            "MR_CHANGED: 输入确认后 MR 已更新，未执行本次比较")
                    if target_branch_sha(live, token) != item["base"].lower():
                        raise BaselineError("TARGET_MOVED: MR 的目标版本已发生变化")
                    repo = clone_mr(live, root / ("clone-%d" % i), token)
                    base, head = live["start"], live["head"]
                    meta[project] = {"url": live["url"], "head": head}

                snapshot, created = prepare_baseline(
                    store, project, item["old_version"], repo, base)
                if item["kind"] == "mr":
                    store.save_candidate({
                        "project_path": project, "mr_url": item["mr_url"],
                        "old_version": item["old_version"],
                        "new_version": item["new_version"],
                        "target_sha": base, "diff_base_sha": live["base"],
                        "source_head_sha": head,
                        "source_tree_sha": git_tree(repo, head),
                        "state": "SOURCE_HEAD_ONLY"
                    })
                result = review_repo(repo, project, base, head,
                                     root / ("source-%d" % i), cache)
                results.append(result)
                specs.append((repo, project, base, head))
                comparisons.append({
                    "project": project, "origin": item["kind"],
                    "old_version": item["old_version"],
                    "new_version": item["new_version"],
                    "base_sha": base, "head_sha": head,
                    "baseline_created": created,
                    "baseline_status": snapshot["status"],
                    "candidate_status": "SOURCE_HEAD_ONLY" if item["kind"] == "mr"
                                        else "LOCAL_COMMIT_COMPARE_NOT_PROMOTED"
                })
            candidates = stitch_cross_project(results)
            report = markdown(results, candidates, meta)
            version_lines = [
                "## 本次输入的项目及修订号", "",
                "| 项目 | 旧版本 (固定 SHA) | 新版本 (固定 SHA) | 模式 |",
                "| --- | --- | --- | --- |"
            ]
            for item in comparisons:
                version_lines.append(
                    "| `%s` | %s (`%s`) | %s (`%s`) | %s |" %
                    (item["project"], item["old_version"], item["base_sha"][:12],
                     item["new_version"], item["head_sha"][:12], item["origin"]))
            report = report.replace(
                "## 一、项目与版本",
                "\n".join(version_lines) + "\n\n## 一、项目与版本", 1)
            dest = Path(args.output).expanduser().resolve()
            output_json = Path(args.json_output).expanduser().resolve()
            dest.parent.mkdir(parents=True, exist_ok=True)
            output_json.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(report, encoding="utf-8")
            bundle, _ = write_release_bundle(
                specs, results, candidates, getattr(args, "run_root", ".diff-analysis/runs"),
                args.db)
            output_json.write_text(json.dumps({
                "comparisons": comparisons, "catalog_revision": store.revision(),
                "results": results, "cross_candidates": candidates,
                "warning": "Static candidate analysis only; version heads are not "
                           "verified merged releases."
            }, ensure_ascii=False, indent=2), encoding="utf-8")
    output_fn("\n新版候选需求与证据包：" + str(bundle))
    output_fn("\n完成。Markdown 报告：" + str(dest))
    output_fn("JSON 报告：" + str(output_json))
    output_fn("跨项目候选关联：%d 条（不是已证实业务依赖）" % len(candidates))
    return True
