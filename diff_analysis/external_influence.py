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
    _declared_callable_types, _line, _mask_java, _brace_end, _paren_end)


VERSION = "external-influence-v3"
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


def method_input_influence(text, path, span, change_range):
    """只按改动表达式、简单局部赋值和包围条件判断参数影响，不推断完整数据流。"""
    masked = _mask_java(text)
    opening = masked.find('{', span['start'], span['end'])
    start = int((change_range or {}).get('start') or 0)
    count = int((change_range or {}).get('count') or 0)
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    begin = max(opening + 1, offsets[min(max(start - 1, 0), len(lines))])
    end = min(span['end'] - 1, offsets[min(max(start + count - 1, 0), len(lines))])
    parameters = span.get('parameters') or {}
    sources = {name: {name} for name in parameters}
    constants, controls, evidence, roots, effects, propagation = set(), [], [], set(), set(), []
    unknown, seen = begin >= end, False

    def dependencies(expression):
        expression = re.sub(r'\s*\.\s*', '.', expression)
        return set().union(*(sources.get(name, set()) for name in
                             re.findall(r'(?<![.\w])[A-Za-z_]\w*', expression)))

    def constant_expression(expression):
        # 字面量和此前确认的局部确定值可以判低，成员读取和调用仍保留未知。
        literal = bool(re.search(r'["\x27]', expression))
        expression = _mask_java(expression).strip()
        names = set(re.findall(r'\b[A-Za-z_]\w*\b', expression))
        return (not re.search(r'[A-Za-z_]\w*\s*\(|\bnew\b|\.', expression)
                and names <= constants | {'true', 'false', 'null'}
                and bool(expression or literal))

    body = masked[opening + 1:span['end'] - 1]
    for match in re.finditer(r'[^;{}]+[;{}]', body):
        fragment = match.group()
        offset = opening + 1 + match.start()
        finish = opening + 1 + match.end()
        assignment = re.search(r'(?<![.\w])([A-Za-z_]\w*)\s*=(?!=)\s*(.*?)\s*[;{}]$', fragment, re.S)
        expression = assignment.group(2) if assignment else fragment
        raw_expression = (text[offset + fragment.find('=', assignment.end(1)) + 1:finish - 1]
                          if assignment else expression)
        dep = dependencies(expression)
        control = re.search(r'\b(if|while|for|switch)\s*\(', fragment)
        if control:
            paren = offset + control.end() - 1
            close = _paren_end(masked, paren)
            if close is not None:
                condition = dependencies(masked[paren + 1:close])
                after = close + 1
                while after < span['end'] and masked[after].isspace():
                    after += 1
                control_end = (_brace_end(masked, after) if masked[after:after + 1] == '{'
                               else masked.find(';', after) + 1)
                if control.group(1) == 'if' and control_end:
                    for _ in range(20):
                        tail = re.match(r'\s*else\b\s*', masked[control_end:span['end']])
                        if not tail:
                            break
                        branch = control_end + tail.end()
                        nested = re.match(r'if\s*\(', masked[branch:span['end']])
                        if nested:
                            close_branch = _paren_end(masked, branch + nested.end() - 1)
                            if close_branch is None:
                                unknown = True
                                break
                            branch = close_branch + 1
                            while branch < span['end'] and masked[branch].isspace():
                                branch += 1
                        next_end = (_brace_end(masked, branch) if masked[branch:branch + 1] == '{'
                                    else masked.find(';', branch, span['end']) + 1)
                        if not next_end:
                            unknown = True
                            break
                        control_end = next_end
                if control_end:
                    controls.append((after, control_end, condition,
                                     not condition and not constant_expression(text[paren + 1:close])))
                    if condition and offset < end:
                        propagation.append({'path': path, 'line': _line(text, paren),
                                            'parameters': sorted(condition), 'kind': 'CONTROL_CONDITION',
                                            'evidence': text[paren + 1:close].strip()[:300]})
                elif condition:
                    unknown = True
        active_controls = []
        for left, right, condition, unresolved in controls:
            if left <= offset < right:
                dep.update(condition)
                active_controls.append(unresolved)
        if offset < end and finish > begin and masked[max(offset, begin):min(finish, end)].strip():
            seen = True
            if dep:
                roots.update(dep)
                effect = ('返回值' if re.search(r'\breturn\b', fragment) else
                          '条件分支' if control else '赋值结果' if assignment else '下游调用参数')
                effects.add(effect)
                evidence.append({'path': path, 'line': _line(text, max(offset, begin)),
                                 'method': span['name'], 'parameters': sorted(dep),
                                 'effect': effect,
                                 'evidence': text[max(offset, begin):min(finish, end)].strip()[:300]})
            else:
                returned = re.fullmatch(r'\s*return\s+(.*?)\s*;\s*', fragment, re.S)
                if any(active_controls) or not ((assignment and constant_expression(raw_expression)) or
                        (returned and constant_expression(text[offset + fragment.find('return') + 6:
                                                              offset + fragment.rfind(';')]))):
                    unknown = True
        if assignment:
            target = assignment.group(1)
            sources[target] = sources.get(target, set()) | dep if active_controls else dep
            if dep and offset < end:
                propagation.append({'path': path, 'line': _line(text, offset),
                                    'parameters': sorted(dep), 'kind': 'LOCAL_ASSIGNMENT',
                                    'evidence': text[offset:finish].strip()[:300]})
            if not dep and not any(active_controls) and constant_expression(raw_expression):
                constants.add(target)
            else:
                constants.discard(target)
    unknown = unknown or not seen
    level = 'HIGH' if roots else 'MEDIUM' if unknown else 'LOW'
    evidence = [item for item in propagation if roots.intersection(item['parameters'])] + evidence
    return {
        'level': level,
        'status': ('CALLER_PARAMETER_REACHES_CHANGE' if roots else
                   'INPUT_SOURCE_UNRESOLVED' if unknown else 'INTERNAL_DETERMINISTIC_EXPRESSION'),
        'reason': ('上层调用传入的参数影响本次改动的表达式或包围分支。' if roots else
                   '尚不能确定本次改动值的来源或传播范围。' if unknown else
                   '本次改动表达式只使用字面量或已确认的内部确定值。'),
        'parameters': sorted(roots), 'effects': sorted(effects), 'method': span['name'], 'path': path,
        'evidence': evidence[:30], 'omitted_evidence_count': max(0, len(evidence) - 30),
        'limitation': '仅覆盖显式参数引用、简单赋值和带花括号的控制条件；不代表完整类型绑定或跨方法数据流。'}

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
    """Direct surface plus at most one local or explicit typed caller hop."""
    direct = [
        {**item, "binding": "DIRECT_CHANGED_METHOD"}
        for item in classify_method(text, path, span)]
    indirect = []

    # Same-class helper calls are common in controllers/listeners. Treat only
    # one lexical call hop as evidence; do not recurse through helper chains.
    call_pattern = re.compile(
        r'\b(?:this\s*\.\s*)?' + re.escape(span["name"]) + r'\s*\(')
    for caller_span in java_method_spans(text):
        if caller_span["start"] == span["start"]:
            continue
        body = text[caller_span["start"]:caller_span["end"]]
        opening = body.find("{")
        if opening < 0 or not call_pattern.search(body[opening + 1:]):
            continue
        for item in classify_method(text, path, caller_span):
            indirect.append({
                **item,
                "priority": "MEDIUM",
                "binding": "ONE_LOCAL_CALLER_HOP",
                "reason": (
                    "Changed helper is called directly by an external inbound "
                    "method in the same class; deeper call-chain proof is not required")})

    type_names = _declared_callable_types(text)
    upper = typed_method_callers(
        files, type_names, span["name"], "ONE_TYPED_CALLER_HOP")
    for caller in upper:
        for item in _classify_caller(files, caller, "ONE_TYPED_CALLER_HOP"):
            indirect.append({
                **item,
                "priority": "MEDIUM",
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
            for item in _classify_caller(
                    files, upper, "MYBATIS_ONE_TYPED_CALLER_HOP"):
                result.append({**item, "priority": "MEDIUM"})
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


def analyze_external_influence(entries, ledgers, cache_dir, query_contract=None):
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
            observations, influences = [], []
            for side, path, source, rng in (
                    ("base", change.get("old_path"), base,
                     change.get("old_range")),
                    ("head", change.get("path"), head,
                     change.get("new_range"))):
                text = source.get(path or "")
                if not text:
                    continue
                if path.endswith(".java"):
                    if '/src/test/' in '/' + path:
                        continue
                    for span in java_method_spans(text):
                        start_line = _line(text, span["start"])
                        end_line = _line(
                            text, max(span["start"], span["end"] - 1))
                        if not _overlap(rng, start_line, end_line):
                            continue
                        influences.append({'side': side, **method_input_influence(text, path, span, rng)})
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
            for query in (query_contract or {}).get('candidates', []):
                if query.get('project') == project and change['id'] in query.get('change_ids', []):
                    influence = query.get('input_influence')
                    if influence:
                        influences.append({'side': 'head', **influence})
            if not unique and not influences:
                continue
            surfaces = sorted(
                unique.values(),
                key=lambda item: (
                    item["side"], item.get("binding", ""),
                    item["external_kind"], item["path"], item["line"]))
            current = [item for item in influences if item.get('side') == 'head'] or influences
            influence = min(current, key=lambda item: {'HIGH': 0, 'MEDIUM': 1, 'LOW': 2}.get(item['level'], 1)) if current else {
                'level': 'MEDIUM', 'status': 'INPUT_SOURCE_UNRESOLVED',
                'reason': '已找到外部入口，但尚未绑定入口参数与本次改动表达式。',
                'parameters': [], 'evidence': []}
            priority = influence['level']
            relevant = [item for item in current if item['level'] == priority]
            methods = sorted({item['method'] for item in relevant if item.get('method')})
            input_summary = {**influence,
                             'parameters': sorted({name for item in relevant for name in item.get('parameters', [])}),
                             'effects': sorted({effect for item in relevant for effect in item.get('effects', [])}),
                             'evidence': [ref for item in relevant for ref in item.get('evidence', [])],
                             'method_assessments': influences}
            if methods:
                input_summary.update({'methods': methods, 'method': '、'.join(methods)})
            candidates.append({
                "project": project,
                "change_id": change["id"],
                "path": change.get("path"),
                "old_path": change.get("old_path"),
                "base_sha": ledger["base"],
                "head_sha": ledger["head"],
                "surfaces": surfaces,
                "priority": priority,
                'input_influence': input_summary,
                'requires_confirmation': priority == 'HIGH' or (priority == 'MEDIUM' and bool(surfaces)),
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
