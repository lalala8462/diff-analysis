"""Optional, evidence-only adapters for RefactoringMiner and CodeTracker.

RefactoringMiner -bc emits commit-by-commit locations. A multi-commit location is
NOT a direct base->head mapping. CodeTracker JSON below is a normalized bridge
export, not a claim of native CodeTracker CLI support.
"""
import json
import re
import subprocess
import tempfile
from pathlib import Path

from .gitops import git

KINDS = {
    "Rename Method", "Move Method", "Move And Rename Method",
    "Extract Method", "Extract And Move Method", "Inline Method",
    "Pull Up Method", "Push Down Method", "Move Class", "Rename Class"
}
MAX_BYTES = 8 * 1024 * 1024
MAX_EVENTS = 3000


def _sha(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-fA-F]{40,64}", value))


def _location(value):
    if not isinstance(value, dict):
        return None
    path = value.get("filePath") or value.get("path")
    start = value.get("startLine") or value.get("start")
    end = value.get("endLine") or value.get("end") or start
    if not isinstance(path, str) or not path:
        return None
    path = path.replace("\\", "/")
    if (path.startswith("/") or re.match(r"^[A-Za-z]:", path)
            or ".." in path.split("/")):
        return None
    if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
        return None
    return {"path": path.replace("\\", "/"), "start": start, "end": end,
            "element": str(value.get("codeElement", ""))[:300]}


def _event(kind, old, new, source, commit=None, confidence="HISTORY_CANDIDATE", description=""):
    return {"kind": kind, "old": old, "new": new, "source": source,
            "commit_sha": commit, "confidence": confidence,
            "description": description[:500]}


def parse_refactoringminer(payload, base, head):
    """Validate public -bc JSON, do not assume intermediate lines equal final lines."""
    if not isinstance(payload, dict) or not isinstance(payload.get("commits"), list):
        raise ValueError("Invalid RefactoringMiner JSON envelope")
    events = []
    commits = payload["commits"]
    for commit in commits[:MAX_EVENTS]:
        if not isinstance(commit, dict) or not _sha(commit.get("sha1")):
            continue
        for ref in commit.get("refactorings", [])[:MAX_EVENTS]:
            if not isinstance(ref, dict) or ref.get("type") not in KINDS:
                continue
            left = [_location(x) for x in ref.get("leftSideLocations", [])]
            right = [_location(x) for x in ref.get("rightSideLocations", [])]
            left = [x for x in left if x]
            right = [x for x in right if x]
            if not left or not right:
                continue
            # A single adjacent commit's locations are directly comparable to
            # the net base/head only if that commit is exactly head.
            direct = len(commits) == 1 and commit["sha1"].lower() == head.lower()
            for old in left[:8]:
                for new in right[:8]:
                    events.append(_event(
                        ref["type"], old, new, "REFACTORING_MINER",
                        commit["sha1"], "DIRECT_COMMIT_CANDIDATE" if direct
                        else "HISTORY_CANDIDATE", ref.get("description", "")))
                    if len(events) >= MAX_EVENTS:
                        return events, [{"type": "REFACTORING_EVENTS_TRUNCATED"}]
    return events, ([{"type": "REFACTORING_COMMITS_TRUNCATED"}]
                    if len(commits) > MAX_EVENTS else [])


def run_refactoringminer(repo, base, head, executable, timeout=120):
    """Optional local CLI. No network fetch, shell, build or automatic install."""
    if not executable:
        return [], [{"type": "REFACTORING_MINER_NOT_CONFIGURED"}]
    with tempfile.TemporaryDirectory(prefix="diff-refactor-") as temp:
        path = Path(temp) / "result.json"
        try:
            proc = subprocess.run(
                [str(executable), "-bc", str(Path(repo).resolve()), base, head,
                 "-json", str(path)], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout, check=False)
            if proc.returncode != 0 or not path.is_file():
                return [], [{"type": "REFACTORING_MINER_FAILED",
                             "detail": proc.stderr[-400:]}]
            if path.stat().st_size > MAX_BYTES:
                return [], [{"type": "REFACTORING_MINER_OVERSIZE"}]
            events, warnings = parse_refactoringminer(
                json.loads(path.read_text(encoding="utf-8")), base, head)
            # RefactoringMiner -bc can return a single *refactoring* commit
            # even when other commits exist between base and head. Checking
            # that commit's actual parent prevents applying its old coordinates
            # to an unrelated net-base revision.
            for event in events:
                if event["confidence"] != "DIRECT_COMMIT_CANDIDATE":
                    continue
                try:
                    parents = git(repo, "rev-list", "--parents", "-n", "1",
                                  event["commit_sha"]).decode().split()
                except (OSError, RuntimeError):
                    parents = []
                if len(parents) != 2 or parents[1] != base:
                    event["confidence"] = "HISTORY_CANDIDATE"
                    warnings.append({
                        "type": "DIRECT_LOCATION_DOWNGRADED",
                        "commit": event["commit_sha"]})
            return events, warnings
        except (OSError, subprocess.TimeoutExpired, ValueError, UnicodeError) as exc:
            return [], [{"type": "REFACTORING_MINER_UNAVAILABLE",
                         "detail": type(exc).__name__}]


def load_codetracker_bridge(source, project, base, head):
    """Read explicitly normalized CodeTracker output; never guess native schema.

    bridge v1:
      {"schema_version":"codetracker-bridge-v1",
       "project":"rhf/payment", "base_sha":"...", "head_sha":"...",
       "mappings":[{"kind":"Move Method","old":{"path":"A.java","start":5,
       "end":10},"new":{"path":"B.java","start":8,"end":13}}]}
    """
    if not source:
        return [], []
    try:
        path = Path(source)
        if path.is_dir():
            path = path / (project.replace("/", "_") + ".json")
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("Bridge file exceeds size cap")
        data = json.loads(path.read_text(encoding="utf-8"))
        if (data.get("schema_version") != "codetracker-bridge-v1" or
                data.get("project") != project or
                data.get("base_sha") != base or data.get("head_sha") != head or
                not isinstance(data.get("mappings"), list)):
            raise ValueError("Bridge identity/schema does not match pinned versions")
        events = []
        for mapping in data["mappings"][:MAX_EVENTS]:
            old = _location(mapping.get("old"))
            new = _location(mapping.get("new"))
            if old and new and mapping.get("kind") in KINDS:
                events.append(_event(mapping["kind"], old, new,
                                     "CODETRACKER_BRIDGE", confidence="EXTERNAL_TRACKED_CANDIDATE",
                                     description=mapping.get("description", "")))
        return events, ([{"type": "CODETRACKER_EVENTS_TRUNCATED"}]
                        if len(data["mappings"]) > MAX_EVENTS else [])
    except (OSError, TypeError, ValueError, UnicodeError) as exc:
        return [], [{"type": "CODETRACKER_BRIDGE_INVALID",
                     "detail": str(exc)[:250]}]


def _overlaps(change, location, side):
    if location is None:
        return False
    path = change["old_path"] if side == "old" else change["path"]
    r = change["old_range"] if side == "old" else change["new_range"]
    if r["count"] == 0:
        # Pure Git R100 has no textual Hunk but still has a file-level event.
        return change["file_status"].startswith("R") and location["path"] == path
    return location["path"] == path and (
        location["start"] <= r["start"] + r["count"] - 1 and
        location["end"] >= r["start"])


def attach_evolution(ledger, events):
    """Attach evidence without modifying net-Diff IDs, paths or the old/new SHA."""
    relations = []
    for index, event in enumerate(events):
        if event["confidence"] in ("DIRECT_COMMIT_CANDIDATE", "EXTERNAL_TRACKED_CANDIDATE"):
            old_ids = [c["id"] for c in ledger["final_changes"]
                       if _overlaps(c, event["old"], "old")]
            new_ids = [c["id"] for c in ledger["final_changes"]
                       if _overlaps(c, event["new"], "new")]
        else:
            # Multi-commit intermediate line numbers must NOT be compared with
            # base/head line numbers. Only paths are safe as a weak hint.
            old_ids = [c["id"] for c in ledger["final_changes"]
                       if c["old_path"] == event["old"]["path"]]
            new_ids = [c["id"] for c in ledger["final_changes"]
                       if c["path"] == event["new"]["path"]]
        old_ids, new_ids = old_ids[:100], new_ids[:100]
        if not old_ids and not new_ids:
            continue
        record = dict(event)
        record["id"] = "R-%s-%04d" % (ledger["project"].replace("/", "_"), index)
        record["old_change_ids"] = old_ids
        record["new_change_ids"] = new_ids
        record["mapping_status"] = (
            "BOTH_SIDES_MATCHED" if old_ids and new_ids else "PARTIAL_MATCH")
        relations.append(record)
        for c in ledger["final_changes"]:
            if c["id"] in old_ids or c["id"] in new_ids:
                c.setdefault("evolution_ids", []).append(record["id"])
                c.setdefault("evolution_types", []).append(event["kind"])
    ledger["evolution_relations"] = relations
    return relations
