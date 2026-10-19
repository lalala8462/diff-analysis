"""Persistent, user-maintained project catalog and pinned version snapshots (P1-B)."""
import json
import re
import sqlite3
from pathlib import Path


class BaselineError(ValueError):
    """Invalid version state or a changed, previously recorded Git baseline."""


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS catalog_meta (
  id INTEGER PRIMARY KEY CHECK (id=1),
  revision INTEGER NOT NULL
);
INSERT OR IGNORE INTO catalog_meta(id, revision) VALUES(1, 0);
CREATE TABLE IF NOT EXISTS catalog_project (
  project_path TEXT PRIMARY KEY,
  active INTEGER NOT NULL DEFAULT 1,
  owner TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS baseline_snapshot (
  project_path TEXT NOT NULL REFERENCES catalog_project(project_path),
  version_name TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  tree_sha TEXT NOT NULL,
  scanner_fingerprint TEXT NOT NULL,
  catalog_revision INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PROVISIONAL','PARTIAL','SCAN_FAILED','VERIFIED')),
  inventory_json TEXT NOT NULL,
  methods_json TEXT NOT NULL,
  calls_json TEXT NOT NULL,
  unresolved_json TEXT NOT NULL,
  warnings_json TEXT NOT NULL,
  verified_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(project_path, version_name)
);
CREATE TABLE IF NOT EXISTS mr_candidate (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_path TEXT NOT NULL REFERENCES catalog_project(project_path),
  mr_url TEXT NOT NULL,
  old_version TEXT NOT NULL,
  new_version TEXT NOT NULL,
  target_sha TEXT NOT NULL,
  diff_base_sha TEXT NOT NULL,
  source_head_sha TEXT NOT NULL,
  source_tree_sha TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('SOURCE_HEAD_ONLY','TARGET_MOVED','MERGED_UNVERIFIED')),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(project_path, mr_url, source_head_sha)
);
"""


def validate_project(value):
    if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", value):
        raise BaselineError("Expected GitLab project path like group/subgroup/repo")
    if any(x in (".", "..") for x in value.split("/")):
        raise BaselineError("Invalid project path")
    return value


def validate_version(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", value):
        raise BaselineError("Version names must use letters, digits, dot, underscore or dash")
    return value


class CatalogStore:
    def __init__(self, path):
        path = Path(path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def revision(self):
        return self.conn.execute("SELECT revision FROM catalog_meta WHERE id=1").fetchone()[0]

    def add_project(self, project, owner=""):
        project = validate_project(project)
        if not isinstance(owner, str) or len(owner) > 128:
            raise BaselineError("Invalid owner")
        with self.conn:
            existing = self.conn.execute(
                "SELECT active, owner FROM catalog_project WHERE project_path=?", (project,)
            ).fetchone()
            if existing is None:
                self.conn.execute(
                    "INSERT INTO catalog_project(project_path, owner) VALUES(?, ?)",
                    (project, owner))
            elif not existing["active"] or existing["owner"] != owner:
                self.conn.execute(
                    "UPDATE catalog_project SET active=1,owner=?,updated_at=CURRENT_TIMESTAMP "
                    "WHERE project_path=?", (owner, project))
            else:
                return self.revision()
            self.conn.execute("UPDATE catalog_meta SET revision=revision+1 WHERE id=1")
        return self.revision()

    def list_projects(self):
        return [dict(row) for row in self.conn.execute(
            "SELECT project_path,active,owner FROM catalog_project ORDER BY project_path")]

    def require_project(self, project):
        project = validate_project(project)
        row = self.conn.execute("SELECT active FROM catalog_project WHERE project_path=?",
                                (project,)).fetchone()
        if row is None or not row["active"]:
            raise BaselineError("Project not in active catalog: " + project)
        return project

    def snapshot(self, project, version):
        row = self.conn.execute(
            "SELECT * FROM baseline_snapshot WHERE project_path=? AND version_name=?",
            (project, version)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in ("inventory_json", "methods_json", "calls_json", "unresolved_json", "warnings_json"):
            result[key[:-5]] = json.loads(result.pop(key))
        return result

    def snapshots(self):
        return [dict(row) for row in self.conn.execute(
            "SELECT project_path,version_name,commit_sha,tree_sha,status,created_at,"
            "verified_at,catalog_revision FROM baseline_snapshot "
            "ORDER BY project_path,created_at,version_name")]

    def save_snapshot(self, row, refresh=False):
        project = self.require_project(row["project_path"])
        version = validate_version(row["version_name"])
        old = self.snapshot(project, version)
        if old:
            if old["commit_sha"] != row["commit_sha"] or old["tree_sha"] != row["tree_sha"]:
                raise BaselineError(
                    "BASELINE_DIVERGED: " + project + "@" + version +
                    " points to a different Git SHA/tree; manual reconciliation required")
            if (not refresh and old["scanner_fingerprint"] == row["scanner_fingerprint"]
                    and old["status"] in ("PROVISIONAL", "VERIFIED")):
                return old, False
            # Invalidate previous verification when analyzer rules change.
        with self.conn:
            self.conn.execute(
                "INSERT INTO baseline_snapshot("
                "project_path,version_name,commit_sha,tree_sha,scanner_fingerprint,"
                "catalog_revision,status,inventory_json,methods_json,calls_json,"
                "unresolved_json,warnings_json,verified_at) VALUES("
                "?,?,?,?,?,?,?,?,?,?,?,?,NULL) ON CONFLICT(project_path,version_name) "
                "DO UPDATE SET scanner_fingerprint=excluded.scanner_fingerprint,"
                "catalog_revision=excluded.catalog_revision,status=excluded.status,"
                "inventory_json=excluded.inventory_json,methods_json=excluded.methods_json,"
                "calls_json=excluded.calls_json,unresolved_json=excluded.unresolved_json,"
                "warnings_json=excluded.warnings_json,verified_at=NULL",
                (project, version, row["commit_sha"], row["tree_sha"],
                 row["scanner_fingerprint"], self.revision(), row["status"],
                 json.dumps(row["inventory"], ensure_ascii=False),
                 json.dumps(row["methods"], ensure_ascii=False),
                 json.dumps(row["calls"], ensure_ascii=False),
                 json.dumps(row["unresolved"], ensure_ascii=False),
                 json.dumps(row["warnings"], ensure_ascii=False)))
        return self.snapshot(project, version), True

    def verify(self, project, version):
        self.require_project(project)
        row = self.snapshot(project, version)
        if not row:
            raise BaselineError("Snapshot not found")
        if row["status"] not in ("PROVISIONAL", "VERIFIED") or row["warnings"]:
            raise BaselineError("Cannot verify an incomplete scan or a scan with warnings")
        with self.conn:
            self.conn.execute(
                "UPDATE baseline_snapshot SET status='VERIFIED',"
                "verified_at=CURRENT_TIMESTAMP WHERE project_path=? AND version_name=?",
                (project, version))
        return self.snapshot(project, version)

    def save_candidate(self, item):
        self.require_project(item["project_path"])
        validate_version(item["old_version"])
        validate_version(item["new_version"])
        if item["old_version"] == item["new_version"]:
            raise BaselineError("Old and new version must differ")
        with self.conn:
            self.conn.execute(
                "INSERT INTO mr_candidate(project_path,mr_url,old_version,new_version,"
                "target_sha,diff_base_sha,source_head_sha,source_tree_sha,state) "
                "VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(project_path,mr_url,source_head_sha) "
                "DO UPDATE SET state=excluded.state",
                (item["project_path"], item["mr_url"], item["old_version"],
                 item["new_version"], item["target_sha"], item["diff_base_sha"],
                 item["source_head_sha"], item["source_tree_sha"], item["state"]))
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM mr_candidate WHERE project_path=? AND mr_url=? "
            "AND source_head_sha=?", (item["project_path"], item["mr_url"],
                                    item["source_head_sha"]))][0]

    def candidates(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM mr_candidate ORDER BY id DESC")]

    def coverage(self):
        projects = self.list_projects()
        latest = self.snapshots()
        scanned = {r["project_path"] for r in latest
                   if r["status"] in ("PROVISIONAL", "PARTIAL", "VERIFIED")}
        return {
            "catalog_revision": self.revision(),
            "total_active": sum(bool(p["active"]) for p in projects),
            "projects": [{
                "project": p["project_path"],
                "active": bool(p["active"]),
                "has_snapshot": p["project_path"] in scanned
            } for p in projects]
        }
