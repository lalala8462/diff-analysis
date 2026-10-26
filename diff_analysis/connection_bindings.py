"""Audited cross-project connection assertions for pinned source snapshots.

A Java annotation, SQL table name, URL or broker destination alone cannot
prove deployment wiring. Both endpoint source evidence AND a version-scoped
human-supplied environment/binding assertion are required; output remains
USER_ATTESTED, never RUNTIME_VERIFIED.
"""
import json
import re
from collections import defaultdict
from pathlib import Path

from .gitops import git, safe_source_path, check_sha
from .review_workflow import load_batch, _hash, _utc, _write_json

KINDS = {
    "DUBBO": ("interface", "group", "version", "registry", "service_id"),
    "HTTP": ("method", "path", "target_service", "environment", "service_id"),
    "MQ": ("broker", "destination", "route", "environment", "service_id"),
    "DATABASE": ("datasource", "schema", "table", "environment", "service_id"),
}
PRODUCERS = {"DUBBO": {"PROVIDER"}, "HTTP": {"SERVER"},
             "MQ": {"PRODUCER"}, "DATABASE": {"WRITER"}}
CONSUMERS = {"DUBBO": {"CONSUMER"}, "HTTP": {"CLIENT"},
             "MQ": {"CONSUMER"}, "DATABASE": {"READER", "WRITER"}}
MAX_OBSERVATIONS = 5000
MAX_SOURCE_BYTES = 1_000_000


def _valid_identity(kind, identity):
    if not isinstance(identity, dict) or set(identity) != set(KINDS[kind]):
        return False
    return all(isinstance(v, str) and v and len(v) <= 256
               and not any(x in v for x in ("\n", "\r", "\x00"))
               and not re.fullmatch(r"(?i)(UNKNOWN|DEFAULT|ANY|\*)", v)
               for v in identity.values())


def _evidence_text(repo, sha, evidence):
    if not isinstance(evidence, dict):
        raise ValueError("Missing pinned code evidence")
    path, start, end = evidence.get("path"), evidence.get("start"), evidence.get("end")
    terms = evidence.get("contains")
    if (not isinstance(path, str) or not safe_source_path(path) or
            not isinstance(start, int) or isinstance(start, bool) or
            not isinstance(end, int) or isinstance(end, bool) or
            start < 1 or start > end or end - start > 99 or
            not isinstance(terms, list) or not terms or len(terms) > 10 or
            not all(isinstance(x, str) and len(x) >= 2 and len(x) <= 200
                    for x in terms)):
        raise ValueError("Invalid evidence path, bounded lines or literals")
    object_key = sha + ":" + path
    size = int(git(repo, "cat-file", "-s", object_key).decode("ascii").strip())
    if size > MAX_SOURCE_BYTES:
        raise ValueError("Source blob too large for bounded proof")
    data = git(repo, "show", object_key)
    if b"\x00" in data:
        raise ValueError("Binary source is not admissible evidence")
    lines = data.decode("utf-8", "replace").splitlines()
    if start > len(lines):
        raise ValueError("Evidence line out of source range")
    excerpt = "\n".join(lines[start - 1:min(end, len(lines))])
    if any(term not in excerpt for term in terms):
        raise ValueError("Expected evidence literals absent from pinned source")
    return {
        "path": path, "start": start, "end": end,
        "sha256": _hash(excerpt.encode("utf-8")),
        "source_blob_sha": git(repo, "rev-parse", sha + ":" + path).decode().strip(),
        "excerpt": excerpt[:5000],
        "contains": terms}


def _provenance(entry, root_scope, repos):
    project, side = entry.get("project"), entry.get("side")
    if side not in ("BASE", "HEAD") or project not in root_scope:
        raise ValueError("Observation outside selected project or version side")
    spec = root_scope[project]
    sha = spec["base"] if side == "BASE" else spec["head"]
    if entry.get("sha") != sha:
        raise ValueError("Observation must pin the exact batch SHA")
    if project not in repos:
        raise ValueError("Local Git repo must be explicitly supplied for each project")
    repo = Path(repos[project]).expanduser().resolve()
    if check_sha(repo, sha) != sha:
        raise ValueError("Unable to verify exact pinned commit")
    return project, side, sha, repo


def correlate(run_dir, request, repos):
    root, release = load_batch(run_dir)
    observations = request.get("observations")
    if (request.get("schema_version") != "connection-bindings-v1" or
            not isinstance(observations, list) or
            not 0 < len(observations) <= MAX_OBSERVATIONS):
        raise ValueError("Expected connection-bindings-v1 with 1..5000 observations")
    scope = {l["project"]: l for l in release["ledgers"]}
    verified = []
    for entry in observations:
        if not isinstance(entry, dict):
            raise ValueError("Observation must be an object")
        kind, role = entry.get("kind"), entry.get("role")
        if kind not in KINDS or role not in PRODUCERS[kind] | CONSUMERS[kind]:
            raise ValueError("Unsupported protocol or endpoint role")
        ident = entry.get("identity")
        if not _valid_identity(kind, ident):
            raise ValueError("Full protocol identity and environment must be explicit")
        project, side, sha, repo = _provenance(entry, scope, repos)
        evidence = _evidence_text(repo, sha, entry.get("evidence"))
        anchor = {"DUBBO": ident["interface"].split(".")[-1],
                  "HTTP": ident["path"], "MQ": ident["destination"],
                  "DATABASE": ident["table"]}[kind]
        if anchor not in evidence["excerpt"]:
            raise ValueError("Protocol identity not visible in pinned source evidence")
        binding = entry.get("deployment_binding")
        # Declared scope is separate from the source facts. It remains an
        # attestation even with an exact source match.
        attested = bool(isinstance(binding, dict) and
                        isinstance(binding.get("author"), str) and
                        len(binding["author"].strip()) >= 2 and
                        isinstance(binding.get("environment"), str) and
                        bool(binding["environment"].strip()) and
                        binding["environment"] not in ("UNKNOWN", "*") and
                        binding.get("environment") == ident.get(
                            "environment", binding.get("environment")) and
                        binding.get("service_id") == ident["service_id"] and
                        isinstance(binding.get("reason"), str) and
                        len(binding["reason"].strip()) >= 8)
        item = {
            "id": "EP-" + _hash([project, side, sha, kind, role, ident,
                                  evidence["source_blob_sha"], evidence["start"]])[:16],
            "project": project, "side": side, "sha": sha, "kind": kind,
            "role": role, "identity": ident, "evidence": evidence,
            "binding_status": "USER_ATTESTED" if attested else "SOURCE_ONLY",
            "deployment_binding": binding if attested else None,
            "limitation": "Source + user attestation is not verified deployment traffic"
        }
        verified.append(item)
    edges, unresolved = [], []
    by_side_kind = defaultdict(list)
    for endpoint in verified:
        by_side_kind[(endpoint["side"], endpoint["kind"])].append(endpoint)
    for (side, kind), endpoints in sorted(by_side_kind.items()):
        provider_roles = PRODUCERS[kind]
        for a in endpoints:
            if a["role"] not in CONSUMERS[kind]:
                continue
            matched = []
            for b in endpoints:
                if a["id"] == b["id"] or a["project"] == b["project"]:
                    continue
                if b["role"] not in provider_roles:
                    continue
                # Complete normalized contract and environment match; source
                # is separately pinned on each endpoint. No cross-side mixing.
                if a["identity"] != b["identity"]:
                    continue
                # The Dubbo registry may be shared by environments; the
                # independently attested deployment scopes must match.
                a_binding = a.get("deployment_binding") or {}
                b_binding = b.get("deployment_binding") or {}
                if (a_binding and b_binding and
                        a_binding.get("environment") != b_binding.get("environment")):
                    continue
                matched.append(b)
            if not matched:
                unresolved.append({"endpoint_id": a["id"], "side": side,
                                   "kind": kind, "status": "NO_PROVEN_TARGET",
                                   "reason": "No selected provider with identical explicit identity"})
                continue
            for b in matched:
                allowed = (a["binding_status"] == "USER_ATTESTED" and
                           b["binding_status"] == "USER_ATTESTED")
                edges.append({
                    "id": "CON-" + _hash([a["id"], b["id"], side])[:16],
                    "kind": kind, "side": side, "caller_endpoint": a["id"],
                    "callee_endpoint": b["id"], "caller_project": a["project"],
                    "caller_sha": a["sha"], "callee_project": b["project"],
                    "callee_sha": b["sha"],
                    "binding_status": ("USER_ATTESTED_MATCH" if allowed else
                                       "BINDING_UNCONFIRMED"),
                    "identity": a["identity"],
                    "evidence_refs": [a["id"], b["id"]],
                    "missing": ([] if allowed else
                                ["BOTH_ENDPOINT_DEPLOYMENT_ATTESTATIONS"]),
                    "limitation": "Not runtime proof; verify actual deployed versions and configuration"
                })
    # A contract connection can be associated with a changed group only
    # where versioned source ranges overlap. Never infer that every change in
    # an owning project altered that interface or table.
    by_endpoint = {x["id"]: x for x in verified}
    change_owner = {
        cid: group["id"] for group in release.get("groups", [])
        for cid in group.get("change_ids", [])}
    linked_changes, unlocated = [], []
    for edge in edges:
        endpoint = by_endpoint[edge["callee_endpoint"]]
        evidence = endpoint["evidence"]
        matched = set()
        for ledger in release["ledgers"]:
            if ledger["project"] != endpoint["project"]:
                continue
            for change in ledger["final_changes"]:
                cid = change["id"]
                field = "new_range" if edge["side"] == "HEAD" else "old_range"
                location = change.get(field) or {}
                path = change.get("path" if edge["side"] == "HEAD" else "old_path")
                count, start = location.get("count", 0), location.get("start", 0)
                if (cid not in change_owner or path != evidence["path"] or
                        not isinstance(start, int) or
                        not isinstance(count, int) or count < 1):
                    continue
                if (start <= evidence["end"] and
                        start + count - 1 >= evidence["start"]):
                    matched.add((change_owner[cid], cid))
        for group_id, cid in sorted(matched):
            linked_changes.append({
                "id": "CI-" + _hash([edge["id"], group_id, cid])[:16],
                "group_id": group_id, "change_id": cid,
                "connection_id": edge["id"], "side": edge["side"],
                "provider_project": edge["callee_project"],
                "consumer_project": edge["caller_project"],
                "consumer_endpoint": edge["caller_endpoint"],
                "status": "REVIEW_REQUIRED",
                "binding_status": edge["binding_status"],
                "limitation": "Exact source overlap does not prove changed runtime behavior"})
        if not matched:
            unlocated.append({
                "connection_id": edge["id"], "side": edge["side"],
                "reason": "PROVIDER_CHANGE_NOT_LOCATED_AT_EVIDENCE_RANGE",
                "limitation": "This does not rule out a related behavioral change"})
    result = {
        "schema_version": "connection-bindings-result-v1",
        "source_analysis_fingerprint": _hash([
            (x["project"], x["base"], x["head"]) for x in release["ledgers"]]),
        "created_at_utc": _utc(), "endpoints": verified, "edges": edges,
        "cross_project_impact_candidates": linked_changes,
        "impact_mapping_gaps": unlocated,
        "unresolved": unresolved,
        "coverage": {"endpoints": len(verified), "edges": len(edges),
                     "not_bound": sum(x["binding_status"] != "USER_ATTESTED_MATCH"
                                      for x in edges)},
        "warning": "Runtime bindings are not automatically verified; absence of "
                   "a matching observation does not imply no dependency"}
    folder = root / "bindings"
    folder.mkdir(exist_ok=True)
    # Immutable revision history; no overwriting prior user evidence.
    previous = sorted(folder.glob("bindings-*.json"))
    revision = len(previous) + 1
    _write_json(folder / ("bindings-%03d.json" % revision), result)
    _write_json(root / "bindings_latest.json", result)
    # Binding is a separate revision. Rebuild only derived graph/impact;
    # immutable code and Git ledgers are never mutated.
    from .unified_pipeline import rebuild_unified
    unified = rebuild_unified(root, binding_report=result)
    package = None
    if (root / "questions.json").is_file():
        from .revision_pipeline import update_review_package
        package = update_review_package(root)
    return {"unified": unified, "review_package": package,
            "revision": revision, "endpoints": len(verified),
            "edges": len(edges), "unresolved": len(unresolved),
            "linked_change_candidates": len(linked_changes),
            "binding_status": "SOURCE_AND_ATTESTATION_ONLY"}
