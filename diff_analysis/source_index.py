"""Immutable, version-scoped source inventory shared by future resource scanners.

This index records Git object metadata, not resolved calls, contracts or runtime
connections. Cache keys include the pinned commit, scan rules and selection
limits; incomplete inventories keep explicit omissions.
"""
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from .gitops import git, check_sha, safe_source_path


SCHEMA_VERSION = "source-snapshot-index-v1"
RULES_VERSION = "source-tree-selection-v2"
SELECTION_REVISION = "portable-source-selection-v2"
SOURCE_EXTENSIONS = frozenset({
    ".java", ".xml", ".sql", ".yml", ".yaml", ".properties", ".json"
})
MAX_INDEX_FILES = 50000
SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _valid_cache(record, key, project, sha, extensions, limit):
    if not isinstance(record, dict):
        return False
    payload = record.get("payload")
    if not isinstance(payload, dict) or record.get("digest") != _digest(payload):
        return False
    if (payload.get("index_key") != key or payload.get("project") != project or
            payload.get("sha") != sha or payload.get("schema_version") != SCHEMA_VERSION or
            payload.get("selection_revision") != SELECTION_REVISION or
            payload.get("extensions") != extensions or payload.get("max_files") != limit or
            not isinstance(payload.get("truncated"), bool) or
            not isinstance(payload.get("omissions"), list)):
        return False
    files = payload.get("files")
    if (not isinstance(files, list) or len(files) > limit
            or payload.get("file_count") != len(files)):
        return False
    if (payload["truncated"] and not any(
            isinstance(gap, dict) and gap.get("reason") == "SOURCE_INDEX_FILE_LIMIT"
            for gap in payload["omissions"])):
        return False
    return all(isinstance(item, dict) and
               isinstance(item.get("path"), str) and
               safe_source_path(item["path"], windows=True) and
               Path(item["path"]).suffix.lower() in extensions and
               isinstance(item.get("blob_sha"), str) and
               bool(SHA_RE.fullmatch(item["blob_sha"])) and
               item.get("category") in ("JAVA", "XML", "SQL", "CONFIG")
               for item in files)


def _category(path):
    suffix = Path(path).suffix.lower()
    return {".java": "JAVA", ".xml": "XML", ".sql": "SQL"}.get(suffix, "CONFIG")


def _inventory(repo, project, sha, key, extensions, limit):
    raw = git(repo, "ls-tree", "-r", "-z", sha)
    files = []
    skipped_paths = 0
    skipped_links = 0
    truncated = False
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        try:
            metadata, name = entry.split(b"\t", 1)
            mode, kind, blob_sha = metadata.decode("ascii").split()
            path = name.decode("utf-8")
        except (ValueError, UnicodeError):
            skipped_paths += 1
            continue
        if kind != "blob":
            continue
        if mode == "120000":
            if Path(path).suffix.lower() in extensions:
                skipped_links += 1
            continue
        if Path(path).suffix.lower() not in extensions:
            continue
        # Apply a portable path policy: an index made on Linux must not later
        # cause source materialization failures on Windows.
        if not safe_source_path(path, windows=True):
            skipped_paths += 1
            continue
        if not SHA_RE.fullmatch(blob_sha):
            skipped_paths += 1
            continue
        if len(files) >= limit:
            truncated = True
            break
        files.append({"path": path, "blob_sha": blob_sha,
                      "category": _category(path)})
    omissions = []
    if skipped_paths:
        omissions.append({"reason": "UNSAFE_OR_UNDECODABLE_SOURCE_PATH",
                          "count": skipped_paths})
    if skipped_links:
        omissions.append({"reason": "SYMLINK_SOURCE_NOT_INDEXED",
                          "count": skipped_links})
    if truncated:
        omissions.append({"reason": "SOURCE_INDEX_FILE_LIMIT",
                          "max_files": limit})
    return {"schema_version": SCHEMA_VERSION,
            "selection_revision": SELECTION_REVISION,
            "index_key": key,
            "project": project, "sha": sha,
            "extensions": extensions, "max_files": limit,
            "files": files, "file_count": len(files),
            "truncated": truncated, "omissions": omissions,
            "limitation": "Git file metadata only; no parsed resource uses or runtime binding"}


def source_snapshot_index(repo, project, sha, cache_dir, *,
                          extensions=SOURCE_EXTENSIONS,
                          rules_version=RULES_VERSION,
                          max_files=MAX_INDEX_FILES):
    """Return (index, cache_hit) for one immutable Git commit.

    The cache is advisory. Corrupt, incompatible or incomplete entries never
    silently masquerade as complete inventories.
    """
    if not isinstance(project, str) or not project.strip():
        raise ValueError("Project must be a nonempty name")
    if not isinstance(rules_version, str) or not rules_version.strip():
        raise ValueError("Scanner rules_version must be nonempty")
    if not isinstance(max_files, int) or isinstance(max_files, bool) or not 0 < max_files <= MAX_INDEX_FILES:
        raise ValueError("Invalid source index file budget")
    if (not isinstance(extensions, (set, frozenset, list, tuple)) or
            not extensions or
            not all(isinstance(x, str) and re.fullmatch(r"\.[a-z0-9]+", x) for x in extensions)):
        raise ValueError("Expected lowercase source suffixes")
    chosen = sorted(set(extensions))
    sha = check_sha(repo, sha)
    key = _digest([SCHEMA_VERSION, SELECTION_REVISION,
                   project, sha, rules_version,
                   chosen, max_files])
    target = Path(cache_dir) / key[:2] / (key + ".json")
    if target.is_file():
        try:
            record = json.loads(target.read_text(encoding="utf-8"))
            if _valid_cache(record, key, project, sha, chosen, max_files):
                return record["payload"], True
        except (OSError, ValueError, TypeError):
            pass
    payload = _inventory(repo, project, sha, key, chosen, max_files)
    target.parent.mkdir(parents=True, exist_ok=True)
    record = {"payload": payload, "digest": _digest(payload)}
    # A temp file in the same directory allows atomic replacement on both
    # Windows and Linux. Competing readers never observe partial JSON.
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".tmp",
                                         prefix="index-", dir=str(target.parent),
                                         delete=False) as handle:
            temp_path = Path(handle.name)
            json.dump(record, handle, ensure_ascii=False, sort_keys=True)
        os.replace(str(temp_path), str(target))
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
    return payload, False
