"""Version-scoped Redis and reflection/Spring name evidence.

Only source-side candidates are created; no Redis instance, Spring container or
runtime invocation is inspected. Cached facts are immutable per commit/rules.
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
from .sdk_contracts import declarations

RULES_VERSION = "shared-resources-v4"
MAX_FILES = 3500
MAX_BYTES = 16_000_000
MAX_FILE_BYTES = 350_000
MAX_OBSERVATIONS = 10000
MAX_IMPACTS = 350
MAX_USES_PER_IMPACT = 60

_STRING = r'"(?:\\.|[^"\\])*"'
CONSTANT = re.compile(r'\b(?:static\s+)?final\s+String\s+([A-Za-z_$]\w*)\s*=\s*([^;]+);')
STRING = re.compile(_STRING)
JAVA_COMMENTS = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*[\s\S]*?\*/|//[^\n]*')
REDIS_CALL = re.compile(
    r'\b(?:[A-Za-z_$]\w*(?:Redis|redis|Cache|cache|Jedis|jedis)[A-Za-z_$\w]*'
    r'|redisTemplate|redisClient|jedis)'
    r'(?:\s*\.\s*opsFor(?:Value|Hash|List|Set|ZSet)\s*\(\s*\))?'
    r'\s*\.\s*(get|set|setex|del|delete|expire|hasKey|getBucket|put|evict)'
    r'\s*\(\s*([^\n;]*?)\)', re.I)
CACHE_ANNOTATION = re.compile(
    r'@(Cacheable|CachePut|CacheEvict)\s*\(([^\n]*?)\)', re.I)
SPRING_BEAN = re.compile(
    r'@(?:Component|Service|Repository|Controller|Named|Qualifier)\s*\(\s*'
    r'(?:(?:value|name)\s*=\s*)?("([^"\n]+)")\s*\)|'
    r'@Bean\s*\(\s*(?:(?:value|name)\s*=\s*)?"([^"\n]+)"\s*\)')
XML_BEAN = re.compile(r'<bean\b[^>]*\bid\s*=\s*"([^"]+)"[^>]*>', re.I)
PACKAGE = re.compile(r'(?m)^\s*package\s+([\w.]+)\s*;')
CLASS = re.compile(r'\b(?:public\s+)?(?:abstract\s+)?(?:class|interface)\s+([A-Za-z_$]\w*)')
METHOD_DECL = re.compile(
    r'(?m)^\s*(?:public|protected)\s+(?:static\s+)?'
    r'[A-Za-z_$][\w.$<>, ?\[\]]+\s+([A-Za-z_$]\w*)\s*\([^;{}]*\)\s*(?:\{|;)')
REFLECT = re.compile(
    r'\b(Class\s*\.\s*forName|\.getDeclaredMethod|\.getMethod|\.getBean)'
    r'\s*\(\s*([^\n;]*?)\)', re.I)
DATA_SOURCE = re.compile(r'@(?:DS|RedisConnectionFactory)\s*\(\s*"([^"]+)"')
JAVA_EXT = {".java", ".xml"}
VALUE_VAR = re.compile(r'\b([A-Z][\w.$<>]*)\s+([a-z][\w$]*)\s*(?:=|;|,)')



def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _clean_comments(text):
    # Consume Java string/character literals before looking for comments:
    # policy keys such as "https://..." must not be truncated at '//'.
    return JAVA_COMMENTS.sub(
        lambda m: m.group() if m.group().startswith(('"', "'")) else
        re.sub(r"[^\n]", " ", m.group()), text)


def _line(text, start):
    return text.count("\n", 0, start) + 1


def _expr(expression, constants):
    """(known initial literal, exact, unresolved) from simple + expressions."""
    expression = expression.strip()
    # Stop at next top-level argument, respecting string content.
    parts, chunk, quoted, escaped = [], "", False, False
    for char in expression:
        if quoted:
            chunk += char
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            chunk += char
        elif char == ",":
            break
        elif char == "+":
            parts.append(chunk.strip())
            chunk = ""
        else:
            chunk += char
    parts.append(chunk.strip())
    prefix, dynamic = "", False
    for part in parts:
        if re.fullmatch(_STRING, part):
            if not dynamic:
                prefix += part[1:-1]
        elif part in constants and constants[part] is not None:
            if not dynamic:
                prefix += constants[part]
        else:
            dynamic = True
    return prefix, not dynamic, dynamic


def _observation(kind, role, value, exact, path, text, start, end,
                 method, ds=None, value_type=None, expression=None):
    return {"kind": kind, "role": role, "resource": value,
            "exact": exact, "path": path, "start": _line(text, start),
            "end": _line(text, end), "method": method,
            "datasource_hint": ds, "value_type_hint": value_type,
            "expression": expression,
            "status": "STATIC_NAME_ONLY_UNCONFIRMED"}


def extract_resources(text, path):
    """Read only source-visible literals and conservative name/prefix evidence."""
    visible = _clean_comments(text) if path.endswith(".java") else text
    # Mask string literals without shifting positions. Declaration and call
    # patterns must not match Java code quoted inside a log/message string.
    masked = re.sub(_STRING, lambda m: re.sub(r"[^\n]", " ", m.group()),
                    visible) if path.endswith(".java") else visible
    gaps, found, constants = [], [], {}
    for match in CONSTANT.finditer(visible):
        if masked[match.start()].isspace():
            continue
        val, exact, _ = _expr(match.group(2), {})
        constants[match.group(1)] = val if exact else None
    ds = DATA_SOURCE.search(visible)
    datasource = ds.group(1) if ds else None

    if path.endswith(".java"):
        variable_types = {name: typename.rsplit(".", 1)[-1]
                          for typename, name in VALUE_VAR.findall(masked)}
        for match in REDIS_CALL.finditer(visible):
            if masked[match.start()].isspace():
                continue
            method, args = match.groups()
            prefix, exact, dynamic = _expr(args, constants)
            if not prefix:
                gaps.append({"path": path, "line": _line(text, match.start()),
                             "reason": "DYNAMIC_REDIS_KEY_UNRESOLVED"})
                continue
            role = ("READ" if method.lower() in ("get", "haskey", "getbucket")
                    else "DELETE" if method.lower() in ("del", "delete", "evict")
                    else "TTL" if method.lower() in ("expire", "setex") else "WRITE")
            fields = args.split(",", 1)
            candidate_value = fields[1].strip() if len(fields) > 1 else ""
            value_type = variable_types.get(candidate_value)
            found.append(_observation("REDIS_KEY", role, prefix, exact, path,
                                      text, match.start(), match.end(), method,
                                      datasource, value_type=value_type,
                                      expression=args[:160]))
            if dynamic:
                gaps.append({"path": path, "line": _line(text, match.start()),
                             "reason": "DYNAMIC_REDIS_KEY_PREFIX_ONLY"})
        for match in CACHE_ANNOTATION.finditer(visible):
            if masked[match.start()].isspace():
                continue
            annotation, attributes = match.groups()
            val = re.search(r'\b(?:value|cacheNames)\s*=\s*"([^"]+)"', attributes)
            key = re.search(r'\bkey\s*=\s*"([^"]+)"', attributes)
            if not val:
                gaps.append({"path": path, "line": _line(text, match.start()),
                             "reason": "CACHE_NAMESPACE_NOT_RESOLVED"})
                continue
            raw = (key.group(1) if key else "")
            literal = re.match(r"\s*'([^']+)'", raw)
            prefix = val.group(1) + "::" + (literal.group(1) if literal else "")
            # No explicit key invokes Spring's key generator. A cache name
            # alone is a namespace clue, never an exact Redis key.
            exact = bool(key and literal is not None and
                         raw.strip() == "'" + literal.group(1) + "'")
            role = ("DELETE" if annotation.lower() == "cacheevict"
                    else "READ_WRITE" if annotation.lower() == "cacheable" else "WRITE")
            found.append(_observation("REDIS_KEY", role, prefix, exact, path, text,
                                      match.start(), match.end(), annotation,
                                      datasource, expression=attributes[:160]))
            if not exact:
                gaps.append({"path": path, "line": _line(text, match.start()),
                             "reason": ("CACHE_KEY_GENERATED_UNRESOLVED" if not key
                                        else "SPEL_CACHE_KEY_PREFIX_ONLY")})
        pkg = PACKAGE.search(visible)
        for match in CLASS.finditer(masked):
            name = match.group(1)
            fqcn = (pkg.group(1) + "." if pkg else "") + name
            found.append(_observation("REFLECT_CLASS", "DECLARATION", fqcn, True,
                                      path, text, match.start(), match.end(), "CLASS"))
        for match in SPRING_BEAN.finditer(visible):
            if masked[match.start()].isspace():
                continue
            name = match.group(2) or match.group(3)
            if name:
                found.append(_observation("SPRING_BEAN", "DECLARATION", name, True,
                                          path, text, match.start(), match.end(), "ANNOTATION"))
        for match in METHOD_DECL.finditer(masked):
            found.append(_observation("REFLECT_METHOD", "DECLARATION", match.group(1), True,
                                      path, text, match.start(), match.end(), "METHOD"))
        for match in REFLECT.finditer(visible):
            if masked[match.start()].isspace():
                continue
            op, args = match.groups()
            name, exact, _ = _expr(args, constants)
            kind = ("REFLECT_CLASS" if "forName" in op else
                    "SPRING_BEAN" if "getBean" in op else "REFLECT_METHOD")
            if not name:
                gaps.append({"path": path, "line": _line(text, match.start()),
                             "reason": "DYNAMIC_REFLECTION_NAME_UNRESOLVED"})
                continue
            found.append(_observation(kind, "CONSUMER", name, exact, path,
                                      text, match.start(), match.end(), op,
                                      expression=args[:160]))
            if not exact:
                gaps.append({"path": path, "line": _line(text, match.start()),
                             "reason": "DYNAMIC_REFLECTION_PREFIX_ONLY"})
    elif path.endswith(".xml"):
        for match in XML_BEAN.finditer(text):
            found.append(_observation("SPRING_BEAN", "DECLARATION",
                                      match.group(1), True, path, text,
                                      match.start(), match.end(), "XML_BEAN"))
    unique = {(x["kind"], x["role"], x["resource"], x["path"],
               x["start"], x["end"]): x for x in found}
    return list(unique.values()), gaps


def shared_snapshot(repo, project, sha, cache_dir):
    """Version and parse-rule scoped source snapshot with digest-checked cache."""
    cache_dir = Path(cache_dir)
    index, _ = source_snapshot_index(
        repo, project, sha, cache_dir / "paths", extensions=JAVA_EXT,
        rules_version=RULES_VERSION)
    key = _hash([index["index_key"], RULES_VERSION,
                 MAX_FILES, MAX_BYTES, MAX_FILE_BYTES, MAX_OBSERVATIONS])
    target = cache_dir / "observations" / key[:2] / (key + ".json")
    if target.exists():
        try:
            wrapper = json.loads(target.read_text("utf-8"))
            record = wrapper.get("record")
            if (isinstance(record, dict) and wrapper.get("digest") == _hash(record)
                    and record.get("key") == key and record.get("sha") == sha
                    and isinstance(record.get("observations"), list)
                    and isinstance(record.get("omissions"), list)):
                return record, True
        except (OSError, ValueError, TypeError):
            pass
    observations, gaps, total = [], list(index["omissions"]), 0
    for number, file in enumerate(index["files"]):
        if number >= MAX_FILES or len(observations) >= MAX_OBSERVATIONS or total >= MAX_BYTES:
            gaps.append({"reason": "RESOURCE_SCAN_BUDGET_EXHAUSTED"})
            break
        try:
            size = int(git(repo, "cat-file", "-s", file["blob_sha"]).strip())
            if size > MAX_FILE_BYTES:
                gaps.append({"path": file["path"], "reason": "SOURCE_BLOB_OVERSIZE"})
                continue
            if total + size > MAX_BYTES:
                gaps.append({"reason": "RESOURCE_BYTES_BUDGET_EXHAUSTED"})
                break
            raw = git(repo, "cat-file", "blob", file["blob_sha"])
            if b"\x00" in raw:
                gaps.append({"path": file["path"], "reason": "NON_TEXT_SOURCE"})
                continue
            total += len(raw)
            uses, missing = extract_resources(raw.decode("utf-8", "replace"), file["path"])
            gaps.extend(missing)
            if len(observations) + len(uses) > MAX_OBSERVATIONS:
                gaps.append({"reason": "RESOURCE_OBSERVATION_LIMIT"})
                observations.extend(uses[:MAX_OBSERVATIONS - len(observations)])
                break
            observations.extend(uses)
        except (RuntimeError, ValueError, OSError) as exc:
            gaps.append({"path": file["path"], "reason": "RESOURCE_SOURCE_READ_ERROR",
                         "error_type": type(exc).__name__})
    record = {"schema_version": RULES_VERSION, "key": key, "project": project,
              "sha": sha, "observations": observations, "omissions": gaps,
              "source_bytes": total,
              "limitation": "Lexical facts only; no verified Redis or reflection binding"}
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=str(target.parent),
                                         prefix="resource-", suffix=".tmp", delete=False) as handle:
            temp = Path(handle.name)
            json.dump({"record": record, "digest": _hash(record)},
                      handle, ensure_ascii=False, sort_keys=True)
        os.replace(str(temp), str(target))
    finally:
        if temp is not None and temp.exists():
            temp.unlink()
    return record, False


def _touches(diff, use, side):
    key = "old_range" if side == "base" else "new_range"
    location = "old_path" if side == "base" else "path"
    region = diff.get(key) or {}
    start, count = region.get("start"), region.get("count")
    return (diff.get(location) == use["path"] and isinstance(start, int)
            and isinstance(count, int) and count > 0
            and start <= use["end"] and start + count - 1 >= use["start"])


def _matches(source, consumer):
    if source["kind"] != consumer["kind"]:
        return False
    if source["kind"] != "REDIS_KEY":
        return (source["resource"] == consumer["resource"] or
                (not source["exact"] and
                 consumer["resource"].startswith(source["resource"])))
    a, b = source["resource"], consumer["resource"]
    if source["exact"] and consumer["exact"]:
        return a == b
    # A dynamic key is only a prefix clue, not an exact shared key.
    return bool(a and b) and (a.startswith(b) or b.startswith(a))


def analyze_shared_resources(entries, ledgers, cache_dir):
    snapshots, gaps, hits, builds = {}, [], 0, 0
    for project, entry in sorted(entries.items()):
        for side in ("base", "head"):
            sha = entry.get(side)
            if not sha:
                continue
            try:
                snapshot, hit = shared_snapshot(entry["repo"], project, sha, cache_dir)
                snapshots[(project, side)] = snapshot
                hits += int(hit)
                builds += int(not hit)
                gaps.extend({"project": project, "side": side, **gap}
                            for gap in snapshot["omissions"])
            except (OSError, ValueError, RuntimeError) as exc:
                gaps.append({"project": project, "side": side,
                             "reason": "RESOURCE_SNAPSHOT_FAILED",
                             "error_type": type(exc).__name__})
    consumers = defaultdict(list)
    for (project, side), snapshot in snapshots.items():
        if side != "head":
            continue
        for item in snapshot["observations"]:
            if item["kind"] == "REDIS_KEY" or item["role"] == "CONSUMER":
                consumers[item["kind"]].append((project, snapshot["sha"], item))
    grouped = {}
    for ledger in ledgers:
        if ledger.get("role") == "EVIDENCE":
            continue
        for diff in ledger.get("final_changes", []):
            for side in ("base", "head"):
                snapshot = snapshots.get((ledger["project"], side))
                if snapshot is None:
                    continue
                for source in snapshot["observations"]:
                    if source["kind"] == "REDIS_KEY":
                        # Source must be an operation being changed.
                        pass
                    elif source["role"] != "DECLARATION":
                        continue
                    if not _touches(diff, source, side):
                        continue
                    for consumer, consumer_sha, use in consumers.get(source["kind"], []):
                        if consumer == ledger["project"] or not _matches(source, use):
                            continue
                        key = (ledger["project"], source["kind"], source["resource"], consumer)
                        if key not in grouped:
                            if len(grouped) >= MAX_IMPACTS:
                                gaps.append({"reason": "RESOURCE_IMPACT_COUNT_LIMIT"})
                                continue
                            grouped[key] = {
                                "producer_project": ledger["project"],
                                "consumer_project": consumer,
                                "kind": source["kind"], "resource": source["resource"],
                                "base_sha": ledger["base"], "head_sha": ledger["head"],
                                "consumer_sha": consumer_sha, "change_ids": [],
                                "source_uses": [], "consumer_uses": [],
                                "status": "STATIC_RESOURCE_NAME_ONLY_UNCONFIRMED",
                                "limitation": "Names and prefixes are not verified runtime instances or bindings"}
                        group = grouped.get(key)
                        if group is None:
                            continue
                        if diff["id"] not in group["change_ids"]:
                            group["change_ids"].append(diff["id"])
                        loc = {**source, "side": side, "sha": snapshot["sha"]}
                        if loc not in group["source_uses"]:
                            if len(group["source_uses"]) < MAX_USES_PER_IMPACT:
                                group["source_uses"].append(loc)
                            else:
                                group["source_uses_truncated"] = True
                        matched = {**use, "sha": consumer_sha}
                        if matched not in group["consumer_uses"]:
                            if len(group["consumer_uses"]) < MAX_USES_PER_IMPACT:
                                group["consumer_uses"].append(matched)
                            else:
                                group["consumer_uses_truncated"] = True
                        if not source["exact"] or not use["exact"]:
                            group["status"] = "DYNAMIC_PREFIX_ONLY_UNCONFIRMED"
                        if (source["kind"] == "REDIS_KEY" and source.get("datasource_hint")
                                and use.get("datasource_hint")
                                and source["datasource_hint"] != use["datasource_hint"]):
                            group["status"] = "REDIS_INSTANCE_HINTS_DIFFER_UNCONFIRMED"
                        if source["kind"] == "REFLECT_METHOD":
                            group["status"] = "METHOD_NAME_ONLY_UNCONFIRMED"
    # Cache DTO fields may change while Redis key operations remain untouched.
    # Link them only if this project has a typed literal write to the key.
    inspected = set()
    for ledger in ledgers:
        if ledger.get("role") == "EVIDENCE":
            continue
        project = ledger["project"]
        head_snapshot = snapshots.get((project, "head"))
        if head_snapshot is None:
            continue
        writes = [x for x in head_snapshot["observations"]
                  if x["kind"] == "REDIS_KEY" and
                  x["role"] in ("WRITE", "READ_WRITE") and x.get("value_type_hint")]
        if not writes:
            continue
        for diff in ledger.get("final_changes", []):
            path = diff.get("path")
            if not path or not path.endswith(".java") or (project, path) in inspected:
                continue
            inspected.add((project, path))
            if len(inspected) > 300:
                gaps.append({"reason": "REDIS_DTO_SOURCE_BUDGET_EXHAUSTED"})
                break
            try:
                old_bytes = git(entries[project]["repo"], "show",
                                ledger["base"] + ":" + (diff.get("old_path") or path))
            except RuntimeError:
                old_bytes = b""
            try:
                new_bytes = git(entries[project]["repo"], "show",
                                ledger["head"] + ":" + path)
            except RuntimeError:
                new_bytes = b""
            if max(len(old_bytes), len(new_bytes)) > MAX_FILE_BYTES:
                gaps.append({"project": project, "path": path,
                             "reason": "REDIS_DTO_FILE_TOO_LARGE"})
                continue
            old = declarations(old_bytes.decode("utf-8", "replace"), path) if old_bytes else None
            new = declarations(new_bytes.decode("utf-8", "replace"), path) if new_bytes else None
            if old is None and new is None:
                continue
            dto_name = (new or old)["simple_name"]
            old_fields = {key: value for key, value in (old or {}).get("members", {}).items()
                          if value["kind"] == "FIELD"}
            new_fields = {key: value for key, value in (new or {}).get("members", {}).items()
                          if value["kind"] == "FIELD"}
            differing = sorted(key for key in old_fields.keys() | new_fields.keys()
                               if old_fields.get(key, {}).get("signature") !=
                               new_fields.get(key, {}).get("signature"))
            if not differing:
                continue
            for write in writes:
                if write["value_type_hint"] != dto_name:
                    continue
                for consumer, consumer_sha, read in consumers.get("REDIS_KEY", []):
                    if consumer == project or not _matches(write, read):
                        continue
                    for field in differing:
                        # Never associate a whole file's unrelated DTO fields
                        # with every hunk: keep exact old/new changed locations.
                        field_old, field_new = old_fields.get(field), new_fields.get(field)
                        covered = any(
                            _touches(diff, {"path": diff.get("old_path") or path,
                                             "start": field_old["line"],
                                             "end": field_old["line"]}, "base")
                            for _ in (0,) if field_old)
                        covered = covered or any(
                            _touches(diff, {"path": path, "start": field_new["line"],
                                             "end": field_new["line"]}, "head")
                            for _ in (0,) if field_new)
                        if not covered:
                            continue
                        resource = write["resource"] + "#" + dto_name + "." + field[6:]
                        key = (project, "REDIS_VALUE_DTO", resource, consumer)
                        if key not in grouped:
                            if len(grouped) >= MAX_IMPACTS:
                                gaps.append({"reason": "RESOURCE_IMPACT_COUNT_LIMIT"})
                                break
                            grouped[key] = {
                                "producer_project": project, "consumer_project": consumer,
                                "kind": "REDIS_VALUE_DTO", "resource": resource,
                                "base_sha": ledger["base"], "head_sha": ledger["head"],
                                "consumer_sha": consumer_sha, "change_ids": [],
                                "source_uses": [], "consumer_uses": [],
                                "status": "DTO_FIELD_AND_TYPED_KEY_LEXICAL_ONLY",
                                "limitation": "Value type and Redis instance not bound; key/DTO candidate only"}
                        group = grouped.get(key)
                        if not group:
                            continue
                        if diff["id"] not in group["change_ids"]:
                            group["change_ids"].append(diff["id"])
                        for side, data in (("base", old_fields.get(field)),
                                           ("head", new_fields.get(field))):
                            if data:
                                loc = {"kind": "REDIS_VALUE_DTO", "role": "DECLARATION",
                                       "resource": resource, "path": path,
                                       "start": data["line"], "end": data["line"],
                                       "side": side, "sha": ledger[side],
                                       "method": "FIELD", "datasource_hint": None,
                                       "value_type_hint": dto_name}
                                if loc not in group["source_uses"]:
                                    group["source_uses"].append(loc)
                        usage = {**read, "sha": consumer_sha}
                        if usage not in group["consumer_uses"]:
                            group["consumer_uses"].append(usage)
    for key, group in grouped.items():
        for field in ("source_uses", "consumer_uses"):
            if group.get(field + "_truncated"):
                gaps.append({"project": group["producer_project"],
                             "consumer_project": group["consumer_project"],
                             "resource": group["resource"],
                             "reason": "RESOURCE_EVIDENCE_LOCATIONS_TRUNCATED",
                             "field": field, "max_locations": MAX_USES_PER_IMPACT})
    return {"schema_version": "shared-impact-v1",
            "impacts": [grouped[key] for key in sorted(grouped)],
            "omissions": gaps, "cache": {"hits": hits, "builds": builds},
            "limitation": "Unchanged consumers searched at pinned HEAD only; no live binding assertion"}
