"""Bounded discovery of protocol clues in immutable Git source trees.

This is an index of source locations, not a runtime topology. Every hint
needs a separate version-scoped identity and deployment attestation to bind.
"""
import re
from pathlib import Path

from .gitops import git, check_sha
from .source_index import source_snapshot_index, RULES_VERSION as SOURCE_INDEX_RULES_VERSION
from .review_workflow import load_batch, _hash, _utc, _write_json

PATTERNS = (
    ("DUBBO", "PROVIDER", re.compile(r"@DubboService\b|<dubbo:service\b")),
    ("DUBBO", "CONSUMER", re.compile(r"@(?:DubboReference|Reference)\b|<dubbo:reference\b")),
    ("HTTP", "SERVER", re.compile(r"@(?:GetMapping|PostMapping|PutMapping|PatchMapping|DeleteMapping|RequestMapping)\b")),
    ("HTTP", "CLIENT", re.compile(r"@FeignClient\b|\.(?:getForObject|postForObject|exchange)\s*\(")),
    ("MQ", "CONSUMER", re.compile(r"@(?:RabbitListener|KafkaListener|JmsListener)\b")),
    ("MQ", "PRODUCER", re.compile(r"\.(?:convertAndSend|send|sendAndReceive)\s*\(")),
    ("DATABASE", "READER", re.compile(r"\b(?:SELECT\s+.+?\s+FROM|@Select\b|<select\b)", re.I)),
    ("DATABASE", "WRITER", re.compile(r"\b(?:INSERT\s+INTO|UPDATE\s+[A-Za-z0-9_.]+|DELETE\s+FROM|@(?:Insert|Update|Delete)\b|<(?:insert|update|delete)\b)", re.I)),
)
EXTS = {".java", ".xml", ".yml", ".yaml", ".properties", ".sql"}
MAX_FILES = 3000
MAX_SCAN_BYTES = 20_000_000
MAX_SINGLE_BLOB = 250_000
MAX_HINTS = 10000


def discover(run_dir, repos):
    root, release = load_batch(run_dir)
    hints, omissions = [], []
    files_seen, bytes_seen = 0, 0
    index_hits, index_builds = 0, 0
    for ledger in release["ledgers"]:
        project = ledger["project"]
        if project not in repos:
            omissions.append({"project": project, "reason": "LOCAL_REPOSITORY_NOT_PROVIDED"})
            continue
        repo = Path(repos[project]).expanduser().resolve()
        for side, sha in (("BASE", ledger["base"]), ("HEAD", ledger["head"])):
            if check_sha(repo, sha) != sha:
                omissions.append({"project": project, "side": side,
                                  "reason": "PINNED_SHA_NOT_AVAILABLE"})
                continue
            index, hit = source_snapshot_index(
                repo, project, sha, root.parent / "_source_snapshot_cache",
                extensions=EXTS)
            index_hits += int(hit)
            index_builds += int(not hit)
            for gap in index["omissions"]:
                omissions.append({"project": project, "side": side, **gap})
            for file in index["files"]:
                if files_seen >= MAX_FILES or bytes_seen >= MAX_SCAN_BYTES or len(hints) >= MAX_HINTS:
                    omissions.append({"project": project, "side": side,
                                      "reason": "DISCOVERY_BUDGET_EXHAUSTED"})
                    break
                path = file["path"]
                blob = file["blob_sha"]
                files_seen += 1
                size = int(git(repo, "cat-file", "-s", blob).decode("ascii").strip())
                if size > MAX_SINGLE_BLOB:
                    omissions.append({"project": project, "side": side,
                                      "path": path, "reason": "FILE_OVERSIZE"})
                    continue
                if size + bytes_seen > MAX_SCAN_BYTES:
                    omissions.append({"project": project, "side": side,
                                      "reason": "BYTES_BUDGET_EXHAUSTED"})
                    break
                raw = git(repo, "cat-file", "blob", blob)
                if b"\x00" in raw:
                    omissions.append({"project": project, "side": side,
                                      "path": path, "reason": "NON_TEXT_SOURCE"})
                    continue
                bytes_seen += len(raw)
                for number, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
                    if len(line) > 5000:
                        omissions.append({"project": project, "side": side,
                                          "path": path, "line": number,
                                          "reason": "LONG_LINE_SKIPPED"})
                        continue
                    for protocol, role, pattern in PATTERNS:
                        # @Service from ordinary Spring is deliberately not a
                        # Dubbo declaration. Loose same-table matches aren't links.
                        if not pattern.search(line):
                            continue
                        hints.append({
                            "id": "H-" + _hash([project, sha, path, number,
                                              protocol, role])[:16],
                            "project": project, "side": side, "sha": sha,
                            "kind": protocol, "possible_role": role,
                            "source": {"path": path, "line": number,
                                       "blob_sha": blob,
                                       "excerpt": line.strip()[:500]},
                            "status": "DISCOVERY_ONLY",
                            "missing": ["FULL_PROTOCOL_IDENTITY",
                                        "ENVIRONMENT_AND_DEPLOYMENT_BINDING",
                                        "COUNTERPART_VERSION_EVIDENCE"]})
                        if len(hints) >= MAX_HINTS:
                            break
                    if len(hints) >= MAX_HINTS:
                        break
    result = {
        "schema_version": "protocol-discovery-v1",
        "created_at_utc": _utc(),
        "hint_count": len(hints),
        "hints": hints, "omissions": omissions,
        "files_visited": files_seen, "source_bytes_read": bytes_seen,
        "source_index_cache": {"hits": index_hits, "builds": index_builds,
                               "rules_version": SOURCE_INDEX_RULES_VERSION},
        "limitation": "Hints are versioned code appearances, not bound RPC, broker "
                      "messages or database objects; use bind-connections separately."
    }
    folder = root / "discovery"
    folder.mkdir(exist_ok=True)
    _write_json(folder / ("protocol-" + _hash(result)[:12] + ".json"), result)
    _write_json(root / "protocol_discovery_latest.json", result)
    return {"hints": len(hints), "omissions": len(omissions),
            "files": files_seen, "bytes": bytes_seen,
            "index_hits": index_hits, "index_builds": index_builds}
