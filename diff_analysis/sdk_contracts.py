"""Fixed-SHA SDK contract changes and consumer-use candidates.

Lexical evidence is never considered javac attribution or a proven incompatibility.
No analyzed project build, business script, or annotation processor is executed.
"""
import posixpath
import re
from collections import defaultdict
from pathlib import Path

from .gitops import git, safe_source_path
from .source_index import source_snapshot_index

RULES_VERSION = "sdk-contract-lexical-v4"
MAX_BLOB = 600_000
MAX_CONSUMER_BYTES = 12_000_000
MAX_CONSUMER_FILES = 2500
MAX_HITS_PER_CONSUMER = 120

PACKAGE = re.compile(r"(?m)^\s*package\s+([\w.]+)\s*;")
TYPE = re.compile(r"\b(class|interface|enum)\s+([A-Za-z_$][\w$]*)\b")
FIELD = re.compile(r"(?m)^[ \t]*(?:public|protected|private)\s+(?:(?:static|final|transient|volatile)\s+)*([\w.$<>?, \[\]]+?)\s+([\w$]+)\s*(?:=[^;]*)?;")
METHOD = re.compile(r"(?m)^[ \t]*(?:(?:public|protected|abstract|default|static|final|synchronized)\s+)*([\w.$<>?, \[\]]+?)\s+([\w$]+)\s*\(([^(){};]*)\)\s*(?:throws\s+[\w., ]+)?\s*(?:\{|;)")
JSON_PROPERTY = re.compile(r'@(?:JsonProperty|SerializedName)\s*\(\s*"([^"\n]+)"')
JSON_FIELD = re.compile(r'@JSONField\s*\(\s*name\s*=\s*"([^"\n]+)"')
IDENT = re.compile(r"^[A-Za-z_$][\w$]*$")


def _source(repo, sha, path, gaps):
    if not safe_source_path(path, windows=True):
        gaps.append({"path": path, "reason": "SDK_UNSAFE_SOURCE_PATH"})
        return None
    try:
        size = int(git(repo, "cat-file", "-s", sha + ":" + path).strip())
        if size > MAX_BLOB:
            gaps.append({"path": path, "reason": "SDK_SOURCE_OVERSIZE"})
            return None
        content = git(repo, "show", sha + ":" + path)
        if b"\x00" in content:
            gaps.append({"path": path, "reason": "SDK_NON_TEXT"})
            return None
        return content.decode("utf-8", "replace")
    except (ValueError, RuntimeError, OSError):
        gaps.append({"path": path, "reason": "SDK_SOURCE_NOT_FOUND"})
        return None


def _code(text):
    def erase(match):
        return re.sub(r"[^\n]", " ", match.group())
    # Java strings before comment delimiters: fake method calls and type
    # names inside literals must not become contract/consumer evidence.
    return re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|'
                  r'/\*[\s\S]*?\*/|//[^\n]*', erase, text)


def _params(value):
    # Generic commas are not parameter boundaries: Map<String, Integer> is
    # one parameter. Preserve JDK 8 generics without invoking javac or code.
    fragments, start, depth = [], 0, 0
    for index, char in enumerate(value):
        if char == "<":
            depth += 1
        elif char == ">":
            depth -= 1
            if depth < 0:
                return None
        elif char == "," and depth == 0:
            fragments.append(value[start:index])
            start = index + 1
    if depth:
        return None
    fragments.append(value[start:])
    parts = []
    for raw in fragments:
        raw = re.sub(r"@[\w.]+(?:\([^)]*\))?", " ", raw)
        words = raw.strip().replace("...", "[]").split()
        if not words:
            continue
        words = [x for x in words if x not in ("final", "volatile")]
        if len(words) < 2:
            return None
        parts.append(" ".join(words[:-1]).replace(" ", ""))
    return parts


def declarations(source, path):
    """Simple top-level declarations only. Unknown constructs stay omissions."""
    code = _code(source)
    pkg = PACKAGE.search(code)
    typ = TYPE.search(code)
    if not typ:
        return None
    kind, cls = typ.groups()
    fqcn = (pkg.group(1) + "." if pkg else "") + cls
    lines = source.splitlines()
    members, omissions = {}, []
    if kind == "enum":
        opening = code.find("{", typ.end())
        if opening < 0:
            omissions.append("ENUM_BODY_MISSING")
        else:
            stop = code.find(";", opening + 1)
            closing = code.find("}", opening + 1)
            end = min([x for x in (stop, closing) if x >= 0], default=-1)
            if end >= 0:
                chunk, depth, token = code[opening + 1:end], 0, ""
                for ch in chunk + ",":
                    if ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth -= 1
                    if ch == "," and depth == 0:
                        name = re.match(r"\s*([A-Za-z_$][\w$]*)", token)
                        if name:
                            member = name.group(1)
                            offset = code.find(member, opening + 1, end)
                            members["ENUM:" + member] = {
                                "identity": "ENUM:" + member, "signature": member,
                                "line": code.count("\n", 0, offset) + 1, "kind": "ENUM"}
                        token = ""
                    else:
                        token += ch
            else:
                omissions.append("ENUM_VALUES_UNRESOLVED")
    for match in FIELD.finditer(code):
        vartype, name = match.groups()
        if not IDENT.fullmatch(name) or vartype.strip().split()[0] in ("return", "throw", "new"):
            continue
        lineno = code.count("\n", 0, match.start()) + 1
        preceding = "\n".join(lines[max(0, lineno - 4):lineno])
        serialized = JSON_PROPERTY.search(preceding) or JSON_FIELD.search(preceding)
        signature = re.sub(r"\s+", "", vartype) + "|serialized=" + (serialized.group(1) if serialized else name)
        members["FIELD:" + name] = {"identity": "FIELD:" + name,
                                     "signature": signature, "line": lineno, "kind": "FIELD"}
    method_counts = defaultdict(int)
    for match in METHOD.finditer(code):
        ret, name, args = match.groups()
        if ret.strip().split()[0] in ("return", "throw", "new", "if", "while", "for", "switch", "private"):
            continue
        if name == cls:
            continue
        parameters = _params(args)
        if parameters is None:
            omissions.append("METHOD_PARAMETERS_UNRESOLVED:" + name)
            continue
        identity = "METHOD:%s/%d" % (name, len(parameters))
        method_counts[identity] += 1
        members[identity] = {
            "identity": identity,
            "signature": ret.strip() + "(" + ",".join(parameters) + ")",
            "line": code.count("\n", 0, match.start()) + 1, "kind": "METHOD"}
    for identity, count in method_counts.items():
        if count > 1:
            members.pop(identity, None)
            omissions.append("OVERLOADED_METHOD_AMBIGUOUS:" + identity)
    return {"type": fqcn, "simple_name": cls, "kind": kind, "path": path,
            "members": members, "omissions": omissions}


def contract_changes(sdk, repo, snapshots, gaps):
    """Use final net changed Java paths; untouched declarations are not changes."""
    prefix = posixpath.dirname(sdk["module"])
    prefix = prefix + "/" if prefix else ""
    old_names = set(snapshots.get("base", {}).get("names", []))
    new_names = set(snapshots.get("head", {}).get("names", []))
    found = []
    java_events = sdk.get("java_changes", [])
    for path in sorted(set(sdk.get("java_change_paths", []))):
        if not path.endswith(".java") or not path.startswith(prefix):
            continue
        before = (_source(repo, sdk["base_sha"], path, gaps)
                  if path in old_names and sdk.get("base_sha") else None)
        after = (_source(repo, sdk["head_sha"], path, gaps)
                 if path in new_names and sdk.get("head_sha") else None)
        old = declarations(before, path) if before is not None else None
        new = declarations(after, path) if after is not None else None
        if old is None and new is None:
            gaps.append({"path": path, "reason": "SDK_DECLARATION_UNRESOLVED"})
            continue
        if old and new and old["type"] != new["type"]:
            gaps.append({"path": path, "reason": "SDK_TYPE_RENAMED_CANDIDATE"})
        for side in (old, new):
            if side:
                gaps.extend({"path": path, "reason": reason} for reason in side["omissions"])
        oldmembers = old["members"] if old else {}
        newmembers = new["members"] if new else {}
        # SDK alerts may cover many unrelated files/hunks. A member only
        # receives the D-* records that actually overlap its declaration.
        related = [c for c in java_events
                   if c.get("path") == path or c.get("old_path") == path]
        for identity in sorted(oldmembers.keys() | newmembers.keys()):
            previous, current = oldmembers.get(identity), newmembers.get(identity)
            if previous and current and previous["signature"] == current["signature"]:
                continue
            matching_ids = []
            for event in related:
                touched = False
                for location, field, source_path in (
                        (previous, "old_range", "old_path"),
                        (current, "new_range", "path")):
                    if not location or event.get(source_path) != path:
                        continue
                    span = event.get(field) or {}
                    line, start, count = location["line"], span.get("start"), span.get("count")
                    if (isinstance(start, int) and isinstance(count, int)
                            and count > 0 and start <= line < start + count):
                        touched = True
                if touched:
                    matching_ids.append(event["id"])
            if java_events and not matching_ids:
                gaps.append({"path": path, "reason": "SDK_MEMBER_DIFF_RANGE_UNMAPPED",
                             "member": identity})
                continue
            found.append({
                "identity": identity, "type": (new or old)["type"],
                "simple_name": (new or old)["simple_name"], "path": path,
                "old": previous, "new": current,
                "status": ("REMOVED" if not current else "ADDED" if not previous else "MODIFIED"),
                "sdk_project": sdk["project"], "base_sha": sdk["base_sha"],
                "head_sha": sdk["head_sha"],
                "change_ids": sorted(set(matching_ids or sdk["changes"]))})
    return found


def _consumer_uses(text, contract):
    """Require explicit imported/qualified type and member expression."""
    fqcn, simple = contract["type"], contract["simple_name"]
    code = _code(text)
    imported = bool(re.search(r"(?m)^\s*import\s+" + re.escape(fqcn) + r"\s*;", code))
    qualified = bool(re.search(r"\b" + re.escape(fqcn) + r"\b", code))
    if not imported and not qualified:
        return []
    kind, _, member = contract["identity"].partition(":")
    name = member.split("/")[0] if kind == "METHOD" else member
    if not IDENT.fullmatch(name):
        return []
    typed = simple if imported else fqcn
    receivers = set()
    for match in re.finditer(r"\b" + re.escape(typed) + r"\s+([A-Za-z_$][\w$]*)\b", code):
        receivers.add(match.group(1))
    patterns = []
    for token in sorted(receivers | {typed}, key=len, reverse=True):
        suffix = r"\s*\(" if kind == "METHOD" else r"\b"
        patterns.append(re.compile(r"(?<![\w$])" + re.escape(token)
                                   + r"\s*\.\s*" + re.escape(name) + suffix))
    if kind == "ENUM" and imported:
        patterns.append(re.compile(r"\bcase\s+" + re.escape(name) + r"\s*:"))
    results = []
    for lineno, line in enumerate(code.splitlines(), 1):
        if re.match(r"\s*import\b", line):
            continue
        if any(p.search(line) for p in patterns):
            results.append({
                "line": lineno, "excerpt": text.splitlines()[lineno - 1].strip()[:300],
                "binding_status": "TYPE_SCOPED_LEXICAL_CANDIDATE"})
    return results


def analyze_contract_uses(sdks, entries, snapshots, ledgers, cache_dir):
    """Version-scoped, direct-Maven consumer matches; includes unchanged files."""
    from .sdk_ast_bindings import attributed_uses
    groups, all_changes, gaps = [], [], []
    for sdk in sdks:
        producer = sdk["project"]
        if producer not in entries or not sdk.get("base_sha") or not sdk.get("head_sha"):
            continue
        changes = contract_changes(sdk, entries[producer]["repo"],
                                   snapshots[producer], gaps)
        all_changes.extend(changes)
        if not changes:
            continue
        for consumer, item in sorted(entries.items()):
            if consumer == producer or not item.get("head"):
                continue
            inv = snapshots.get(consumer, {}).get("head", {})
            dependency = next((d for m in inv.get("modules", []) for d in m["dependencies"]
                               if (d["group"], d["artifact"]) == (sdk["group"], sdk["artifact"])), None)
            if not dependency:
                continue
            try:
                index, _ = source_snapshot_index(
                    item["repo"], consumer, item["head"], cache_dir,
                    extensions={".java"}, rules_version=RULES_VERSION)
            except (ValueError, RuntimeError, OSError) as exc:
                gaps.append({"project": consumer, "reason": "CONSUMER_INDEX_FAILED",
                             "detail": str(exc)[:100]})
                continue
            gaps.extend({"project": consumer, **gap} for gap in index["omissions"])
            ast_uses, ast_gaps = attributed_uses(
                sdk, consumer, entries, snapshots, changes, Path(cache_dir) / "jdk8_bindings")
            gaps.extend({"project": consumer, "sdk": sdk["project"], **gap}
                        for gap in ast_gaps)
            uses, byte_budget = [], 0
            ast_locations = {
                (item["contract"]["identity"], item["consumer_path"], item["line"])
                for item in ast_uses}
            for seen, file in enumerate(index["files"]):
                if seen >= MAX_CONSUMER_FILES or byte_budget >= MAX_CONSUMER_BYTES:
                    gaps.append({"project": consumer, "reason": "SDK_CONSUMER_SCAN_BUDGET"})
                    break
                path = file["path"]
                source = _source(item["repo"], item["head"], path, gaps)
                if source is None:
                    continue
                byte_budget += len(source.encode("utf-8"))
                for change in changes:
                    for use in _consumer_uses(source, change):
                        if (change["identity"], path, use["line"]) in ast_locations:
                            continue
                        uses.append({"contract": change, "consumer_path": path,
                                     "consumer_sha": item["head"], **use})
                        if len(uses) >= MAX_HITS_PER_CONSUMER:
                            gaps.append({"project": consumer, "reason": "SDK_CONSUMER_HIT_LIMIT"})
                            break
                    if len(uses) >= MAX_HITS_PER_CONSUMER:
                        break
                if len(uses) >= MAX_HITS_PER_CONSUMER:
                    break
            # Attributed evidence takes precedence over lexical candidates;
            # unresolved uses remain explicitly marked lexical.
            uses = (ast_uses + uses)[:MAX_HITS_PER_CONSUMER]
            if uses:
                groups.append({
                    "sdk": sdk, "consumer": consumer,
                    "consumer_base_sha": item.get("base"), "consumer_head_sha": item["head"],
                    "dependency_version": dependency.get("version"),
                    "dependency_path": dependency.get("path"), "uses": uses,
                    "status": "REVIEW_REQUIRED",
                    "limitation": "JDK8 attributed SDK source symbols when available; "
                                  "lexical fallback remains unverified. "
                                  "Neither proves the deployed binary or compatibility."})
    return {"changes": all_changes, "impacts": groups, "gaps": gaps}
