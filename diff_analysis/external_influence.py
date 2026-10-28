"""External-influence classification for final diffs.

This module does not attempt whole-program reachability. It asks a narrower
review question: is the changed code itself on an externally invocable surface
such as HTTP, Dubbo/RPC, MQ, WebSocket, or a similar inbound adapter?
"""
import re
from pathlib import Path

from .gitops import git
from .source_index import source_snapshot_index
from .query_contract import (
    java_method_spans, mapper_callers, mapper_declaration, typed_method_callers,
    _declared_callable_types, _line)


VERSION = "external-influence-v2"
MAX_FILES = 4000
MAX_FILE_BYTES = 500000

HTTP = re.compile(
    r'@(GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping|RequestMapping)\b',
    re.I)
MQ = re.compile(
    r'@(RabbitListener|KafkaListener|JmsListener|SqsListener|'
    r'RocketMQMessageListener|RocketMQListener)\b', re.I)
WEBSOCKET = re.compile(
    r'@(ServerEndpoint|MessageMapping|SubscribeMapping|OnMessage)\b', re.I)
DUBBO_IMPORT = re.compile(
    r'import\s+(?:com\.alibaba\.dubbo|org\.apache\.dubbo)'
    r'\.config\.annotation\.(?:Service|DubboService)\s*;')
DUBBO_ANNOTATION = re.compile(r'@(DubboService|Service)\b')
SPRING_CONTROLLER = re.compile(r'@(RestController|Controller)\b')
CLASS = re.compile(r'\b(?:public\s+)?(?:abstract\s+)?class\s+([A-Za-z_]\w*)\b')
MAPPER = re.compile(r'<mapper\b([^>]*)>', re.I)
XML_ATTR = re.compile(r'\b(id|namespace)\s*=\s*["\x27]([^"\x27]+)', re.I)
MYBATIS_STATEMENT = re.compile(
    r'<(select|insert|update|delete)\b([^>]*)>(.*?)</\1\s*>',
    re.I | re.S)

def _overlap(change_range, start, end):
    begin = int((change_range or {}).get("start") or 0)
    count = int((change_range or {}).get("count") or 0)
    if not begin or not count:
        return False
    finish = begin + count - 1
    return begin <= end and finish >= start


def _class_header(text):
    match = CLASS.search(text)
    if not match:
        return ""
    return text[max(0, match.start() - 3000):match.start() + 300]


def _method_header(text, span):
    fragment = text[span["start"]:span["end"]]
    opening = fragment.find("{")
    return fragment if opening < 0 else fragment[:opening]


def _method_evidence(text, span):
    line = span.get("line") or _line(text, span["start"])
    lines = text.splitlines()
    evidence = lines[line - 1].strip() if 0 < line <= len(lines) else span["name"]
    return line, evidence[:250]


def classify_method(text, path, span):
    """Classify a changed method by direct inbound external surface."""
    header = _method_header(text, span)
    class_header = _class_header(text)
    line, evidence = _method_evidence(text, span)
    result = []

    http = HTTP.search(header)
    if http:
        result.append({
            "external_kind": "HTTP_ENDPOINT",
            "priority": "HIGH",
            "method": span["name"],
            "line": line,
            "path": path,
            "evidence": evidence,
            "reason": "Changed method is directly mapped as an HTTP endpoint"})

    mq = MQ.search(header) or MQ.search(class_header)
    if mq:
        result.append({
            "external_kind": "MQ_INBOUND",
            "priority": "HIGH",
            "method": span["name"],
            "line": line,
            "path": path,
            "evidence": evidence,
            "reason": "Changed method/class is an inbound message listener candidate"})

    websocket = WEBSOCKET.search(header) or WEBSOCKET.search(class_header)
    if websocket:
        result.append({
            "external_kind": "WEBSOCKET_INBOUND",
            "priority": "HIGH",
            "method": span["name"],
            "line": line,
            "path": path,
            "evidence": evidence,
            "reason": "Changed method/class is exposed through WebSocket/STOMP"})

    dubbo_class = (
        "@DubboService" in class_header or
        (DUBBO_IMPORT.search(text) and DUBBO_ANNOTATION.search(class_header)))
    if dubbo_class and re.search(r'\bpublic\b', header):
        result.append({
            "external_kind": "DUBBO_RPC_SERVICE",
            "priority": "HIGH",
            "method": span["name"],
            "line": line,
            "path": path,
            "evidence": evidence,
            "reason": "Changed public method belongs to a Dubbo service class"})

    # Controller class with an unmapped changed method is not treated as an
    # endpoint. Keep this as context only when the method itself has mapping.
    return result



def _caller_span(caller):
    if caller.get("caller_start") is None or caller.get("caller_end") is None:
        return None
    return {
        "name": caller.get("caller_method"),
        "start": caller["caller_start"],
        "end": caller["caller_end"],
        "line": caller.get("line"),
        "parameters": caller.get("caller_parameters", {})}


def _classify_caller(files, caller, binding):
    span = _caller_span(caller)
    text = files.get(caller.get("path") or "")
    if not span or not text:
        return []
    return [
        {**item, "binding": binding}
        for item in classify_method(text, caller["path"], span)]


def one_hop_external_surfaces(files, text, path, span):
    """Direct surface plus at most one explicit typed caller hop."""
    direct = [
        {**item, "binding": "DIRECT_CHANGED_METHOD"}
        for item in classify_method(text, path, span)]
    type_names = _declared_callable_types(text)
    upper = typed_method_callers(
        files, type_names, span["name"], "ONE_TYPED_CALLER_HOP")
    indirect = []
    for caller in upper:
        for item in _classify_caller(files, caller, "ONE_TYPED_CALLER_HOP"):
            indirect.append({
                **item,
                "reason": (
                    "Changed method has a one-hop typed caller on an external "
                    "inbound surface; full call-chain proof is intentionally not required")})
    unique = {}
    for item in direct + indirect:
        key = (item.get("binding"), item["external_kind"], item["path"],
               item["line"], item["method"])
        unique[key] = item
    return list(unique.values())


def mybatis_statements(text, path):
    mapper = MAPPER.search(text)
    namespace = (
        dict(XML_ATTR.findall(mapper.group(1))).get("namespace", "")
        if mapper else "")
    result = []
    for match in MYBATIS_STATEMENT.finditer(text):
        attrs = dict(XML_ATTR.findall(match.group(2)))
        if not attrs.get("id"):
            continue
        result.append({
            "namespace": namespace,
            "id": attrs["id"],
            "operation": match.group(1).lower(),
            "path": path,
            "line": _line(text, match.start()),
            "end_line": _line(text, max(match.start(), match.end() - 1))})
    return result


def mybatis_external_surfaces(files, statement):
    """Bind Mapper XML to callers and inspect at most one upper typed hop."""
    declaration = mapper_declaration(
        files, statement["namespace"], statement["id"])
    callers = mapper_callers(files, declaration)
    result = []
    for caller in callers:
        result.extend(_classify_caller(
            files, caller, "MYBATIS_DIRECT_CALLER_EXTERNAL"))
        type_names = caller.get("caller_types") or []
        method = caller.get("caller_method")
        if not type_names or not method:
            continue
        for upper in typed_method_callers(
                files, type_names, method, "MYBATIS_ONE_TYPED_CALLER_HOP"):
            result.extend(_classify_caller(
                files, upper, "MYBATIS_ONE_TYPED_CALLER_HOP"))
    unique = {}
    for item in result:
        item = {
            **item,
            "reason": (
                "Changed MyBatis statement is directly or one typed caller hop "
                "from an external inbound surface")}
        key = (item.get("binding"), item["external_kind"], item["path"],
               item["line"], item["method"])
        unique[key] = item
    return list(unique.values())


def _snapshot(entry, project, side, paths, cache_dir, include_all_java=False):
    sha = entry.get(side)
    if not sha:
        return {}, []
    index, _ = source_snapshot_index(
        entry["repo"], project, sha, Path(cache_dir) / "indexes",
        extensions={".java", ".xml"}, rules_version=VERSION, max_files=MAX_FILES)
    omissions = [{"project": project, "side": side, **gap}
                 for gap in index["omissions"]]
    files = {}
    for item in index["files"]:
        if (item["path"] not in paths and
                not (include_all_java and item["path"].endswith(".java"))):
            continue
        raw = git(entry["repo"], "cat-file", "blob", item["blob_sha"])
        if len(raw) > MAX_FILE_BYTES:
            omissions.append({
                "project": project, "side": side, "path": item["path"],
                "reason": "FILE_TOO_LARGE"})
            continue
        files[item["path"]] = raw.decode("utf-8", "replace")
    return files, omissions


def analyze_external_influence(entries, ledgers, cache_dir):
    """Annotate final diffs using direct or one-hop inbound external surfaces."""
    candidates, omissions = [], []
    for ledger in ledgers:
        project = ledger["project"]
        entry = entries.get(project)
        if not entry:
            continue
        changed_paths = set()
        for change in ledger.get("final_changes", []):
            for path in (change.get("path"), change.get("old_path")):
                if path and (path.endswith(".java") or path.endswith(".xml")):
                    changed_paths.add(path)
        if not changed_paths:
            continue
        try:
            base, gaps = _snapshot(
                entry, project, "base", changed_paths, cache_dir,
                include_all_java=False)
            omissions.extend(gaps)
            head, gaps = _snapshot(
                entry, project, "head", changed_paths, cache_dir,
                include_all_java=True)
            omissions.extend(gaps)
        except (OSError, RuntimeError, ValueError) as exc:
            omissions.append({
                "project": project, "reason": "EXTERNAL_SURFACE_SCAN_FAILED",
                "detail": type(exc).__name__})
            continue

        for change in ledger.get("final_changes", []):
            observations = []
            for side, path, source, rng in (
                    ("base", change.get("old_path"), base,
                     change.get("old_range")),
                    ("head", change.get("path"), head,
                     change.get("new_range"))):
                text = source.get(path or "")
                if not text:
                    continue
                if path.endswith(".java"):
                    for span in java_method_spans(text):
                        start_line = _line(text, span["start"])
                        end_line = _line(
                            text, max(span["start"], span["end"] - 1))
                        if not _overlap(rng, start_line, end_line):
                            continue
                        for item in one_hop_external_surfaces(
                                source, text, path, span):
                            observations.append({"side": side, **item})
                elif path.endswith(".xml"):
                    for statement in mybatis_statements(text, path):
                        if not _overlap(
                                rng, statement["line"], statement["end_line"]):
                            continue
                        for item in mybatis_external_surfaces(
                                source, statement):
                            observations.append({
                                "side": side,
                                "mybatis_statement": {
                                    "namespace": statement["namespace"],
                                    "id": statement["id"],
                                    "operation": statement["operation"],
                                    "path": statement["path"],
                                    "line": statement["line"]},
                                **item})

            unique = {}
            for item in observations:
                key = (
                    item["side"], item.get("binding"),
                    item["external_kind"], item["path"],
                    item["line"], item["method"])
                unique[key] = item
            if not unique:
                continue
            surfaces = sorted(
                unique.values(),
                key=lambda item: (
                    item["side"], item.get("binding", ""),
                    item["external_kind"], item["path"], item["line"]))
            high_bindings = {
                "DIRECT_CHANGED_METHOD",
                "MYBATIS_DIRECT_CALLER_EXTERNAL"}
            priority = (
                "HIGH" if any(
                    item.get("binding") in high_bindings
                    for item in surfaces)
                else "MEDIUM")
            candidates.append({
                "project": project,
                "change_id": change["id"],
                "path": change.get("path"),
                "old_path": change.get("old_path"),
                "base_sha": ledger["base"],
                "head_sha": ledger["head"],
                "surfaces": surfaces,
                "priority": priority,
                "status": "EXTERNAL_INFLUENCE_CANDIDATE"})

    return {
        "schema_version": VERSION,
        "candidates": candidates,
        "omissions": omissions,
        "limitation": (
            "Only direct inbound surfaces and one explicit typed caller hop are "
            "checked. Absence does not prove external callers cannot influence "
            "the change; whole-program path reconstruction is intentionally not "
            "a prerequisite for raising risk.")}
