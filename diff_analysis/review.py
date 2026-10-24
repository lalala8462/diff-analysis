"""Change impact analysis: old/new method graph, reverse callers, API policy checks."""
from collections import defaultdict
from pathlib import Path
from .gitops import check_sha, diff_files, diff_hunks, files_at
from .scanner import scan_tree, method_changes, build_graph, callers_of


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
    old, old_calls, old_errors = scan_tree(
        old_root, cache, classpath=old_cp["classpath"] if old_cp else None)
    new, calls, errors = scan_tree(
        new_root, cache, classpath=new_cp["classpath"] if new_cp else None)
    hunks = {}
    for f in changed_files:
        if f['path'].endswith('.java'):
            hunks[f['path']] = diff_hunks(repo, base, head, f['path'])
    changed = method_changes(old, new, hunks)
    old_reverse, old_unresolved = build_graph(old, old_calls)
    new_reverse, new_unresolved = build_graph(new, calls)
    combined = defaultdict(set)
    for src in (old_reverse, new_reverse):
        for target, upstreams in src.items():
            combined[target].update(upstreams)
    impact, limited = callers_of(changed, combined)
    # Separate version-bound paths for the new release evidence pipeline.
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
            'java_classpath': {
                side: {'fingerprint': cp["fingerprint"],
                       'jar_count': cp["jar_count"], 'status': cp["status"]}
                for side, cp in classpaths.items() if cp},
            'changed_files': changed_files, 'changed_methods': sorted(changed),
            'methods': new, 'old_methods': old, 'calls': calls, 'old_calls': old_calls,
            'impact_paths': impact, 'old_impact_paths': old_impact,
            'new_impact_paths': new_impact, 'unresolved': new_unresolved,
            'old_unresolved_count': len(old_unresolved), 'old_unresolved': old_unresolved,
            'api_issues': api_issues,
            'warnings': [*('old: ' + s for s in skipped_old), *('new: ' + s for s in skipped_new),
                         *('old parse: ' + x['path'] for x in old_errors),
                         *('new parse: ' + x['path'] for x in errors)] + (['impact traversal limit reached'] if limited or old_limited or new_limited else []),
            'incomplete': bool(skipped_old or skipped_new or old_errors or errors or limited or old_limited or new_limited)}


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
