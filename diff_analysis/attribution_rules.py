"""Exact-SHA scoped human attribution for mixed commits and complex single Hunks.

This is an audited optional decision file, not inference from a commit message.
A Hunk with multiple tasks is never forcibly assigned to one group.
"""
import json
import re
from pathlib import Path

ISSUE = re.compile(r"^[A-Z][A-Z0-9]{1,15}-[1-9]\d{0,8}$")


def apply_attribution_rules(ledger, filename):
    if not filename:
        return []
    try:
        path = Path(filename)
        if path.is_dir():
            path = path / (ledger["project"].replace("/", "_") + ".json")
        if path.stat().st_size > 2_000_000:
            raise ValueError("Attribution rules exceed size limit")
        data = json.loads(path.read_text(encoding="utf-8"))
        if (data.get("schema_version") != "attribution-rules-v1"
                or data.get("project") != ledger["project"]
                or data.get("base_sha") != ledger["base"]
                or data.get("head_sha") != ledger["head"]
                or not isinstance(data.get("decisions"), list)):
            raise ValueError("Rules are not scoped to the exact project and Git SHA pair")
        by_id = {x["id"]: x for x in ledger["final_changes"]}
        warnings = []
        seen = set()
        # Fail the entire decision file closed on malformed/ambiguous scope.
        staged = []
        for item in data["decisions"]:
            if not isinstance(item, dict):
                raise ValueError("Each decision must be an object")
            cid = item.get("diff_id")
            if cid not in by_id or cid in seen:
                raise ValueError("Unknown/duplicate diff ID in decision file")
            seen.add(cid)
            reason = item.get("reason")
            if not isinstance(reason, str) or len(reason.strip()) < 5:
                raise ValueError("Explicit review reason is required")
            keys = item.get("issue_keys")
            if not isinstance(keys, list) or not keys or len(set(keys)) != len(keys):
                raise ValueError("Provide one or several distinct issue_keys")
            if not all(isinstance(k, str) and ISSUE.fullmatch(k) for k in keys):
                raise ValueError("Invalid issue key format")
            segments = item.get("segments", [])
            if not isinstance(segments, list):
                raise ValueError("segments must be an array")
            c = by_id[cid]
            start, count = c["new_range"]["start"], c["new_range"]["count"]
            spans = []
            for segment in segments:
                if not isinstance(segment, dict):
                    raise ValueError("Invalid segment")
                a, n, issue = (segment.get("new_start"),
                               segment.get("new_count"), segment.get("issue_key"))
                if (not isinstance(a, int) or not isinstance(n, int)
                        or n <= 0 or not (start <= a and a + n <= start + count)
                        or issue not in keys):
                    raise ValueError("Line segment outside final Hunk or unknown issue")
                if any(max(a, x) < min(a + n, y) for x, y in spans):
                    raise ValueError("Attribution segments overlap")
                spans.append((a, a + n))
            if len(keys) > 1 and not segments:
                warnings.append({"type": "MIXED_HUNK_WITHOUT_SEGMENTS", "diff_id": cid})
            staged.append((c, {"issue_keys": keys, "reason": reason.strip(),
                               "segments": segments,
                               "source": "USER_REVIEW_EXACT_SHA"}))
        for change, decision in staged:
            change["manual_attribution"] = decision
            change["attribution_status"] = (
                "REVIEWED_SINGLE_ISSUE" if len(decision["issue_keys"]) == 1
                else "REVIEWED_MIXED_HUNK")
        return warnings
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return [{"type": "ATTRIBUTION_RULES_INVALID",
                 "detail": str(exc)[:220]}]
