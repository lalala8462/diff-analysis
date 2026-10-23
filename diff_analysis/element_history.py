"""Conservative first-parent identity chains for Java refactoring events.

A chain describes continuity of refactoring evidence, not behavior equivalence.
Branching, unsupported commit parentage, duplicate matches, and intervening edits
are visible gaps. Never splice intermediate source positions onto net diff lines.
"""
import re
from .gitops import git


def _read(repo, *args):
    try:
        return git(repo, *args).decode("utf-8", "replace").strip()
    except (OSError, RuntimeError):
        return None


def _name(location):
    raw = (location or {}).get("element", "")
    matches = re.findall(r"([A-Za-z_$][\w$]*)\s*\(", raw)
    return matches[-1] if matches else None


def _same_element(new, old):
    if not new or not old or new.get("path") != old.get("path"):
        return False
    left, right = _name(new), _name(old)
    # Filename alone must never establish identity across two refactor events.
    return bool(left and right and left == right)


def _stable_gap(repo, ordered, start_idx, end_idx, path):
    """Allow intervening commits only when that file is unchanged."""
    if end_idx <= start_idx:
        return False
    for step in ordered[start_idx + 1:end_idx]:
        parent, commit = step["parent"], step["sha"]
        if step["merge"]:
            return False
        changed = _read(repo, "diff", "--name-only", parent, commit, "--", path)
        if changed is None or changed:
            return False
    return True


def _path(repo, base, head, max_commits=500):
    """A linear first-parent path with commits ordered from base to head."""
    raw = _read(repo, "rev-list", "--first-parent", "--reverse",
                "--parents", base + ".." + head)
    if raw is None:
        return None, "HISTORY_UNAVAILABLE"
    rows = [row.split() for row in raw.splitlines()]
    if len(rows) > max_commits:
        return None, "HISTORY_LIMIT"
    previous = base
    result = []
    for row in rows:
        if len(row) < 2 or row[1] != previous or len(row) > 2:
            # First-parent walk may skip base if it is only reachable via
            # a side branch. Merge commits require explicit parent mapping.
            return None, "NON_LINEAR_OR_MERGE"
        result.append({"sha": row[0], "parent": previous, "merge": False})
        previous = row[0]
    if previous != head:
        return None, "NOT_FIRST_PARENT_DESCENDANT"
    return result, "LINEAR"


def build_identity_chains(repo, base, head, events):
    """Link contiguous refactoring steps, with explicit evidence gaps.

    Records are derived from RefactoringMiner commit events, not native
    CodeTracker cross-version mappings (which already span base/head).
    """
    ordered, status = _path(repo, base, head)
    if ordered is None:
        return [], [{"type": "ELEMENT_CHAIN_UNAVAILABLE", "reason": status}]
    index = {step["sha"]: i for i, step in enumerate(ordered)}
    accepted = []
    warnings = []
    for number, ev in enumerate(events):
        if ev.get("source") != "REFACTORING_MINER":
            continue
        commit = ev.get("commit_sha")
        if commit not in index:
            warnings.append({"type": "ELEMENT_EVENT_OUTSIDE_FIRST_PARENT", "commit": commit})
            continue
        if ev.get("kind") in ("Extract Method", "Extract And Move Method"):
            # Extraction is a branch from the original method. It is not
            # replacement identity; report via evolution and purity analysis.
            continue
        accepted.append((number, index[commit], ev))
    accepted.sort(key=lambda item: (item[1], item[0]))
    used = set()
    chains = []
    for number, i, first in accepted:
        if number in used:
            continue
        path = [first]
        used.add(number)
        last_index = i
        while True:
            last = path[-1]
            possibilities = []
            for candidate_no, j, ev in accepted:
                if candidate_no in used or j <= last_index:
                    continue
                if (_same_element(last["new"], ev["old"]) and
                        _stable_gap(repo, ordered, last_index, j,
                                    last["new"]["path"])):
                    possibilities.append((candidate_no, j, ev))
            # Two equally plausible continuations are ambiguous, never
            # silently select one using array order.
            if len(possibilities) != 1:
                if len(possibilities) > 1:
                    warnings.append({
                        "type": "ELEMENT_CHAIN_AMBIGUOUS",
                        "starting_commit": first.get("commit_sha"),
                        "candidate_count": len(possibilities)})
                break
            candidate_no, j, ev = possibilities[0]
            used.add(candidate_no)
            path.append(ev)
            last_index = j
        first_index = index[path[0]["commit_sha"]]
        last_index = index[path[-1]["commit_sha"]]
        before_ok = _stable_gap(repo, ordered, -1, first_index,
                                path[0]["old"]["path"])
        after_ok = _stable_gap(repo, ordered, last_index, len(ordered),
                               path[-1]["new"]["path"])
        # Index -1 is the old base, len(ordered) means the final head
        # boundary. _stable_gap checks only the intervening commits.
        boundary_complete = bool(before_ok and after_ok)
        chains.append({
            "id": "E-%04d" % len(chains),
            "source": "REFACTORING_MINER",
            "status": "CONTIGUOUS_CANDIDATE" if boundary_complete
                      else "BOUNDARY_UNVERIFIED",
            "old": path[0]["old"], "new": path[-1]["new"],
            "steps": [{
                "kind": ev["kind"], "commit_sha": ev["commit_sha"],
                "old": ev["old"], "new": ev["new"]} for ev in path],
            "step_count": len(path),
            "boundary_checked": boundary_complete,
            "limitation": "Refactoring continuity is not behavior equivalence"
        })
    return chains, warnings


def attach_chains(ledger, chains):
    """Only attach full first-parent chains using base/head coordinates."""
    def overlap(change, location, side):
        if not location:
            return False
        key = "old_range" if side == "old" else "new_range"
        path = "old_path" if side == "old" else "path"
        r = change[key]
        return (change[path] == location["path"] and r["count"] > 0
                and location["start"] <= r["start"] + r["count"] - 1
                and location["end"] >= r["start"])

    stored = []
    for chain in chains:
        item = dict(chain)
        if chain["status"] == "CONTIGUOUS_CANDIDATE":
            item["old_change_ids"] = [c["id"] for c in ledger["final_changes"]
                                      if overlap(c, chain["old"], "old")]
            item["new_change_ids"] = [c["id"] for c in ledger["final_changes"]
                                      if overlap(c, chain["new"], "new")]
        else:
            item["old_change_ids"], item["new_change_ids"] = [], []
        item["mapping_status"] = (
            "NET_DIFF_BOTH_SIDES" if item["old_change_ids"] and item["new_change_ids"]
            else "NET_DIFF_PARTIAL_OR_UNKNOWN")
        stored.append(item)
        for change in ledger["final_changes"]:
            if change["id"] in item["old_change_ids"] + item["new_change_ids"]:
                change.setdefault("element_chain_ids", []).append(item["id"])
    ledger["element_chains"] = stored
    return stored
