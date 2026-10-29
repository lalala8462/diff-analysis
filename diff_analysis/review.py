"""Change impact analysis: old/new method graph, reverse callers, API policy checks."""
from collections import defaultdict
from pathlib import Path
from .gitops import check_sha, diff_files, diff_hunks, files_at
from .scanner import scan_tree, scan_coverage, method_changes, build_graph, callers_of


def version_of(root):
    """Only direct Maven project version: inherited/property versions reported unknown."""
    pom = Path(root) / 'pom.xml'
    if not pom.exists():
        return None
    try:
        import xml.etree.ElementTree as ET
        node = ET.parse(str(pom)).getroot()
        ns = node.tag.split('}')[0] + '}' if node.tag.startswith('{') else ''
        version = node.find(ns + 'version')
        return version.text.strip() if version is not None and version.text else None
    except Exception:
        return None


def is_api_path(path):
    p = '/' + path.lower().replace('\\', '/')
    return p.endswith('api.java') or '/api/' in p


def review_repo(repo, project, base, head, workdir, cache, classpaths=None):
    base = check_sha(repo, base)
    head = check_sha(repo, head)
    changed_files = diff_files(repo, base, head)
    old_root, new_root = Path(workdir) / 'base', Path(workdir) / 'head'
    skipped_old = files_at(repo, base, old_root)
    skipped_new = files_at(repo, head, new_root)
    classpaths = classpaths or {}
    old_cp, new_cp = classpaths.get("BASE"), classpaths.get("HEAD")
    if old_cp and old_cp["classpath"]:
        old, old_calls, old_errors = scan_tree(
            old_root, cache, classpath=old_cp["classpath"])
    else:
        old, old_calls, old_errors = scan_tree(old_root, cache)
    if new_cp and new_cp["classpath"]:
        new, calls, errors = scan_tree(
            new_root, cache, classpath=new_cp["classpath"])
    else:
        new, calls, errors = scan_tree(new_root, cache)
    hunks = {}
    for f in changed_files:
        if f['path'].endswith('.java'):
            hunks[f['path']] = diff_hunks(repo, base, head, f['path'])
    changed = method_changes(old, new, hunks)
    old_reverse, old_unresolved = build_graph(old, old_calls)
    new_reverse, new_unresolved = build_graph(new, calls)
    old_impact, old_limited = callers_of(changed, old_reverse)
    new_impact, new_limited = callers_of(changed, new_reverse)
    api_changes = [f for f in changed_files if is_api_path(f['path']) or is_api_path(f['old_path'])]
    api_issues = []
    if api_changes:
        old_v, new_v = version_of(old_root), version_of(new_root)
        if old_v is None or new_v is None:
            status = 'UNKNOWN_VERSION'
        elif old_v == new_v:
            status = 'NOT_BUMPED'
        else:
            status = 'BUMPED'
        api_issues.append({'paths': [f['path'] for f in api_changes], 'status': status,
                           'old_version': old_v, 'new_version': new_v})
    return {'project': project, 'base': base, 'head': head,
            'java_coverage': {
                'BASE': {**scan_coverage(old_root, old, old_errors + (
                    [{'reason': 'IMPACT_TRAVERSAL_LIMIT'}] if old_limited else []), skipped_old), 'sha': base},
                'HEAD': {**scan_coverage(new_root, new, errors + (
                    [{'reason': 'IMPACT_TRAVERSAL_LIMIT'}] if new_limited else []), skipped_new), 'sha': head}},
            'java_classpath': {
                side: {'fingerprint': cp["fingerprint"],
                       'jar_count': cp["jar_count"], 'status': cp["status"]}
                for side, cp in classpaths.items() if cp},
            'changed_files': changed_files, 'changed_methods': sorted(changed),
            'methods': new, 'old_methods': old, 'calls': calls, 'old_calls': old_calls,
            'old_impact_paths': old_impact,
            'new_impact_paths': new_impact, 'unresolved': new_unresolved,
            'old_unresolved_count': len(old_unresolved), 'old_unresolved': old_unresolved,
            'api_issues': api_issues}


def stitch_cross_project(results):
    """Weak name/arity cross-project matches, separately per pinned side.

    EVIDENCE projects participate even if they have no changed methods.
    Matching does not prove that any RPC or class binding actually exists.
    """
    candidates = []
    for side in ("BASE", "HEAD"):
        by_target = defaultdict(list)
        for r in results:
            symbols = r.get("old_methods", {}) if side == "BASE" else r.get("methods", {})
            # Old callers can target methods modified or removed in the new tree.
            for symbol in r.get("changed_methods", []):
                m = symbols.get(symbol)
                if m:
                    by_target[(m["name"], int(m["arity"]))].append(
                        (r["project"], symbol, m["class"]))
        for r in results:
            unresolved = r.get("old_unresolved", []) if side == "BASE" else r.get("unresolved", [])
            for call in unresolved:
                for project, symbol, full_class in by_target.get(
                        (call["name"], call["arity"]), []):
                    if project == r["project"]:
                        continue
                    recv_type = call.get("receiver_type", "")
                    if recv_type and recv_type.split(".")[-1] != full_class.split(".")[-1]:
                        continue
                    candidates.append({
                        "side": side, "caller_project": r["project"],
                        "caller": call["owner"], "callee_project": project,
                        "callee": symbol, "path": call["path"], "line": call["line"],
                        "status": "BINDING_UNCONFIRMED"})
    return candidates
