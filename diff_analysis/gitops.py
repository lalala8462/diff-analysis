"""Git object reads and structured zero-context changes. Never executes project code."""
import os
import re
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(repo, *args, env=None):
    p = subprocess.run(["git", "-C", str(repo), "--no-pager", *args],
                       capture_output=True, env=env, check=False)
    if p.returncode:
        msg = p.stderr.decode("utf-8", "replace")[:500]
        raise GitError("git operation failed: " + msg)
    return p.stdout


def check_sha(repo, value):
    if not re.fullmatch(r"[a-f0-9]{40,64}", value, re.I):
        raise GitError("Expected pinned commit SHA, got invalid value")
    sha = git(repo, "rev-parse", "--verify", value + "^{commit}").decode().strip()
    return sha


def files_at(repo, sha, root, max_files=12000, max_size=2_000_000):
    """Export source files from Git objects, no checkout, hooks or filters.

    Only Java and selected metadata are materialized. Reject malicious paths.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    files = git(repo, "ls-tree", "-r", "-z", sha).split(b"\0")
    count = 0
    skips = []
    allow_ext = {".java", ".xml", ".properties", ".yml", ".yaml", ".sql", ".vue", ".tsx", ".ts", ".jsx", ".js", ".css", ".scss"}
    for entry in files:
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, typ, blob = metadata.decode().split()
        if typ != "blob" or mode == "120000":
            continue
        path = raw_path.decode("utf-8", "surrogateescape")
        parts = Path(path).parts
        if (not parts or Path(path).is_absolute() or ".." in parts or "\\" in path
                or any(p in {".git", ".hg"} for p in parts)):
            skips.append(path)
            continue
        if Path(path).suffix.lower() not in allow_ext:
            continue
        count += 1
        if count > max_files:
            raise GitError("Source file safety limit reached; analysis incomplete")
        data = git(repo, "cat-file", "blob", blob)
        if len(data) > max_size:
            skips.append(path + " (oversize)")
            continue
        dest = root.joinpath(*parts)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    return skips


def diff_files(repo, base, head):
    raw = git(repo, "diff", "--name-status", "-z", "--find-renames", base, head)
    tokens = raw.split(b"\0")
    out = []
    i = 0
    while i < len(tokens) and tokens[i]:
        status = tokens[i].decode("utf8", "replace")
        if status[0] in "RC":
            old, new = (tokens[i+1].decode("utf8", "replace"), tokens[i+2].decode("utf8", "replace"))
            out.append({"status": status, "path": new, "old_path": old})
            i += 3
        else:
            path = tokens[i+1].decode("utf8", "replace")
            out.append({"status": status, "path": path, "old_path": path})
            i += 2
    return out


def diff_hunks(repo, base, head, file):
    """Return changed old/new ranges. Empty ranges are zero-width insert/delete anchors."""
    raw = git(repo, "diff", "--unified=0", "--no-ext-diff", base, head, "--", file).decode("utf8", "replace")
    result = []
    for a, al, b, bl in re.findall(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", raw, re.M):
        result.append({"old_start": int(a), "old_count": int(al or 1),
                       "new_start": int(b), "new_count": int(bl or 1)})
    return result
