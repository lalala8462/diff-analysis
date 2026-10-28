"""Read-only fixed-SHA query-result/sorting contract analysis.

The scanner is deliberately conservative:
MyBatis namespace+select id is first bound to a Java Mapper declaration, then
Mapper calls and sorting propagation in the same Java caller method are used
to strengthen a candidate. Front-end matches remain supplemental evidence
until an HTTP endpoint/request binding is proved.
"""
import re
from pathlib import Path

from .gitops import git
from .source_index import source_snapshot_index


VERSION = "query-contract-v2"
EXTS = {".xml", ".java", ".vue", ".js", ".jsx", ".ts", ".tsx"}
MAX_FILES = 4000
MAX_FILE_BYTES = 500000
MAX_REFERENCES = 30

SELECT = re.compile(r'<select\b([^>]*)>(.*?)</select\s*>', re.I | re.S)
ATTR = re.compile(r'\b(id|namespace)\s*=\s*["\x27]([^"\x27]+)', re.I)
MAPPER = re.compile(r'<mapper\b([^>]*)>', re.I)
DYNAMIC_SQL = re.compile(
    r'<(?:if|choose|when|otherwise|foreach|include|trim|bind)\b|\$\{|#\{', re.I)
PACKAGE = re.compile(r'\bpackage\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*;')
TYPE = re.compile(r'\b(?:class|interface)\s+([A-Za-z_]\w*)\b')
JAVA_FIELD_SORT = re.compile(
    r'\b(?:sortName|sortBy|orderBy)\b\s*=\s*["\x27]([A-Za-z_]\w*)'
    r'|setSortName\s*\(\s*["\x27]([A-Za-z_]\w*)["\x27]\s*\)', re.I)
JAVA_ORDER_LITERAL = re.compile(
    r'\bORDER\s+BY\s+([A-Za-z_]\w*)(?:\s+(?:ASC|DESC))?\b', re.I)
PAGEHELPER_LITERAL = re.compile(
    r'\b(?:startPage|orderBy)\s*\([^;\n]*?["\x27]([A-Za-z_]\w*)\s+'
    r'(?:asc|desc)["\x27]', re.I)
FRONT_SORT = re.compile(
    r'\b(?:sortName|sortBy|orderBy|prop)\b\s*(?:=|:)\s*["\x27]([A-Za-z_]\w*)["\x27]'
    r'|\b(?:sort-name|sort-by|order-by|prop)\s*=\s*["\x27]([A-Za-z_]\w*)["\x27]',
    re.I)
GET_ORDER = re.compile(r'\b([A-Za-z_]\w*)\.getOrderBy\s*\(')
PAGEHELPER_DYNAMIC = re.compile(
    r'\b(?:PageHelper\s*\.\s*)?(?:startPage|orderBy)\s*\([^;\n]*getOrderBy\s*\(',
    re.I)
HTTP_MAPPING = re.compile(
    r'@(GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping|RequestMapping)'
    r'\s*(?:\(\s*(?:(?:value|path)\s*=\s*)?["\x27]([^"\x27]*)["\x27][^)]*\))?',
    re.I)
CLASS_REQUEST_MAPPING = re.compile(
    r'@RequestMapping\s*\(\s*(?:(?:value|path)\s*=\s*)?'
    r'["\x27]([^"\x27]*)["\x27][^)]*\)\s*'
    r'(?:public\s+)?(?:abstract\s+)?(?:class|interface)\s+[A-Za-z_]\w*',
    re.I | re.S)


def projection(sql):
    """Parse only a top-level SELECT list; uncertain expressions become gaps."""
    sql = re.sub(r'/\*[\s\S]*?\*/|--[^\n]*', ' ', sql)
    start = re.search(r'\bselect\b', sql, re.I)
    if not start:
        return [], ['UNRESOLVED_SELECT']
    depth, quote, chunks, begin, i = 0, None, [], start.end(), start.end()
    while i < len(sql):
        char = sql[i]
        if quote:
            if char == quote:
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    i += 2
                    continue
                quote = None
        elif char in ('"', "'", chr(96)):
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth < 0:
                return [], ['UNBALANCED_PARENTHESES']
        elif depth == 0 and sql[i:i + 4].lower() == 'from' and (
                i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == '_')) and (
                i + 4 == len(sql) or not (sql[i + 4].isalnum() or sql[i + 4] == '_')):
            chunks.append(sql[begin:i].strip())
            break
        elif char == ',' and depth == 0:
            chunks.append(sql[begin:i].strip())
            begin = i + 1
        i += 1
    else:
        return [], ['TOP_LEVEL_FROM_NOT_FOUND']
    if depth or quote:
        return [], ['UNBALANCED_EXPRESSION']
    cols, gaps = [], []
    for part in chunks:
        alias = re.search(r'\bAS\s+([A-Za-z_]\w*)$', part, re.I)
        simple = re.fullmatch(r'(?:[A-Za-z_]\w*\.)?([A-Za-z_]\w*)', part)
        implicit = re.fullmatch(
            r'(?:[A-Za-z_]\w*\.)?[A-Za-z_]\w*\s+([A-Za-z_]\w*)', part)
        chosen = alias or simple or implicit
        if chosen:
            cols.append(chosen.group(1).lower())
        else:
            gaps.append('WILDCARD_OR_COMPLEX_EXPRESSION')
    return sorted(set(cols)), sorted(set(gaps))


def selects(text, path):
    mapper = MAPPER.search(text)
    namespace = dict(ATTR.findall(mapper.group(1))).get('namespace', '') if mapper else ''
    result = {}
    for match in SELECT.finditer(text):
        attrs = dict(ATTR.findall(match.group(1)))
        if 'id' not in attrs:
            continue
        body = match.group(2)
        cols, gaps = projection(re.sub(r'<[^>]*>', ' ', body))
        if DYNAMIC_SQL.search(body):
            gaps.append('DYNAMIC_SQL')
        result[(namespace, attrs['id'])] = {
            'path': path,
            'line': text.count('\n', 0, match.start()) + 1,
            'end_line': text.count('\n', 0, match.end()) + 1,
            'columns': cols,
            'gaps': sorted(set(gaps)),
            'sql_excerpt': re.sub(r'\s+', ' ', body).strip()[:600]}
    return result


def _line(text, offset):
    return text.count('\n', 0, offset) + 1


def _evidence(text, path, match, kind):
    line = _line(text, match.start())
    lines = text.splitlines()
    return {'path': path, 'line': line, 'kind': kind,
            'evidence': lines[line - 1].strip()[:250] if line <= len(lines) else ''}


def scan_references(text, path, column):
    """Find literal result/sort references; these are evidence, not bindings."""
    suffix = Path(path).suffix.lower()
    found = []
    patterns = []
    if suffix == '.java':
        patterns = [(JAVA_FIELD_SORT, 'JAVA_SORT_LITERAL'),
                    (JAVA_ORDER_LITERAL, 'JAVA_SQL_ORDER_LITERAL'),
                    (PAGEHELPER_LITERAL, 'PAGEHELPER_LITERAL_SORT')]
    elif suffix in {'.vue', '.js', '.jsx', '.ts', '.tsx'}:
        patterns = [(FRONT_SORT, 'FRONTEND_SORT_FIELD')]
    for pattern, kind in patterns:
        for match in pattern.finditer(text):
            value = next((group for group in match.groups() if group), '')
            if value.lower() == column:
                found.append(_evidence(text, path, match, kind))
    unique = {}
    for item in found:
        unique[(item['path'], item['line'], item['kind'], item['evidence'])] = item
    return list(unique.values())


def _java_identity(text):
    package = PACKAGE.search(text)
    typ = TYPE.search(text)
    if not typ:
        return '', ''
    simple = typ.group(1)
    prefix = package.group(1) + '.' if package else ''
    return prefix + simple, simple


def _mask_java(text):
    """Mask comments and strings while preserving offsets and newlines."""
    chars = list(text)
    i, quote = 0, None
    while i < len(chars):
        if quote:
            if chars[i] == '\\' and i + 1 < len(chars):
                if chars[i] != '\n':
                    chars[i] = ' '
                if chars[i + 1] != '\n':
                    chars[i + 1] = ' '
                i += 2
                continue
            current = chars[i]
            if current == quote:
                chars[i] = ' '
                quote = None
            elif current != '\n':
                chars[i] = ' '
            i += 1
            continue
        if chars[i] in ('"', "'"):
            quote = chars[i]
            chars[i] = ' '
            i += 1
            continue
        if i + 1 < len(chars) and chars[i] == '/' and chars[i + 1] == '/':
            chars[i] = chars[i + 1] = ' '
            i += 2
            while i < len(chars) and chars[i] != '\n':
                chars[i] = ' '
                i += 1
            continue
        if i + 1 < len(chars) and chars[i] == '/' and chars[i + 1] == '*':
            chars[i] = chars[i + 1] = ' '
            i += 2
            while i + 1 < len(chars) and not (
                    chars[i] == '*' and chars[i + 1] == '/'):
                if chars[i] != '\n':
                    chars[i] = ' '
                i += 1
            if i + 1 < len(chars):
                chars[i] = chars[i + 1] = ' '
                i += 2
            continue
        i += 1
    return ''.join(chars)


def _brace_end(masked, opening):
    depth = 0
    for pos in range(opening, len(masked)):
        if masked[pos] == '{':
            depth += 1
        elif masked[pos] == '}':
            depth -= 1
            if depth == 0:
                return pos + 1
    return None


def java_method_spans(text):
    """Return conservative Java method ranges, excluding declarations without bodies."""
    masked = _mask_java(text)
    pattern = re.compile(
        r'(?m)^[ \t]*(?:@[A-Za-z_][\w.]*[^\n]*\n[ \t]*)*'
        r'(?:(?:public|protected|private|static|final|synchronized|default|native)\s+)*'
        r'(?:[A-Za-z_][\w.$<>\[\],? \t]*\s+)+([A-Za-z_]\w*)\s*'
        r'\([^;{}]*\)\s*(?:throws\s+[^{]+)?\{')
    spans = []
    for match in pattern.finditer(masked):
        opening = masked.find('{', match.start(), match.end())
        ending = _brace_end(masked, opening)
        if ending:
            spans.append({'name': match.group(1), 'start': match.start(),
                          'end': ending, 'line': _line(text, match.start())})
    return spans


def _enclosing_method(spans, offset):
    candidates = [span for span in spans if span['start'] <= offset < span['end']]
    return min(candidates, key=lambda item: item['end'] - item['start']) if candidates else None


def mapper_declaration(files, namespace, select_id):
    """Bind MyBatis namespace/select id to a Java mapper/interface declaration."""
    if not namespace:
        return None
    method_pattern = re.compile(r'\b' + re.escape(select_id) +
                                r'\s*\([^;{}]*\)\s*(?:throws\s+[^;{]+)?[;{]')
    for path, text in files.items():
        if not path.endswith('.java'):
            continue
        fqcn, simple = _java_identity(text)
        if fqcn != namespace:
            continue
        match = method_pattern.search(_mask_java(text))
        if match:
            return {'namespace': namespace, 'class': simple, 'method': select_id,
                    'path': path, 'line': _line(text, match.start()),
                    'binding': 'NAMESPACE_SELECT_TO_JAVA_METHOD'}
    return None


def _declared_callable_types(text):
    """Primary Java type plus directly implemented/extended interface names."""
    fqcn, simple = _java_identity(text)
    if not simple:
        return []
    result = [simple]
    masked = _mask_java(text)
    match = re.search(
        r'\b(?:class|interface)\s+' + re.escape(simple) +
        r'\b([^\{]*)\{', masked)
    if match:
        tail = match.group(1)
        for clause in re.findall(r'\b(?:implements|extends)\s+([^\{]+)', tail):
            for item in clause.split(','):
                name = re.sub(r'<.*', '', item).strip().split('.')[-1]
                if re.fullmatch(r'[A-Za-z_]\w*', name):
                    result.append(name)
    return sorted(set(result))


def typed_method_callers(files, type_names, method, binding):
    """Find calls through variables with one of the requested declared types."""
    type_names = sorted(set(name for name in type_names if name))
    if not type_names:
        return []
    callers = []
    alternation = '|'.join(re.escape(name) for name in type_names)
    variable_pattern = re.compile(
        r'\b(?:' + alternation + r')'
        r'(?:\s*<[^;=(){}]+>)?\s+([A-Za-z_]\w*)\b')
    for path, text in files.items():
        if not path.endswith('.java'):
            continue
        masked = _mask_java(text)
        variables = sorted(set(variable_pattern.findall(masked)))
        if not variables:
            continue
        spans = java_method_spans(text)
        fqcn, simple = _java_identity(text)
        callable_types = _declared_callable_types(text)
        for variable in variables:
            call_pattern = re.compile(r'\b' + re.escape(variable) + r'\s*\.\s*' +
                                      re.escape(method) + r'\s*\(')
            for match in call_pattern.finditer(masked):
                span = _enclosing_method(spans, match.start())
                callers.append({
                    'path': path,
                    'line': _line(text, match.start()),
                    'caller_method': span['name'] if span else None,
                    'caller_start': span['start'] if span else None,
                    'caller_end': span['end'] if span else None,
                    'caller_class': simple or None,
                    'caller_fqcn': fqcn or None,
                    'caller_types': callable_types,
                    'target_types': type_names,
                    'receiver_variable': variable,
                    'binding': binding,
                    'evidence': text.splitlines()[_line(text, match.start()) - 1].strip()[:250]})
    unique = {}
    for item in callers:
        key = (item['path'], item['line'], item['caller_method'],
               item['receiver_variable'], tuple(item['target_types']))
        unique[key] = item
    return list(unique.values())


def mapper_callers(files, declaration):
    """Find source calls to the namespace-bound Mapper method."""
    if not declaration:
        return []
    callers = typed_method_callers(
        files, [declaration['class']], declaration['method'],
        'DECLARED_MAPPER_TYPE_AND_METHOD_CALL')
    for item in callers:
        item['mapper_variable'] = item['receiver_variable']
    return callers


def _variable_type(text, variable, start, end):
    fragment = _mask_java(text[start:end])
    matches = list(re.finditer(
        r'\b([A-Za-z_]\w*(?:\s*<[^;=(){}]+>)?)\s+' +
        re.escape(variable) + r'\b', fragment))
    if not matches:
        return None
    value = re.sub(r'\s*<.*', '', matches[-1].group(1)).strip()
    return value.split('.')[-1]


def caller_sort_evidence(text, path, caller, column):
    """Sorting evidence is strong only when it occurs in the bound mapper caller."""
    if caller.get('caller_start') is None or caller.get('caller_end') is None:
        return [], []
    start, end = caller['caller_start'], caller['caller_end']
    fragment = text[start:end]
    direct = scan_references(fragment, path, column)
    for item in direct:
        item['line'] += text.count('\n', 0, start)
        item['binding'] = 'SAME_METHOD_AS_MAPPER_CALL'
    dynamic = []
    for match in GET_ORDER.finditer(fragment):
        line = _line(text, start + match.start())
        receiver = match.group(1)
        dynamic.append({
            'path': path, 'line': line, 'kind': 'DYNAMIC_GET_ORDER_BY',
            'receiver': receiver,
            'receiver_type': _variable_type(text, receiver, start, end),
            'binding': 'SAME_METHOD_AS_MAPPER_CALL',
            'evidence': text.splitlines()[line - 1].strip()[:250]})
    for match in PAGEHELPER_DYNAMIC.finditer(fragment):
        line = _line(text, start + match.start())
        dynamic.append({
            'path': path, 'line': line, 'kind': 'PAGEHELPER_DYNAMIC_SORT',
            'binding': 'SAME_METHOD_AS_MAPPER_CALL',
            'evidence': text.splitlines()[line - 1].strip()[:250]})
    unique = {}
    for item in direct + dynamic:
        key = (item['path'], item['line'], item['kind'], item.get('receiver'))
        unique[key] = item
    direct = [item for item in unique.values()
              if item['kind'] not in ('DYNAMIC_GET_ORDER_BY', 'PAGEHELPER_DYNAMIC_SORT')]
    dynamic = [item for item in unique.values()
               if item['kind'] in ('DYNAMIC_GET_ORDER_BY', 'PAGEHELPER_DYNAMIC_SORT')]
    return direct, dynamic


def _join_http_path(base, method):
    base = (base or '').strip()
    method = (method or '').strip()
    if not base and not method:
        return '/'
    value = '/'.join(part.strip('/') for part in (base, method) if part.strip('/'))
    return '/' + value if value else '/'


def _endpoint_for_caller(text, caller):
    if caller.get('caller_start') is None or caller.get('caller_end') is None:
        return None
    fragment = text[caller['caller_start']:caller['caller_end']]
    opening = fragment.find('{')
    header = fragment if opening < 0 else fragment[:opening]
    mapping = HTTP_MAPPING.search(header)
    if not mapping:
        return None
    class_mapping = CLASS_REQUEST_MAPPING.search(text)
    base = class_mapping.group(1) if class_mapping else ''
    annotation = mapping.group(1)
    path = mapping.group(2) or ''
    http_method = annotation[:-7].upper() if annotation.lower().endswith('mapping') else annotation.upper()
    if annotation.lower() == 'requestmapping':
        http_method = 'REQUEST'
    return {
        'path': caller['path'],
        'line': caller.get('line'),
        'controller_class': caller.get('caller_class'),
        'controller_method': caller.get('caller_method'),
        'http_method': http_method,
        'url': _join_http_path(base, path),
        'binding': 'TYPED_SERVICE_CALL_IN_MAPPED_CONTROLLER',
        'evidence': caller.get('evidence', '')}


def controller_endpoints(files, mapper_callers_list):
    """Trace one typed caller hop from Mapper caller method to mapped Controller."""
    endpoints = []
    for mapper_caller in mapper_callers_list:
        source = files.get(mapper_caller.get('path'), '')
        direct = _endpoint_for_caller(source, mapper_caller)
        if direct:
            direct['service_binding'] = 'MAPPER_CALLED_DIRECTLY_FROM_CONTROLLER'
            endpoints.append(direct)
        method = mapper_caller.get('caller_method')
        types = mapper_caller.get('caller_types') or []
        if not method or not types:
            continue
        for upper in typed_method_callers(
                files, types, method, 'TYPED_SERVICE_METHOD_CALL'):
            endpoint = _endpoint_for_caller(files.get(upper['path'], ''), upper)
            if endpoint:
                endpoint['service_types'] = types
                endpoint['service_method'] = method
                endpoint['service_binding'] = 'TYPED_SERVICE_METHOD_CALL'
                endpoints.append(endpoint)
    unique = {}
    for item in endpoints:
        key = (item['path'], item['controller_method'], item['http_method'], item['url'])
        unique[key] = item
    return list(unique.values())


def endpoint_frontend_references(snapshots, entries, endpoints, column):
    """Bind exact endpoint literal and sort field only when both appear in one front-end file."""
    result = []
    urls = sorted(set(item.get('url') for item in endpoints if item.get('url') not in (None, '', '/')))
    if not urls:
        return result
    for (project, side), sources in snapshots.items():
        if side != 'head':
            continue
        for path, text in sources.items():
            if Path(path).suffix.lower() not in {'.vue', '.js', '.jsx', '.ts', '.tsx'}:
                continue
            refs = scan_references(text, path, column)
            if not refs:
                continue
            for url in urls:
                literal = re.search(r'["\x27]' + re.escape(url) + r'["\x27]', text)
                if not literal:
                    continue
                for ref in refs:
                    result.append({
                        'project': project,
                        'sha': entries[project]['head'],
                        'path': path,
                        'line': ref['line'],
                        'kind': ref['kind'],
                        'url': url,
                        'binding': 'HTTP_PATH_AND_SORT_FIELD_SAME_FILE',
                        'evidence': ref['evidence']})
    unique = {}
    for item in result:
        key = (item['project'], item['path'], item['line'], item['url'], item['kind'])
        unique[key] = item
    return list(unique.values())


def _type_sort_sources(files, type_name, column):
    if not type_name:
        return []
    result = []
    for path, text in files.items():
        if not path.endswith('.java'):
            continue
        _, simple = _java_identity(text)
        if simple == type_name:
            result.extend(scan_references(text, path, column))
    return result


def _change_ids(ledger, path, old_select, new_select):
    result = []
    for change in ledger.get('final_changes', []):
        if change.get('path') != path and change.get('old_path') != path:
            continue
        old_range = change.get('old_range', {})
        new_range = change.get('new_range', {})
        old_start, old_count = old_range.get('start', 0), old_range.get('count', 0)
        new_start, new_count = new_range.get('start', 0), new_range.get('count', 0)
        old_hit = old_count and old_start <= old_select['end_line'] and (
            old_start + old_count - 1 >= old_select['line'])
        new_hit = new_count and new_start <= new_select['end_line'] and (
            new_start + new_count - 1 >= new_select['line'])
        if old_hit or new_hit:
            result.append(change['id'])
    return sorted(set(result))


def analyze_query_contracts(entries, ledgers, cache_dir):
    snapshots, omissions = {}, []
    for project, entry in sorted(entries.items()):
        for side in ('base', 'head'):
            sha = entry.get(side)
            if not sha:
                continue
            try:
                index, _ = source_snapshot_index(
                    entry['repo'], project, sha, Path(cache_dir) / 'indexes',
                    extensions=EXTS, rules_version=VERSION, max_files=MAX_FILES)
                omissions.extend({'project': project, 'side': side, **gap}
                                 for gap in index['omissions'])
                files = {}
                for item in index['files']:
                    raw = git(entry['repo'], 'cat-file', 'blob', item['blob_sha'])
                    if len(raw) > MAX_FILE_BYTES:
                        omissions.append({'project': project, 'side': side,
                                          'path': item['path'], 'reason': 'FILE_TOO_LARGE'})
                        continue
                    files[item['path']] = raw.decode('utf-8', 'replace')
                snapshots[(project, side)] = files
            except (OSError, RuntimeError, ValueError) as exc:
                omissions.append({'project': project, 'side': side,
                                  'reason': 'SCAN_FAILED', 'detail': type(exc).__name__})

    candidates = []
    for ledger in ledgers:
        project = ledger['project']
        old = snapshots.get((project, 'base'), {})
        new = snapshots.get((project, 'head'), {})
        for path in sorted(set(old) & set(new)):
            if not path.endswith('.xml'):
                continue
            before, after = selects(old[path], path), selects(new[path], path)
            for key in sorted(set(before) & set(after)):
                previous, current = before[key], after[key]
                if previous['gaps'] or current['gaps']:
                    omissions.append({
                        'project': project, 'path': path, 'select': key[1],
                        'reason': 'INCOMPLETE_PROJECTION',
                        'details': sorted(set(previous['gaps'] + current['gaps']))})
                    continue

                declaration = mapper_declaration(new, key[0], key[1])
                callers = mapper_callers(new, declaration)
                if not declaration:
                    omissions.append({
                        'project': project, 'path': path, 'select': key[1],
                        'reason': 'MAPPER_METHOD_NOT_BOUND', 'namespace': key[0]})

                for column in sorted(set(previous['columns']) - set(current['columns'])):
                    bound_direct, bound_dynamic = [], []
                    parameter_sources = []
                    for caller in callers:
                        source = new.get(caller['path'], '')
                        direct, dynamic = caller_sort_evidence(
                            source, caller['path'], caller, column)
                        for item in direct:
                            item['caller_method'] = caller.get('caller_method')
                            bound_direct.append(item)
                        for item in dynamic:
                            item['caller_method'] = caller.get('caller_method')
                            bound_dynamic.append(item)
                            if item.get('receiver_type'):
                                parameter_sources.extend(_type_sort_sources(
                                    new, item['receiver_type'], column))

                    project_refs = []
                    for source_path, source in new.items():
                        if Path(source_path).suffix.lower() in EXTS:
                            project_refs.extend(scan_references(source, source_path, column))

                    frontend_refs = []
                    # Front-end evidence may live in a separately registered project.
                    # It never upgrades binding strength without HTTP endpoint evidence.
                    for (consumer, side), sources in snapshots.items():
                        if side != 'head':
                            continue
                        for source_path, source in sources.items():
                            if Path(source_path).suffix.lower() in {
                                    '.vue', '.js', '.jsx', '.ts', '.tsx'}:
                                frontend_refs.extend({
                                    'project': consumer,
                                    'sha': entries[consumer]['head'],
                                    'binding': 'FRONTEND_FIELD_ONLY_UNBOUND',
                                    **item}
                                    for item in scan_references(
                                        source, source_path, column))

                    if bound_direct:
                        status = 'BOUND_CALLER_LITERAL_SORT'
                        priority = 'HIGH'
                    elif bound_dynamic and parameter_sources:
                        status = 'BOUND_DYNAMIC_SORT_WITH_PARAMETER_SOURCE'
                        priority = 'HIGH'
                    elif bound_dynamic:
                        status = 'BOUND_DYNAMIC_SORT_PARAMETER_UNRESOLVED'
                        priority = 'MEDIUM'
                    elif declaration and project_refs:
                        status = 'MAPPER_BOUND_PROJECT_REFERENCE'
                        priority = 'MEDIUM'
                    elif project_refs:
                        status = 'SAME_PROJECT_LEXICAL_REFERENCE'
                        priority = 'LOW'
                    else:
                        # A projection removal with no surviving sort use is not a
                        # query/sort contract alert.
                        continue

                    candidate = {
                        'kind': 'QUERY_RESULT_SORT_CONTRACT_CANDIDATE',
                        'project': project,
                        'base_sha': ledger['base'],
                        'head_sha': ledger['head'],
                        'namespace': key[0],
                        'select_id': key[1],
                        'path': path,
                        'deleted_column': column,
                        'change_ids': _change_ids(
                            ledger, path, previous, current),
                        'old': previous,
                        'new': current,
                        'mapper_declaration': declaration,
                        'mapper_callers': callers[:MAX_REFERENCES],
                        'bound_sort_references': bound_direct[:MAX_REFERENCES],
                        'dynamic_sort_references': bound_dynamic[:MAX_REFERENCES],
                        'parameter_sort_sources': parameter_sources[:MAX_REFERENCES],
                        'project_references': project_refs[:MAX_REFERENCES],
                        'frontend_references': frontend_refs[:MAX_REFERENCES],
                        'priority': priority,
                        'status': status,
                        'limitation': (
                            'SELECT projection removal does not prove ORDER BY invalid; '
                            'front-end references are not endpoint-bound until HTTP/request '
                            'propagation is verified.')}
                    candidates.append(candidate)

                    for name, values in (
                            ('mapper_callers', callers),
                            ('project_references', project_refs),
                            ('frontend_references', frontend_refs),
                            ('parameter_sort_sources', parameter_sources)):
                        if len(values) > MAX_REFERENCES:
                            omissions.append({
                                'project': project, 'path': path,
                                'select': key[1], 'column': column,
                                'reason': 'EVIDENCE_LIMIT', 'field': name,
                                'max_locations': MAX_REFERENCES})

    return {'schema_version': VERSION, 'candidates': candidates,
            'omissions': omissions,
            'warning': (
                'Static evidence only. HIGH means a stronger source binding, '
                'not a confirmed production defect.')}
