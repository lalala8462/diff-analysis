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
from .report import markdown
from .scanner import ScannerError, scan_tree, build_graph
from .catalog import BaselineError
from . import baseline_cli
from . import wizard
from .release_report import (write_release_bundle, start_ledger_checkpoint,
                             rebuild_from_checkpoint)


def catalog_result(repo, sha, root, cache):
    sha = check_sha(repo, sha)
    skips = files_at(repo, sha, root)
    methods, calls, errors = scan_tree(root, cache)
    reverse, unresolved = build_graph(methods, calls)
    return {'project': Path(repo).name, 'head': sha, 'base': sha,
            'changed_files': [], 'changed_methods': [], 'methods': methods,
            'old_methods': {}, 'calls': calls, 'impact_paths': {},
            'unresolved': unresolved, 'old_unresolved_count': 0,
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
        child.add_argument('--output', default='report.md')
        child.add_argument('--json-output', default=None)
        child.add_argument('--catalog', action='append', default=[], help='Additional local Git repo pinned as /path/to/repo@<sha>')
        child.add_argument('--run-root', default='.diff-analysis/runs', help='Timestamped evidence report directory')
        child.add_argument('--refactoringminer-bin', default=None,
                           help='Optional local RefactoringMiner executable (-bc JSON)')
        child.add_argument('--codetracker-evidence', default=None,
                           help='Optional normalized CodeTracker bridge-v1 JSON file')
        child.add_argument('--attribution-rules', default=None,
                           help='Exact-SHA Hunk attribution decisions JSON or per-project directory')
        child.add_argument('--db', default='.diff-analysis/state.sqlite3', help='SQLite run index (separate from baseline facts)')
    interactive = sub.add_parser('wizard', help='Interactive multi-project Git revision comparison (CMD/PowerShell)')
    interactive.add_argument('--db', default='.diff-analysis/state.sqlite3')
    interactive.add_argument('--output', default='report.md')
    interactive.add_argument('--json-output', default='review.json')
    interactive.add_argument('--run-root', default='.diff-analysis/runs')
    interactive.add_argument('--refactoringminer-bin', default=None)
    interactive.add_argument('--codetracker-evidence', default=None)
    interactive.add_argument('--attribution-rules', default=None)
    rebuild = sub.add_parser('rebuild-checkpoint',
                             help='Rebuild offline reports from a saved Git ledger (no JDK or Git reads)')
    rebuild.add_argument('--checkpoint', required=True, help='Path to ledger_checkpoint.json')
    rebuild.add_argument('--run-root', default='.diff-analysis/runs')
    rebuild.add_argument('--db', default='.diff-analysis/state.sqlite3')
    baseline_cli.configure(sub)
    return p


def run(args):
    if args.command == 'wizard':
        wizard.run_wizard(args)
        return
    if args.command == 'rebuild-checkpoint':
        root, release = rebuild_from_checkpoint(args.checkpoint, args.run_root, args.db)
        print('Rebuilt evidence report:', root / 'report.md')
        print('Offline AI-ready report:', root / 'ai_review.md')
        print('Status:', release['status'], '(Java/static scan NOT rerun)')
        return
    if baseline_cli.handle(args):
        return
    with tempfile.TemporaryDirectory(prefix='diff-analysis-') as tmp:
        root = Path(tmp)
        cache = root / 'javac'
        results, specs, meta, scan_failures = [], [], {}, []
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
                mr = get_mr(url, args.allowed_host, token, args.allow_http)
                repo = clone_mr(mr, root / ('clone-%s' % i), token)
                specs.append((repo, mr['project'], mr['base'], mr['head']))
                meta[mr['project']] = {'url': mr['url'], 'head': mr['head']}

        # P1-A: save independent, immutable Git evidence before compiling or
        # running the optional Java analyzer. An individual scanner failure
        # never discards the saved ledger of another project.
        bundle_root, manifest = start_ledger_checkpoint(specs, args.run_root)
        usable = {(l['project'], l['base'], l['head'])
                  for l in manifest['ledgers']}
        for i, (repo, project, base, head) in enumerate(specs):
            if (project, base, head) not in usable:
                continue
            try:
                result = review_repo(repo, project, base, head,
                                     root / ('sources-%s' % i), cache)
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
        report = markdown(results, candidates, meta)
        if manifest['failures'] or scan_failures:
            report += ('\n\n## 阶段失败（请优先阅读独立运行目录）\n\n'
                       '部分扫描失败，不能据此认定没有影响；'
                       'Git 底账与阶段状态保存在 ledger_checkpoint.json'
                       ' 和独立 report.md 中。\n')
        output = Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report, encoding='utf8')
        if args.json_output:
            dest = Path(args.json_output).resolve()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps({
                'results': results, 'cross_candidates': candidates,
                'stage_failures': manifest['failures'] + scan_failures,
            }, ensure_ascii=False, indent=2), encoding='utf8')
        bundle, release = write_release_bundle(
            specs, results, candidates, args.run_root, args.db,
            refactoringminer_bin=args.refactoringminer_bin,
            codetracker_evidence=args.codetracker_evidence,
            attribution_rules=args.attribution_rules,
            checkpoint={'root': bundle_root, 'manifest': manifest},
            scan_failures=scan_failures)
        print('Review saved:', output)
        print('Repositories scanned:', len(results),
              '| Modified methods:', sum(len(r['changed_methods']) for r in results),
              '| Cross-project candidates:', len(candidates))
        print('Timestamped evidence & candidate groups:', bundle)
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
