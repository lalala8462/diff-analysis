"""CLI: local reproducible review or self-hosted GitLab multi-MR review."""
import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from .gitlab import get_mr, clone_mr, GitLabError
from .gitops import GitError, check_sha, files_at
from .review import review_repo, stitch_cross_project
from .scanner import ScannerError, scan_tree, build_graph
from .catalog import BaselineError, validate_project
from . import baseline_cli
from . import wizard
from .release_report import (write_release_bundle, start_ledger_checkpoint,
                             rebuild_from_checkpoint, DEFAULT_RUN_ROOT,
                             resolve_run_root)
from .review_workflow import supplement, import_review
from .review_feedback import (apply_group_feedback, import_basis_checks, review_status)
from .connection_bindings import correlate
from .semantic_evidence import collect_semantic_evidence
from .incremental_review import recompute_review
from .protocol_discovery import discover
from .auto_evidence import auto_evidence
from .contract_context import discover_contract_context
from .unified_pipeline import rebuild_unified
from .java_classpath import load_classpaths, for_project
from .architecture_sources import isolate_specs, mr_copy_path


def catalog_result(repo, sha, root, cache):
    sha = check_sha(repo, sha)
    skips = files_at(repo, sha, root)
    methods, calls, errors = scan_tree(root, cache)
    reverse, unresolved = build_graph(methods, calls)
    return {'project': Path(repo).name, 'head': sha, 'base': sha,
            'changed_files': [], 'changed_methods': [], 'methods': methods,
            'old_methods': methods, 'calls': calls, 'old_calls': calls,
            'impact_paths': {},
            'unresolved': unresolved, 'old_unresolved': unresolved,
            'old_unresolved_count': len(unresolved),
            'api_issues': [], 'warnings': skips + [e['path'] for e in errors],
            'incomplete': bool(skips or errors), 'catalog': True}


def parser():
    p = argparse.ArgumentParser(prog='diff-analysis', description='Evidence-based multi-MR impact review')
    sub = p.add_subparsers(dest='command', required=True)
    local = sub.add_parser('review-local', help='Analyze locally accessible git repo commits')
    local.add_argument('--repo', required=True)
    local.add_argument('--base', required=True, help='Exact 40/64-char base commit SHA')
    local.add_argument('--head', required=True, help='Exact 40/64-char head commit SHA')
    local.add_argument('--project', default=None)
    remote = sub.add_parser('review-mrs', help='Analyze GitLab MR URLs via API and clone')
    remote.add_argument('--mr', action='append', required=True, help='Repeat for each MR URL')
    remote.add_argument('--allowed-host', required=True, help='Exact GitLab hostname allowlist')
    remote.add_argument('--token-env', default='GITLAB_TOKEN', help='Environment variable name, not token value')
    remote.add_argument('--allow-http', action='store_true', help='Only for an explicitly trusted internal network')
    for child in (local, remote):
        child.add_argument('--catalog', action='append', default=[], help='Additional local Git repo pinned as /path/to/repo@<sha>')
        child.add_argument('--evidence', action='append', default=[],
                           help='EVIDENCE pair PROJECT=REPO_PATH@BASE_SHA@HEAD_SHA (same SHA allowed)')
        child.add_argument('--run-root', default=str(DEFAULT_RUN_ROOT), help='Timestamped evidence report directory')
        child.add_argument('--refactoringminer-bin', default=None,
                           help='Optional local RefactoringMiner executable (-bc JSON)')
        child.add_argument('--codetracker-evidence', default=None,
                           help='Optional normalized CodeTracker bridge-v1 JSON file')
        child.add_argument('--attribution-rules', default=None,
                           help='Exact-SHA Hunk attribution decisions JSON or per-project directory')
        child.add_argument('--db', default='.diff-analysis/state.sqlite3', help='SQLite run index (separate from baseline facts)')
        child.add_argument('--java-classpath-manifest', default=None,
                           help='Explicit jar SHA-256 manifest pinned per project side')
        child.add_argument('--project-registry', default=None, help='UTF-8 JSON project registry for SDK consumer checks')
    interactive = sub.add_parser('wizard', help='Interactive multi-project Git revision comparison (CMD/PowerShell)')
    interactive.add_argument('--db', default='.diff-analysis/state.sqlite3')
    interactive.add_argument('--run-root', default=str(DEFAULT_RUN_ROOT))
    interactive.add_argument('--refactoringminer-bin', default=None)
    interactive.add_argument('--codetracker-evidence', default=None)
    interactive.add_argument('--attribution-rules', default=None)
    interactive.add_argument('--java-classpath-manifest', default=None)
    interactive.add_argument('--project-registry', default=None, help='UTF-8 JSON project registry for SDK consumer checks')
    rebuild = sub.add_parser('rebuild-checkpoint',
                             help='Rebuild offline reports from a saved Git ledger (no JDK or Git reads)')
    rebuild.add_argument('--checkpoint', required=True, help='Path to ledger_checkpoint.json')
    rebuild.add_argument('--run-root', default=str(DEFAULT_RUN_ROOT))
    rebuild.add_argument('--db', default='.diff-analysis/state.sqlite3')
    fetch = sub.add_parser('supplement', help='Read bounded code at fixed project SHA')
    fetch.add_argument('--run-dir', required=True)
    fetch.add_argument('--request', required=True, help='evidence-requests-v1 JSON file')
    fetch.add_argument('--repo', action='append', required=True,
                       help='Repeat PROJECT=LOCAL_GIT_PATH; repo is validated against pinned SHA')
    review_import = sub.add_parser('import-review',
                                   help='Append manual and offline-AI decisions')
    review_import.add_argument('--run-dir', required=True)
    review_import.add_argument('--file', required=True, help='review-import-v1 JSON')
    group_edit = sub.add_parser('revise-groups',
                                 help='Apply auditable manual group/unit feedback')
    group_edit.add_argument('--run-dir', required=True)
    group_edit.add_argument('--file', required=True, help='group-feedback-v1 JSON')
    basis = sub.add_parser('import-basis-check',
                           help='Review the evidence basis of an imported decision')
    basis.add_argument('--run-dir', required=True)
    basis.add_argument('--file', required=True, help='basis-checks-v1 JSON')
    state = sub.add_parser('review-status', help='Show independent review status')
    state.add_argument('--run-dir', required=True)
    for command, schema, help_text in (
            ('bind-connections', 'connection-bindings-v1',
             'Check source-backed Dubbo/HTTP/MQ/database endpoint identities'),
            ('semantic-evidence', 'semantic-evidence-v1',
             'Extract complete indexed methods or full contract files')):
        job = sub.add_parser(command, help=help_text)
        job.add_argument('--run-dir', required=True)
        job.add_argument('--file', required=True, help=schema + ' JSON input')
        job.add_argument('--repo', action='append', required=True,
                         help='Repeat PROJECT=LOCAL_GIT_REPO')
    context = sub.add_parser('contract-context',
                             help='Find changed-field and branch clues in impacted Java methods')
    context.add_argument('--run-dir', required=True)
    context.add_argument('--repo', action='append', required=True,
                         help='PROJECT=LOCAL_GIT_REPO')
    unified = sub.add_parser('rebuild-unified',
                             help='Recompute versioned unified protocol/Java impact graph')
    unified.add_argument('--run-dir', required=True)
    auto = sub.add_parser('auto-evidence',
                          help='Auto-plan pinned full methods and API/DTO files for review questions')
    auto.add_argument('--run-dir', required=True)
    auto.add_argument('--repo', action='append', required=True,
                      help='Repeat PROJECT=LOCAL_GIT_REPO')
    discovery = sub.add_parser('discover-connections',
                               help='Search pinned Git source for protocol evidence candidates')
    discovery.add_argument('--run-dir', required=True)
    discovery.add_argument('--repo', action='append', required=True,
                           help='Repeat PROJECT=LOCAL_GIT_REPO')
    recalc = sub.add_parser('recompute-review',
                             help='Recompute impacts after manual group revision')
    recalc.add_argument('--run-dir', required=True)
    baseline_cli.configure(sub)
    return p


def run(args):
    if args.command == 'wizard':
        wizard.run_wizard(args)
        return
    if args.command == 'rebuild-unified':
        outcome = rebuild_unified(args.run_dir)
        from .revision_pipeline import update_review_package
        package = update_review_package(args.run_dir)
        print('Unified graph:', json.dumps(outcome, ensure_ascii=False))
        print('Updated review:', json.dumps(package, ensure_ascii=False))
        return
    if args.command == 'recompute-review':
        outcome = recompute_review(args.run_dir)
        print('Revision propagation:', json.dumps(outcome, ensure_ascii=False))
        return
    if args.command in ('bind-connections', 'semantic-evidence',
                        'discover-connections', 'auto-evidence',
                        'contract-context'):
        repos = {}
        for pair in args.repo:
            project, sep, path = pair.partition('=')
            if not sep or not project or not path or project in repos:
                raise ValueError('Expected unique --repo PROJECT=LOCAL_GIT_REPO')
            repos[project] = path
        if args.command == 'discover-connections':
            outcome = discover(args.run_dir, repos)
        elif args.command == 'contract-context':
            outcome = discover_contract_context(args.run_dir, repos)
        elif args.command == 'auto-evidence':
            outcome = auto_evidence(args.run_dir, repos)
        else:
            request = json.loads(Path(args.file).read_text(encoding='utf-8'))
            outcome = (correlate(args.run_dir, request, repos)
                       if args.command == 'bind-connections' else
                       collect_semantic_evidence(args.run_dir, request, repos))
        print('Offline evidence:', json.dumps(outcome, ensure_ascii=False))
        return
    if args.command == 'revise-groups':
        outcome = apply_group_feedback(args.run_dir,
                        json.loads(Path(args.file).read_text('utf-8')))
        print('Group revision:', json.dumps(outcome, ensure_ascii=False))
        return
    if args.command == 'import-basis-check':
        outcome = import_basis_checks(args.run_dir,
                        json.loads(Path(args.file).read_text('utf-8')))
        print('Basis checks:', json.dumps(outcome, ensure_ascii=False))
        return
    if args.command == 'review-status':
        outcome = review_status(args.run_dir)
        print('Review status:', json.dumps(outcome, ensure_ascii=False, indent=2))
        return
    if args.command == 'supplement':
        repos = {}
        for pair in args.repo:
            project, sep, path = pair.partition('=')
            if not sep or not project or not path or project in repos:
                raise ValueError('Expected unique --repo PROJECT=LOCAL_GIT_PATH')
            repos[project] = path
        result = supplement(args.run_dir,
                            json.loads(Path(args.request).read_text('utf-8')), repos)
        print('Evidence supplement:', json.dumps(result, ensure_ascii=False))
        return
    if args.command == 'import-review':
        result = import_review(args.run_dir,
                               json.loads(Path(args.file).read_text('utf-8')))
        print('Imported offline review:', json.dumps(result, ensure_ascii=False))
        return
    if args.command == 'rebuild-checkpoint':
        root, release = rebuild_from_checkpoint(args.checkpoint, resolve_run_root(args.run_root), args.db)
        print('Rebuilt evidence report:', root / 'report.md')
        print('Offline AI-ready report:', root / 'ai_review.md')
        print('Status:', release['status'], '(Java/static scan NOT rerun)')
        return
    if baseline_cli.handle(args):
        return
    run_root = resolve_run_root(getattr(args, 'run_root', None))
    with tempfile.TemporaryDirectory(prefix='diff-analysis-') as tmp:
        root = Path(tmp)
        cache = root / 'javac'
        results, specs, scan_failures = [], [], []
        if args.command == 'review-local':
            repo = Path(args.repo).resolve()
            project = args.project or repo.name
            # The checkpoint validates revisions and records Git errors per project.
            specs.append((repo, project, args.base, args.head))
        else:
            if not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', args.token_env):
                raise ValueError('Invalid environment variable name')
            token = os.environ.get(args.token_env)
            if not token:
                raise ValueError('Missing GitLab token in ' + args.token_env)
            for i, url in enumerate(args.mr):
                try:
                    mr = get_mr(url, args.allowed_host, token, args.allow_http)
                    repo = clone_mr(mr, mr_copy_path(mr['project']), token)
                    specs.append((repo, mr['project'], mr['base'], mr['head']))
                except (GitLabError, GitError, OSError, ValueError) as exc:
                    scan_failures.append({
                        'project': url, 'stage': 'PREPARE',
                        'error_type': type(exc).__name__,
                        'reason': str(exc)[:500]})
                    continue

        roles = {}
        for entry in getattr(args, 'evidence', []):
            project, sep, rest = entry.partition('=')
            location, delim, head = rest.rpartition('@')
            folder, delim2, base = location.rpartition('@')
            if not sep or not delim or not delim2 or not folder or not base or not head:
                raise ValueError('Expected --evidence PROJECT=REPO_PATH@BASE_SHA@HEAD_SHA')
            validate_project(project)
            if any(row[1] == project for row in specs):
                raise ValueError('Duplicate evidence project: ' + project)
            repo = Path(folder).expanduser().resolve()
            specs.append((repo, project, base, head))
            roles[(project, base, head)] = 'EVIDENCE'

        # P1-A: save independent, immutable Git evidence before compiling or
        # running the optional Java analyzer. An individual scanner failure
        # never discards the saved ledger of another project.
        specs, isolate_failures, _ = isolate_specs(specs)
        scan_failures.extend(isolate_failures)
        bundle_root, manifest = start_ledger_checkpoint(specs, run_root, roles)
        usable = {(l['project'], l['base'], l['head'])
                  for l in manifest['ledgers']}
        classpath_error = None
        try:
            java_classpaths = load_classpaths(
                getattr(args, 'java_classpath_manifest', None))
        except (ValueError, OSError) as exc:
            java_classpaths = {}
            classpath_error = str(exc)
        for i, (repo, project, base, head) in enumerate(specs):
            if (project, base, head) not in usable:
                continue
            try:
                if classpath_error:
                    raise ScannerError("Classpath manifest invalid: " + classpath_error)
                selected_cp = for_project(java_classpaths, project, base, head)
                if not any(selected_cp.values()):
                    result = review_repo(repo, project, base, head,
                                         root / ('sources-%s' % i), cache)
                else:
                    result = review_repo(repo, project, base, head,
                                         root / ('sources-%s' % i), cache,
                                         classpaths=selected_cp)
            except (ScannerError, GitError, OSError, ValueError) as exc:
                scan_failures.append({
                    'project': project, 'base': base, 'head': head,
                    'stage': 'STATIC_SCAN', 'error_type': type(exc).__name__,
                    'reason': str(exc)[:500]})
                continue
            results.append(result)

        for i, item in enumerate(args.catalog):
            location, sep, sha = item.rpartition('@')
            if not sep:
                scan_failures.append({
                    'project': item, 'stage': 'CATALOG_SCAN',
                    'error_type': 'InvalidCatalogSpec',
                    'reason': 'Expected --catalog /repo/path@<commit-sha>'})
                continue
            repo = Path(location).resolve()
            try:
                results.append(catalog_result(
                    repo, sha, root / ('catalog-%s' % i), cache))
            except (ScannerError, GitError, OSError, ValueError) as exc:
                scan_failures.append({
                    'project': repo.name, 'stage': 'CATALOG_SCAN',
                    'error_type': type(exc).__name__, 'reason': str(exc)[:500]})

        candidates = stitch_cross_project(results)
        bundle, release = write_release_bundle(
            specs, results, candidates, run_root, args.db,
            refactoringminer_bin=args.refactoringminer_bin,
            codetracker_evidence=args.codetracker_evidence,
            attribution_rules=args.attribution_rules,
            checkpoint={'root': bundle_root, 'manifest': manifest},
            scan_failures=scan_failures, project_registry=getattr(args, 'project_registry', None))
        print('Review saved:', bundle / 'report.md')
        print('Repositories scanned:', len(results),
              '| Modified methods:', sum(len(r['changed_methods']) for r in results),
              '| Cross-project candidates:', len(candidates))
        print('Timestamped evidence & candidate groups:', bundle)
        if (bundle / 'report.html').exists():
            print('Human HTML report:', bundle / 'report.html')
        print('Offline AI-ready report:', bundle / 'ai_review.md')
        if release.get('stage_failures'):
            print('WARNING: %d stage failures; see independent report.md and '
                  'ledger_checkpoint.json' % len(release['stage_failures']))
        print('CAUTION: Static analysis is not a proof of compatibility.')


def main():
    try:
        run(parser().parse_args())
    except (ValueError, BaselineError, GitError, GitLabError, ScannerError, OSError) as e:
        print('ERROR: ' + str(e), file=sys.stderr)
        sys.exit(2)


if __name__ == '__main__':
    main()
