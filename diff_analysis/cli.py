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
from .release_report import write_release_bundle


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
        child.add_argument('--db', default='.diff-analysis/state.sqlite3', help='SQLite run index (separate from baseline facts)')
    interactive = sub.add_parser('wizard', help='Interactive multi-project Git revision comparison (CMD/PowerShell)')
    interactive.add_argument('--db', default='.diff-analysis/state.sqlite3')
    interactive.add_argument('--output', default='report.md')
    interactive.add_argument('--json-output', default='review.json')
    interactive.add_argument('--run-root', default='.diff-analysis/runs')
    baseline_cli.configure(sub)
    return p


def run(args):
    from .review import review_repo
    if args.command == 'wizard':
        wizard.run_wizard(args)
        return
    if baseline_cli.handle(args):
        return
    with tempfile.TemporaryDirectory(prefix='diff-analysis-') as tmp:
        root = Path(tmp)
        cache = root / 'javac'
        results = []
        specs = []
        meta = {}
        if args.command == 'review-local':
            repo = Path(args.repo).resolve()
            project = args.project or repo.name
            results.append(review_repo(repo, project, args.base, args.head, root / 'sources-0', cache))
            specs.append((repo, project, results[-1]['base'], results[-1]['head']))
        else:
            if not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', args.token_env):
                raise ValueError('Invalid environment variable name')
            token = os.environ.get(args.token_env)
            if not token:
                raise ValueError('Missing GitLab token in ' + args.token_env)
            for i, url in enumerate(args.mr):
                mr = get_mr(url, args.allowed_host, token, args.allow_http)
                repo = clone_mr(mr, root / ('clone-%s' % i), token)
                results.append(review_repo(repo, mr['project'], mr['base'], mr['head'], root / ('sources-%s' % i), cache))
                specs.append((repo, mr['project'], results[-1]['base'], results[-1]['head']))
                meta[mr['project']] = {'url': mr['url'], 'head': mr['head']}
        for i, item in enumerate(args.catalog):
            location, sep, sha = item.rpartition('@')
            if not sep:
                raise ValueError('Expected --catalog /repo/path@<commit-sha>')
            results.append(catalog_result(Path(location).resolve(), sha, root / ('catalog-%s' % i), cache))
        candidates = stitch_cross_project(results)
        report = markdown(results, candidates, meta)
        output = Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report, encoding='utf8')
        if args.json_output:
            dest = Path(args.json_output).resolve()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps({'results': results, 'cross_candidates': candidates}, ensure_ascii=False, indent=2), encoding='utf8')
        print('Review saved:', output)
        print('Repositories:', len(results), '| Modified methods:', sum(len(r['changed_methods']) for r in results), '| Cross-project candidates:', len(candidates))
        bundle, _ = write_release_bundle(
            specs, results, candidates, args.run_root, args.db)
        print('Timestamped evidence & candidate groups:', bundle)
        print('Offline AI-ready report:', bundle / 'ai_review.md')
        print('CAUTION: Static analysis is not a proof of compatibility.')


def main():
    try:
        run(parser().parse_args())
    except (ValueError, BaselineError, GitError, GitLabError, ScannerError, OSError) as e:
        print('ERROR: ' + str(e), file=sys.stderr)
        sys.exit(2)


if __name__ == '__main__':
    main()
