"""P1-B CLI extensions: catalog and exact-SHA baseline management."""
import json
import os
import re

from .baseline import prepare_baseline, prepare_mr_baseline, snapshot_summary
from .catalog import CatalogStore, BaselineError


def configure(sub):
    a = sub.add_parser("catalog-add", help="Manually register an in-scope GitLab project")
    a.add_argument("--db", default=".diff-analysis/state.sqlite3")
    a.add_argument("--project", required=True, help="GitLab namespace/project")
    a.add_argument("--owner", default=None)

    a = sub.add_parser("catalog-list", help="Show full user-maintained project scope")
    a.add_argument("--db", default=".diff-analysis/state.sqlite3")

    a = sub.add_parser("baseline-local", help="Build a pinned baseline using a local Git repo")
    a.add_argument("--db", default=".diff-analysis/state.sqlite3")
    a.add_argument("--project", required=True)
    a.add_argument("--version", required=True, help="Old version name (e.g. V1.0)")
    a.add_argument("--repo", required=True, help="Existing local Git repo path")
    a.add_argument("--sha", required=True, help="Full old-version Commit SHA")

    a = sub.add_parser("baseline-mr", help="Baseline an opened MR target and record head candidate")
    a.add_argument("--db", default=".diff-analysis/state.sqlite3")
    a.add_argument("--mr", required=True, help="GitLab Merge Request URL")
    a.add_argument("--old-version", required=True)
    a.add_argument("--new-version", required=True)
    a.add_argument("--allowed-host", required=True)
    a.add_argument("--token-env", default="GITLAB_TOKEN")
    a.add_argument("--allow-http", action="store_true")

    a = sub.add_parser("baseline-status", help="Show snapshots, head candidates and scan gaps")
    a.add_argument("--db", default=".diff-analysis/state.sqlite3")

    a = sub.add_parser("baseline-verify", help="Acknowledge pinned baseline with zero scan warnings")
    a.add_argument("--db", default=".diff-analysis/state.sqlite3")
    a.add_argument("--project", required=True)
    a.add_argument("--version", required=True)
    a.add_argument("--acknowledge", action="store_true",
                   help="I checked project/version identity and scan scope")


COMMANDS = {
    "catalog-add", "catalog-list", "baseline-local",
    "baseline-mr", "baseline-status", "baseline-verify"
}


def handle(args):
    if args.command not in COMMANDS:
        return False
    with CatalogStore(args.db) as store:
        if args.command == "catalog-add":
            revision = store.add_project(args.project, args.owner)
            payload = {"project": args.project, "catalog_revision": revision}
        elif args.command == "catalog-list":
            payload = {"catalog_revision": store.revision(),
                       "projects": store.list_projects(), "coverage": store.coverage()}
        elif args.command == "baseline-local":
            snap, created = prepare_baseline(
                store, args.project, args.version, args.repo, args.sha)
            payload = {"snapshot": snapshot_summary(snap), "created": created}
        elif args.command == "baseline-mr":
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.token_env):
                raise BaselineError("Invalid environment variable name")
            token = os.environ.get(args.token_env)
            if not token:
                raise BaselineError("Missing GitLab token in " + args.token_env)
            payload = prepare_mr_baseline(
                store, args.mr, args.old_version, args.new_version,
                args.allowed_host, token, args.allow_http)
        elif args.command == "baseline-status":
            payload = {
                "coverage": store.coverage(),
                "snapshots": store.snapshots(),
                "candidates": store.candidates()
            }
        else:
            if not args.acknowledge:
                raise BaselineError(
                    "--acknowledge required after checking version identity and scan scope")
            snap = store.verify(args.project, args.version)
            payload = {"snapshot": snapshot_summary(snap),
                       "warning": "VERIFIED means pinned source/scan scope confirmed, "
                                  "not proven business dependency completeness."}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return True
