"""Fixed-SHA, read-only table-usage inventory.

Matching source table names are never proof of shared database instances.
Only SQL/DDL/mapping observations overlapping changed lines seed candidates.
"""
import hashlib
import json
import os
import re
import tempfile
from collections import defaultdict
from pathlib import Path

from .gitops import git
from .source_index import source_snapshot_index

RULES_VERSION = "database-table-v1"
MAX_FILE_BYTES = 500_000
MAX_SNAPSHOT_BYTES = 20_000_000
MAX_SNAPSHOT_FILES = 4000
MAX_USES = 12000
MAX_IMPACTS = 300
MAX_USES_PER_IMPACT = 50
DYNAMIC = "$" + "{"

PART = r"(?:[" + chr(96) + r"]?[A-Za-z_][\w$]*[" + chr(96) + r"]?)"
IDENT = r"(" + PART + r"(?:\s*\.\s*" + PART + r")?)"
SQL_TABLE = re.compile(
    r"\b(FROM|JOIN|UPDATE|INTO|DELETE\s+FROM|CREATE\s+TABLE"
    r"|ALTER\s+TABLE|DROP\s+TABLE|TRUNCATE\s+TABLE)\s+"
    r"(?:IF\s+(?:NOT\s+)?EXISTS\s+)?" + IDENT, re.I)
XML_SQL = re.compile(r"<(select|insert|update|delete)\b[^>]*>(.*?)</\1\s*>", re.I | re.S)
XML_MARKUP = re.compile(r"<[^>]*>", re.S)
JAVA_STRING = r'"(?:\\.|[^"\\])*"'
JAVA_LITERAL = re.compile(JAVA_STRING)
JAVA_SEQUENCE = re.compile(r"(?:" + JAVA_STRING + r")(?:\s*\+\s*(?:" + JAVA_STRING + r"))*")
JAVA_TABLE = re.compile(r'@(?:TableName\s*\(\s*|Table\s*\(\s*name\s*=\s*)"([A-Za-z_][\w$.]*)"\s*\)')
JAVA_DS = re.compile(r'@DS\s*\(\s*"([A-Za-z_][\w-]*)"\s*\)')


def _fingerprint(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _blank(match):
    return re.sub(r"[^\n]", " ", match.group())


def _normalize(value):
    return re.sub(r"\s+", "", value).replace(chr(96), "").lower()


def _line(text, offset):
    return text.count("\n", 0, offset) + 1


def _sql_uses(fragment, path, full_text, absolute_start, method, ds_hint=None):
    result = []
    masked = re.sub(r"/\*[\s\S]*?\*/|--[^\n]*", _blank, fragment)
    for found in SQL_TABLE.finditer(masked):
        table = _normalize(found.group(2))
        if not table or table in ("select", "values", "set"):
            continue
        verb = re.sub(r"\s+", " ", found.group(1).upper())
        role = ("DDL" if "TABLE" in verb else "WRITE"
                if verb in ("UPDATE", "INTO", "DELETE FROM") else "READ")
        result.append({
            "table": table, "table_base": table.rsplit(".", 1)[-1],
            "role": role, "path": path,
            "start": _line(full_text, absolute_start + found.start()),
            "end": _line(full_text, absolute_start + len(fragment)),
            "method": method, "datasource_hint": ds_hint,
            "binding_status": "SOURCE_TABLE_TOKEN_ONLY"})
    return result


def extract_table_uses(text, path):
    """Return lexical usage observations and explicit parse gaps."""
    suffix = Path(path).suffix.lower()
    uses, omissions = [], []
    if suffix == ".sql":
        offset = 0
        for statement in text.split(";"):
            uses.extend(_sql_uses(statement, path, text, offset, "SQL_FILE"))
            offset += len(statement) + 1
        if DYNAMIC in text:
            omissions.append({"path": path, "reason": "DYNAMIC_SQL_TABLE_CANDIDATE"})
    elif suffix == ".xml":
        for m in XML_SQL.finditer(text):
            content = XML_MARKUP.sub(_blank, m.group(2))
            uses.extend(_sql_uses(content, path, text, m.start(2),
                                  "MYBATIS_" + m.group(1).upper()))
            if DYNAMIC in m.group(2):
                omissions.append({"path": path, "line": _line(text, m.start()),
                                  "reason": "DYNAMIC_MYBATIS_SQL"})
    elif suffix == ".java":
        visible = re.sub(r"/\*[\s\S]*?\*/|//[^\n]*", _blank, text)
        ds = JAVA_DS.search(visible)
        datasource_hint = ds.group(1) if ds else None
        for m in JAVA_TABLE.finditer(visible):
            name = _normalize(m.group(1))
            uses.append({
                "table": name, "table_base": name.rsplit(".", 1)[-1],
                "role": "MAPPING", "path": path,
                "start": _line(text, m.start()), "end": _line(text, m.end()),
                "method": "JAVA_TABLE_ANNOTATION",
                "datasource_hint": datasource_hint,
                "binding_status": "SOURCE_TABLE_ANNOTATION_ONLY"})
        for m in JAVA_SEQUENCE.finditer(visible):
            tokens = JAVA_LITERAL.findall(m.group())
            if not tokens:
                continue
            snippet = " ".join(token[1:-1].replace('\\"', '"') for token in tokens)
            if not re.match(
                    r"^\s*(?:SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|TRUNCATE|MERGE|WITH|FROM|JOIN|INTO)\b",
                    snippet, re.I):
                continue
            if not SQL_TABLE.search(snippet):
                if DYNAMIC in snippet and re.search(r"\b(?:FROM|JOIN|UPDATE|INTO)\b", snippet, re.I):
                    omissions.append({"path": path, "line": _line(text, m.start()),
                                      "reason": "DYNAMIC_JAVA_SQL"})
                continue
            found = _sql_uses(snippet, path, snippet, 0, "JAVA_SQL_LITERAL", datasource_hint)
            for entry in found:
                entry["start"] = _line(text, m.start())
                entry["end"] = _line(text, m.end())
            uses.extend(found)
    unique = {}
    for use in uses:
        key = (use["path"], use["start"], use["end"],
               use["table"], use["role"], use["method"])
        unique[key] = use
    return list(unique.values()), omissions


def table_snapshot(repo, project, sha, cache_dir):
    """Persist parsed observations separately from stage-zero file metadata."""
    cache_dir = Path(cache_dir)
    index, _ = source_snapshot_index(
        repo, project, sha, cache_dir / "source_indexes",
        extensions={".java", ".xml", ".sql"}, rules_version=RULES_VERSION)
    key = _fingerprint([index["index_key"], RULES_VERSION,
                        MAX_FILE_BYTES, MAX_SNAPSHOT_BYTES, MAX_SNAPSHOT_FILES, MAX_USES])
    target = cache_dir / "table_facts" / key[:2] / (key + ".json")
    if target.is_file():
        try:
            wrapper = json.loads(target.read_text("utf-8"))
            data = wrapper.get("data")
            if (isinstance(data, dict) and wrapper.get("digest") == _fingerprint(data)
                    and data.get("key") == key and data.get("sha") == index["sha"]
                    and data.get("schema_version") == RULES_VERSION
                    and isinstance(data.get("uses"), list)
                    and isinstance(data.get("omissions"), list)):
                return data, True
        except (OSError, ValueError, TypeError):
            pass
    uses, omissions = [], list(index["omissions"])
    total_bytes = 0
    for number, file in enumerate(index["files"]):
        if (number >= MAX_SNAPSHOT_FILES or len(uses) >= MAX_USES
                or total_bytes >= MAX_SNAPSHOT_BYTES):
            omissions.append({"reason": "TABLE_INDEX_BUDGET_EXHAUSTED"})
            break
        try:
            size = int(git(repo, "cat-file", "-s", file["blob_sha"]).strip())
            if size > MAX_FILE_BYTES:
                omissions.append({"path": file["path"], "reason": "TABLE_FILE_TOO_LARGE"})
                continue
            if size + total_bytes > MAX_SNAPSHOT_BYTES:
                omissions.append({"reason": "TABLE_BYTE_BUDGET_EXHAUSTED"})
                break
            raw = git(repo, "cat-file", "blob", file["blob_sha"])
            if b"\x00" in raw:
                omissions.append({"path": file["path"], "reason": "TABLE_NON_TEXT_BLOB"})
                continue
            total_bytes += len(raw)
            found, gaps = extract_table_uses(raw.decode("utf-8", "replace"), file["path"])
            omissions.extend(gaps)
            if len(uses) + len(found) > MAX_USES:
                omissions.append({"reason": "TABLE_USE_COUNT_LIMIT"})
                uses.extend(found[:MAX_USES - len(uses)])
                break
            uses.extend(found)
        except (OSError, RuntimeError, ValueError) as exc:
            omissions.append({"path": file["path"], "reason": "TABLE_BLOB_READ_FAILED",
                              "detail": type(exc).__name__})
    payload = {
        "schema_version": RULES_VERSION, "key": key,
        "project": project, "sha": index["sha"], "uses": uses,
        "omissions": omissions, "source_file_count": index["file_count"],
        "files_read_bytes": total_bytes,
        "limitation": "Source table tokens do not establish database identities"}
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=str(target.parent),
                prefix="tables-", suffix=".tmp", delete=False) as handle:
            temp = Path(handle.name)
            json.dump({"data": payload, "digest": _fingerprint(payload)},
                      handle, ensure_ascii=False, sort_keys=True)
        os.replace(str(temp), str(target))
    finally:
        if temp is not None and temp.exists():
            temp.unlink()
    return payload, False


def _touches(change, use, side):
    region = change.get("old_range" if side == "base" else "new_range", {})
    start, count = region.get("start", 0), region.get("count", 0)
    path = change.get("old_path" if side == "base" else "path")
    return (path == use["path"] and isinstance(start, int)
            and isinstance(count, int) and count > 0
            and start <= use["end"] and start + count - 1 >= use["start"])


def analyze_table_impacts(entries, ledgers, cache_dir):
    """Group changed-table candidates by source project, table and consumer."""
    snapshots, gaps, cache_hits, cache_builds = {}, [], 0, 0
    for project, entry in sorted(entries.items()):
        for side in ("base", "head"):
            sha = entry.get(side)
            if not sha:
                continue
            try:
                snap, hit = table_snapshot(entry["repo"], project, sha, cache_dir)
                snapshots[(project, side)] = snap
                cache_hits += int(hit)
                cache_builds += int(not hit)
                gaps.extend({"project": project, "side": side, **g}
                            for g in snap["omissions"])
            except (OSError, ValueError, RuntimeError) as exc:
                gaps.append({"project": project, "side": side,
                             "reason": "TABLE_SNAPSHOT_FAILED",
                             "detail": type(exc).__name__})
    consumers = defaultdict(list)
    for (project, side), snap in snapshots.items():
        if side != "head":
            continue
        for use in snap["uses"]:
            if use["role"] != "DDL":
                consumers[use["table_base"]].append((project, snap["sha"], use))
    groups = {}
    for ledger in ledgers:
        if ledger.get("role") == "EVIDENCE":
            continue
        project = ledger["project"]
        for change in ledger.get("final_changes", []):
            for side in ("base", "head"):
                snap = snapshots.get((project, side))
                if not snap:
                    continue
                for changed in snap["uses"]:
                    if not _touches(change, changed, side):
                        continue
                    for consumer, consumer_sha, used in consumers.get(changed["table_base"], []):
                        if consumer == project:
                            continue
                        key = (project, changed["table_base"], consumer)
                        if key not in groups:
                            if len(groups) >= MAX_IMPACTS:
                                gaps.append({"reason": "TABLE_IMPACT_COUNT_LIMIT"})
                                continue
                            groups[key] = {
                                "producer_project": project,
                                "consumer_project": consumer,
                                "table": changed["table_base"],
                                "producer_base_sha": ledger["base"],
                                "producer_head_sha": ledger["head"],
                                "consumer_sha": consumer_sha,
                                "change_ids": [], "producer_uses": [],
                                "consumer_uses": [],
                                "status": "TABLE_NAME_ONLY_UNCONFIRMED",
                                "limitation": "Same table name is not verified shared datasource/schema/instance"}
                        item = groups.get(key)
                        if item is None:
                            continue
                        if change["id"] not in item["change_ids"]:
                            item["change_ids"].append(change["id"])
                        source_use = {**changed, "sha": snap["sha"], "side": side}
                        if (source_use not in item["producer_uses"]
                                and len(item["producer_uses"]) < MAX_USES_PER_IMPACT):
                            item["producer_uses"].append(source_use)
                        use = {**used, "sha": consumer_sha}
                        if (use not in item["consumer_uses"]
                                and len(item["consumer_uses"]) < MAX_USES_PER_IMPACT):
                            item["consumer_uses"].append(use)
                        if (changed.get("datasource_hint") and used.get("datasource_hint")
                                and changed["datasource_hint"] != used["datasource_hint"]):
                            item["status"] = "DATASOURCE_HINTS_DIFFER_UNCONFIRMED"
                        if ("." in changed["table"] and "." in used["table"]
                                and changed["table"] != used["table"]):
                            item["status"] = "SCHEMA_HINTS_DIFFER_UNCONFIRMED"
    return {
        "schema_version": "table-impact-v1",
        "impacts": [groups[key] for key in sorted(groups)],
        "omissions": gaps,
        "cache": {"hits": cache_hits, "builds": cache_builds},
        "warning": "Only static source candidates; never verified shared database instances"}
