"""P1-B pinned Git snapshot indexing and first-baseline creation.

Baseline is an exact target commit, not MR merge-base or a deployment claim.
"""
import hashlib
import json
import re
import tempfile
from pathlib import Path
from urllib.parse import quote

from .catalog import BaselineError
from .gitops import git, check_sha, files_at, GitError
from .gitlab import get_mr, clone_mr, gitlab_json
from .scanner import scan_tree, build_graph, ScannerError


def scanner_fingerprint():
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in ("scanner.py", "java/JavaAstScanner.java", "java/BehaviorFacts.java", "gitops.py"):
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def git_tree(repo, sha):
    return git(repo, "rev-parse", sha + "^{tree}").decode("ascii").strip()


def inventory(repo, sha):
    """All versioned Git blobs, independent of parser support and checkout filters."""
    raw = git(repo, "ls-tree", "-r", "-z", sha)
    items = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        meta, path = item.split(b"\t", 1)
        mode, kind, objid = meta.decode("ascii").split()
        filename = path.decode("utf-8", "replace")
        if kind == "blob":
            items.append({"path": filename, "object_sha": objid, "mode": mode,
                          "type": Path(filename).suffix.lower()})
        else:
            items.append({"path": filename, "object_sha": objid, "mode": mode,
                          "type": "GITLINK"})
    return items


def prepare_baseline(store, project, version, repo, commit_sha):
    """Reproducible, idempotent snapshot; divergent versions cannot overwrite."""
    project = store.require_project(project)
    sha = check_sha(repo, commit_sha)
    tree = git_tree(repo, sha)
    fp = scanner_fingerprint()
    old = store.snapshot(project, version)
    if old:
        if old["commit_sha"] != sha or old["tree_sha"] != tree:
            raise BaselineError(
                "BASELINE_DIVERGED: existing " + project + "@" + version +
                " has different Git objects; explicit reconciliation required")
        if old["scanner_fingerprint"] == fp and old["status"] in ("PROVISIONAL", "VERIFIED"):
            return old, False

    items = inventory(repo, sha)
    result = {
        "project_path": project, "version_name": version,
        "commit_sha": sha, "tree_sha": tree,
        "scanner_fingerprint": fp,
        "inventory": items, "methods": {}, "calls": [], "unresolved": [],
        "warnings": [], "status": "PROVISIONAL"
    }
    with tempfile.TemporaryDirectory(prefix="diff-baseline-") as root:
        root = Path(root)
        try:
            skipped = files_at(repo, sha, root / "source")
            methods, calls, errors = scan_tree(root / "source", root / "compiler")
            _, unresolved = build_graph(methods, calls)
            result["methods"] = methods
            result["calls"] = calls
            result["unresolved"] = unresolved
            result["warnings"] = [
                {"type": "SOURCE_SKIPPED", "reason": message} for message in skipped
            ] + [
                {"type": "PARSE_FAILED", "reason": error.get("error", "unknown"),
                 "path": error.get("path")} for error in errors
            ]
            if result["warnings"]:
                result["status"] = "PARTIAL"
        except (OSError, ScannerError, GitError) as exc:
            result["status"] = "SCAN_FAILED"
            result["warnings"].append({
                "type": "SCAN_FAILED", "reason": str(exc)[:1000]})
    return store.save_snapshot(result, refresh=bool(old))


def target_branch_sha(mr, token):
    """Check current GitLab target tip separately from diff_refs start SHA."""
    branch = mr.get("target_branch")
    if not branch:
        raise BaselineError("MR target branch is missing")
    payload = gitlab_json(
        mr["origin"],
        "projects/" + quote(mr["project"], safe="") +
        "/repository/branches/" + quote(branch, safe=""),
        token)
    sha = (payload.get("commit") or {}).get("id")
    if not isinstance(sha, str) or not re.fullmatch(r"[a-fA-F0-9]{40,64}", sha):
        raise BaselineError("Cannot verify current target branch commit SHA")
    return sha.lower()


def prepare_mr_baseline(store, url, old_version, new_version, host, token,
                        allow_http=False):
    """Build V1.0 snapshot from MR start SHA, record V1.1 as head-only candidate."""
    origin = get_mr(url, host, token, allow_http)
    project = store.require_project(origin["project"])
    if old_version == new_version:
        raise BaselineError("Old and new version must differ")
    if not origin.get("start"):
        raise BaselineError("MR diff_refs.start_sha unavailable: cannot pin old target")
    if origin.get("state") != "opened":
        raise BaselineError(
            "This command accepts open MRs only; do not derive an old target "
            "baseline from a target branch after the merge")
    branch_tip = target_branch_sha(origin, token)
    if branch_tip != origin["start"].lower():
        raise BaselineError(
            "TARGET_MOVED: MR diff_refs.start_sha does not match target branch "
            "current commit. Refresh or reconcile the MR before recording baseline.")
    with tempfile.TemporaryDirectory(prefix="diff-baseline-mr-") as folder:
        repo = clone_mr(origin, Path(folder) / "repo", token)
        snapshot, created = prepare_baseline(
            store, project, old_version, repo, origin["start"])
        candidate = store.save_candidate({
            "project_path": project, "mr_url": url,
            "old_version": old_version, "new_version": new_version,
            "target_sha": origin["start"],
            "diff_base_sha": origin["base"],
            "source_head_sha": origin["head"],
            "source_tree_sha": git_tree(repo, origin["head"]),
            "state": "SOURCE_HEAD_ONLY"
        })
    return {"snapshot": snapshot_summary(snapshot),
            "created": created,
            "candidate": candidate,
            "warning": "SOURCE_HEAD_ONLY is not a merged release. "
                       "Promotion requires verifying the actual merged Git tree."}


def snapshot_summary(row):
    data = dict(row)
    items = data.pop("inventory", [])
    data.pop("methods", None)
    data.pop("calls", None)
    unresolved = data.pop("unresolved", [])
    data["inventory_count"] = len(items)
    data["java_file_count"] = sum(x["type"] == ".java" for x in items)
    data["unresolved_call_count"] = len(unresolved)
    data["warning_count"] = len(data.get("warnings", []))
    return data
