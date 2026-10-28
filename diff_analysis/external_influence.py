"""External-influence classification for final diffs.

This module does not attempt whole-program reachability. It asks a narrower
review question: is the changed code itself on an externally invocable surface
such as HTTP, Dubbo/RPC, MQ, WebSocket, or a similar inbound adapter?
"""
import re
from pathlib import Path

from .gitops import git
from .source_index import source_snapshot_index
from .query_contract import java_method_spans, _line


VERSION = "external-influence-v1"
MAX_FILES = 4000
MAX_FILE_BYTES = 500000

HTTP = re.compile(
    r'@(GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping|RequestMapping)\\b',
    re.I)
MQ = re.compile(
    r'@(RabbitListener|KafkaListener|JmsListener|SqsListener|'
    r'RocketMQMessageListener|RocketMQListener)\\b', re.I)
WEBSOCKET = re.compile(
    r'@(ServerEndpoint|MessageMapping|SubscribeMapping|OnMessage)\\b', re.I)
DUBBO_IMPORT = re.compile(
    r'import\\s+(?:com\\.alibaba\\.dubbo|org\\.apache\\.dubbo)'
    r'\\.config\\.annotation\\.(?:Service|DubboService)\\s*;')
DUBBO_ANNOTATION = re.compile(r'@(DubboService|Service)\\b')
SPRING_CONTROLLER = re.compile(r'@(RestController|Controller)\\b')
CLASS = re.compile(r'\\b(?:public\\s+)?(?:abstract\\s+)?class\\s+([A-Za-z_]\\w*)\\b')


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
    if dubbo_class and re.search(r'\\bpublic\\b', header):
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


def _snapshot(entry, project, side, paths, cache_dir):
    sha = entry.get(side)
    if not sha:
        return {}, []
    index, _ = source_snapshot_index(
        entry["repo"], project, sha, Path(cache_dir) / "indexes",
        extensions={".java"}, rules_version=VERSION, max_files=MAX_FILES)
    omissions = [{"project": project, "side": side, **gap}
                 for gap in index["omissions"]]
    files = {}
    for item in index["files"]:
        if item["path"] not in paths:
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
    """Annotate final changes that directly intersect inbound external surfaces."""
    candidates, omissions = [], []
    for ledger in ledgers:
        project = ledger["project"]
        entry = entries.get(project)
        if not entry:
            continue
        java_paths = set()
        for change in ledger.get("final_changes", []):
            for path in (change.get("path"), change.get("old_path")):
                if path and path.endswith(".java"):
                    java_paths.add(path)
        if not java_paths:
            continue
        try:
            base, gaps = _snapshot(
                entry, project, "base", java_paths, cache_dir)
            omissions.extend(gaps)
            head, gaps = _snapshot(
                entry, project, "head", java_paths, cache_dir)
            omissions.extend(gaps)
        except (OSError, RuntimeError, ValueError) as exc:
            omissions.append({
                "project": project, "reason": "EXTERNAL_SURFACE_SCAN_FAILED",
                "detail": type(exc).__name__})
            continue

        for change in ledger.get("final_changes", []):
            if not (change.get("path", "").endswith(".java") or
                    change.get("old_path", "").endswith(".java")):
                continue
            observations = []
            for side, path, source, rng in (
                    ("base", change.get("old_path"), base,
                     change.get("old_range")),
                    ("head", change.get("path"), head,
                     change.get("new_range"))):
                text = source.get(path or "")
                if not text:
                    continue
                for span in java_method_spans(text):
                    start_line = _line(text, span["start"])
                    end_line = _line(text, max(span["start"], span["end"] - 1))
                    if not _overlap(rng, start_line, end_line):
                        continue
                    for item in classify_method(text, path, span):
                        observations.append({"side": side, **item})
            unique = {}
            for item in observations:
                key = (item["side"], item["external_kind"], item["path"],
                       item["line"], item["method"])
                unique[key] = item
            if unique:
                candidates.append({
                    "project": project,
                    "change_id": change["id"],
                    "path": change.get("path"),
                    "old_path": change.get("old_path"),
                    "base_sha": ledger["base"],
                    "head_sha": ledger["head"],
                    "surfaces": sorted(
                        unique.values(),
                        key=lambda item: (
                            item["side"], item["external_kind"],
                            item["path"], item["line"])),
                    "priority": "HIGH",
                    "status": "DIRECT_EXTERNAL_SURFACE_CANDIDATE"})

    return {
        "schema_version": VERSION,
        "candidates": candidates,
        "omissions": omissions,
        "limitation": (
            "Direct inbound surface classification only. Absence of a candidate "
            "does not prove a change is unreachable from external callers; full "
            "call-chain reconstruction is intentionally not required.")}
