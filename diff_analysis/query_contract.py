"""Read-only fixed-SHA query-result/sorting contract analysis.

The scanner is deliberately conservative:
MyBatis namespace+select id is first bound to a Java Mapper declaration. Risk
is raised when the backend can accept externally controlled sort fields and
propagate them into PageHelper/orderBy for the affected query. Front-end code
is not required for this decision.
"""
import re
from pathlib import Path

from .gitops import git
from .source_index import source_snapshot_index


VERSION = "query-contract-v4"
EXTS = {".xml", ".java"}
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
GET_ORDER = re.compile(
    r'\b([A-Za-z_]\w*)\.'
    r'(getOrderBy|getSortName|getSortField|getOrderField|getSortColumn|getOrderColumn)'
    r'\s*\(')
PAGEHELPER_DYNAMIC = re.compile(
    r'\b(?:PageHelper\s*\.\s*)?(?:startPage|orderBy)\s*\([^;\n]*getOrderBy\s*\(',
    re.I)
HTTP_MAPPING = re.compile(
    r'@(GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping|RequestMapping)'
    r'\s*(?:\(\s*(?:(?:value|path)\s*=\s*)?["\x27]([^"\x27]*)["\x27][^)]*\))?',
    re.I)
CLASS_REQUEST_MAPPING = re.compile(
    r'@RequestMapping\s*\(\s*(?:(?:value|path)\s*=\s*)?'
    r'["\x27]([^"\x27]*)["\x27][^)]*\)', re.I)


def _top_level_from_offset(sql):
    start = re.search(r'\bselect\b', sql, re.I)
    if not start:
        return None
    depth, quote, i = 0, None, start.end()
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
                return None
        elif depth == 0 and sql[i:i + 4].lower() == 'from' and (
                i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == '_')) and (
                i + 4 == len(sql) or not (sql[i + 4].isalnum() or sql[i + 4] == '_')):
            return i
        i += 1
    return None


def _projection_parts(sql):
    sql = re.sub(r'/\*[\s\S]*?\*/|--[^\n]*', ' ', sql)
    start = re.search(r'\bselect\b', sql, re.I)
    if not start:
        return [], ['UNRESOLVED_SELECT']
    end = _top_level_from_offset(sql)
    if end is None:
        return [], ['TOP_LEVEL_FROM_NOT_FOUND']
    raw = sql[start.end():end]
    depth, quote, chunks, begin = 0, None, [], 0
    for i, char in enumerate(raw):
        if quote:
            if char == quote:
                quote = None
        elif char in ('"', "'", chr(96)):
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth < 0:
                return [], ['UNBALANCED_PARENTHESES']
        elif char == ',' and depth == 0:
            chunks.append(raw[begin:i].strip())
            begin = i + 1
    chunks.append(raw[begin:].strip())
    if depth or quote:
        return [], ['UNBALANCED_EXPRESSION']
    return chunks, []


def _simple_column(expression):
    cleaned = expression.strip().replace(chr(96), '')
    match = re.fullmatch(
        r'(?:[A-Za-z_]\w*\.)?([A-Za-z_]\w*)', cleaned)
    return match.group(1).lower() if match else None


def projection_details(sql):
    """Return output names together with source expressions and alias kinds."""
    parts, gaps = _projection_parts(sql)
    if gaps:
        return [], gaps
    items = []
    for index, raw_part in enumerate(parts):
        part = raw_part.strip()
        if index == 0:
            part = re.sub(r'^DISTINCT\s+', '', part, flags=re.I).strip()
        explicit = re.search(
            r'\s+AS\s+([\x60"]?[A-Za-z_]\w*[\x60"]?)$', part, re.I)
        if explicit:
            output = explicit.group(1).strip(chr(96) + '"').lower()
            expression = part[:explicit.start()].strip()
            kind = ('COLUMN_ALIAS' if _simple_column(expression)
                    else 'EXPRESSION_ALIAS')
            items.append({'output': output, 'expression': expression,
                          'kind': kind})
            continue
        simple = _simple_column(part)
        if simple:
            items.append({'output': simple, 'expression': part,
                          'kind': 'SIMPLE_COLUMN'})
            continue
        implicit_column = re.fullmatch(
            r'((?:[A-Za-z_]\w*\.)?[A-Za-z_]\w*)\s+'
            r'([A-Za-z_]\w*)', part.replace(chr(96), ''))
        if implicit_column:
            items.append({'output': implicit_column.group(2).lower(),
                          'expression': implicit_column.group(1),
                          'kind': 'COLUMN_ALIAS'})
            continue
        implicit_expression = re.fullmatch(
            r'(.+(?:\)|\bEND|[\x27"]|\d))\s+([A-Za-z_]\w*)',
            part, re.I)
        if implicit_expression:
            items.append({'output': implicit_expression.group(2).lower(),
                          'expression': implicit_expression.group(1).strip(),
                          'kind': 'EXPRESSION_ALIAS'})
            continue
        gaps.append('WILDCARD_OR_COMPLEX_EXPRESSION')
    return items, sorted(set(gaps))


def projection(sql):
    """Compatibility wrapper returning only parsed output names and gaps."""
    items, gaps = projection_details(sql)
    return sorted(set(item['output'] for item in items)), gaps


def _normalize_expression(value):
    return re.sub(r'\s+', '', value or '').replace(chr(96), '').lower()


def _column_change_assessment(old_item, current_items):
    same_expression = next((
        item for item in current_items
        if _normalize_expression(item.get('expression')) ==
           _normalize_expression(old_item.get('expression'))
        and item.get('output') != old_item.get('output')), None)
    change_kind = 'OUTPUT_ALIAS_RENAMED' if same_expression else 'OUTPUT_COLUMN_REMOVED'
    replacement = same_expression.get('output') if same_expression else None
    source_column = _simple_column(old_item.get('expression') or '')
    if source_column and source_column == old_item.get('output'):
        sql_sort_assessment = 'BASE_COLUMN_MAY_STILL_BE_ORDERABLE'
    else:
        sql_sort_assessment = 'REMOVED_ALIAS_OR_EXPRESSION_HAS_HIGHER_ORDER_RISK'
    return change_kind, replacement, sql_sort_assessment



def selects(text, path):
    mapper = MAPPER.search(text)
    namespace = dict(ATTR.findall(mapper.group(1))).get('namespace', '') if mapper else ''
    result = {}
    for match in SELECT.finditer(text):
        attrs = dict(ATTR.findall(match.group(1)))
        if 'id' not in attrs:
            continue
        body = match.group(2)
        visible = re.sub(r'<[^>]*>', lambda item: ' ' * len(item.group()), body)
        projection_items, gaps = projection_details(visible)
        cols = sorted(set(item['output'] for item in projection_items))
        from_offset = _top_level_from_offset(visible)
        if from_offset is None:
            gaps.append('UNRESOLVED_SELECT_BOUNDARY')
        elif any(item.start() < from_offset for item in DYNAMIC_SQL.finditer(body)):
            gaps.append('DYNAMIC_SELECT_PROJECTION')
        result[(namespace, attrs['id'])] = {
            'path': path,
            'line': text.count('\n', 0, match.start()) + 1,
            'end_line': text.count('\n', 0, match.end()) + 1,
            'columns': cols,
            'projection_items': projection_items,
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


def _paren_end(text, opening):
    depth = 0
    for pos in range(opening, len(text)):
        if text[pos] == '(':
            depth += 1
        elif text[pos] == ')':
            depth -= 1
            if depth == 0:
                return pos
    return None


def _split_java_parameters(raw):
    parts, begin = [], 0
    angle = paren = bracket = 0
    for index, char in enumerate(raw):
        if char == '<':
            angle += 1
        elif char == '>':
            angle = max(0, angle - 1)
        elif char == '(':
            paren += 1
        elif char == ')':
            paren = max(0, paren - 1)
        elif char == '[':
            bracket += 1
        elif char == ']':
            bracket = max(0, bracket - 1)
        elif char == ',' and not angle and not paren and not bracket:
            parts.append(raw[begin:index].strip())
            begin = index + 1
    tail = raw[begin:].strip()
    if tail:
        parts.append(tail)
    return parts


def _method_parameters(raw):
    result = {}
    for part in _split_java_parameters(raw):
        cleaned = re.sub(
            r'@[A-Za-z_][\w.]*\s*(?:\([^)]*\))?\s*', '', part)
        cleaned = re.sub(r'\b(?:final|volatile|transient)\b\s*', '', cleaned)
        match = re.search(r'([A-Za-z_]\w*)\s*$', cleaned)
        if not match:
            continue
        name = match.group(1)
        type_text = cleaned[:match.start()].strip()
        type_text = type_text.replace('...', '[]').strip()
        if type_text:
            result[name] = type_text.split('.')[-1]
    return result


def java_method_spans(text):
    """Return conservative Java method ranges and parameter types."""
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
        param_open = masked.find('(', match.end(1), opening)
        param_close = _paren_end(masked, param_open) if param_open >= 0 else None
        parameters = (_method_parameters(text[param_open + 1:param_close])
                      if param_close is not None else {})
        if ending:
            spans.append({
                'name': match.group(1), 'start': match.start(),
                'end': ending, 'line': _line(text, match.start()),
                'parameters': parameters})
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
        for keyword in ('extends', 'implements'):
            clause = re.search(
                r'\b' + keyword + r'\s+(.+?)(?=\bextends\b|\bimplements\b|$)',
                tail)
            if not clause:
                continue
            for item in clause.group(1).split(','):
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
                    'caller_parameters': span.get('parameters', {}) if span else {},
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
        parameters = caller.get('caller_parameters') or {}
        receiver_type = parameters.get(receiver) or _variable_type(
            text, receiver, start, end)
        dynamic.append({
            'path': path, 'line': line,
            'kind': ('DYNAMIC_GET_ORDER_BY' if match.group(2) == 'getOrderBy'
                     else 'DYNAMIC_SORT_FIELD_GETTER'),
            'getter': match.group(2),
            'receiver': receiver,
            'receiver_type': receiver_type,
            'receiver_source': ('METHOD_PARAMETER'
                                if receiver in parameters
                                else 'LOCAL_OR_FIELD'),
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
    dynamic_kinds = (
        'DYNAMIC_GET_ORDER_BY', 'DYNAMIC_SORT_FIELD_GETTER',
        'PAGEHELPER_DYNAMIC_SORT')
    direct = [item for item in unique.values()
              if item['kind'] not in dynamic_kinds]
    dynamic = [item for item in unique.values()
               if item['kind'] in dynamic_kinds]
    return direct, dynamic


def _join_http_path(base, method):
    base = (base or '').strip()
    method = (method or '').strip()
    if not base and not method:
        return '/'
    value = '/'.join(part.strip('/') for part in (base, method) if part.strip('/'))
    return '/' + value if value else '/'


def _class_request_path(text):
    type_match = re.search(r'\b(?:class|interface)\s+[A-Za-z_]\w*\b', _mask_java(text))
    if not type_match:
        return ''
    mappings = [
        match for match in CLASS_REQUEST_MAPPING.finditer(text)
        if match.start() < type_match.start()]
    return mappings[-1].group(1) if mappings else ''


def _endpoint_for_caller(text, caller):
    if caller.get('caller_start') is None or caller.get('caller_end') is None:
        return None
    fragment = text[caller['caller_start']:caller['caller_end']]
    opening = fragment.find('{')
    header = fragment if opening < 0 else fragment[:opening]
    mapping = HTTP_MAPPING.search(header)
    if not mapping:
        return None
    base = _class_request_path(text)
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
        'request_parameters': caller.get('caller_parameters') or {},
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


def _sort_parameter_capability(files, type_name):
    """Describe whether a request/service parameter type can choose sort field/order."""
    if not type_name:
        return None
    for path, text in files.items():
        if not path.endswith('.java'):
            continue
        _, simple = _java_identity(text)
        if simple != type_name:
            continue
        field_pattern = (
            r'\b(?:String\s+)?'
            r'(sortName|sortField|orderField|sortColumn|orderColumn)\b')
        has_sort_name = bool(re.search(field_pattern, text, re.I))
        has_sort_type = bool(re.search(
            r'\b(?:String\s+)?(?:sortType|sortOrder|orderType|direction)\b',
            text, re.I))
        get_order = re.search(
            r'\b(?:String\s+)?getOrderBy\s*\([^)]*\)\s*\{', text)
        getter_control = re.search(
            r'\bget(?:SortName|SortField|OrderField|SortColumn|OrderColumn)\s*\(',
            text)
        evidence = []
        for pattern, kind in (
                (field_pattern, 'SORT_FIELD_MEMBER'),
                (r'\b(?:String\s+)?(?:sortType|sortOrder|orderType|direction)\b',
                 'SORT_DIRECTION_MEMBER'),
                (r'\bgetOrderBy\s*\(', 'GET_ORDER_BY_METHOD')):
            match = re.search(pattern, text)
            if match:
                evidence.append({
                    'path': path, 'line': _line(text, match.start()), 'kind': kind,
                    'evidence': text.splitlines()[_line(text, match.start()) - 1].strip()[:250]})
        return {
            'type': type_name,
            'path': path,
            'has_sort_name': has_sort_name,
            'has_sort_type': has_sort_type,
            'has_get_order_by': bool(get_order),
            'externally_selectable_field': bool(
                has_sort_name and (get_order or getter_control)),
            'externally_selectable_direction': bool(
                has_sort_type and (get_order or getter_control)),
            'evidence': evidence}
    return None


def _external_sort_capability(dynamic_refs, endpoints, capabilities):
    """Risk is about backend capability, not proof that one frontend currently sends a field."""
    parameter_refs = [
        item for item in dynamic_refs
        if item.get('receiver_source') == 'METHOD_PARAMETER']
    selectable = [
        item for item in capabilities
        if item and item.get('externally_selectable_field')]
    if parameter_refs and endpoints and selectable:
        level = 'EXTERNAL_HTTP_DYNAMIC_SORT_CONFIRMED_STATIC'
    elif parameter_refs and selectable:
        level = 'METHOD_PARAMETER_DYNAMIC_SORT_CAPABILITY'
    elif parameter_refs:
        level = 'METHOD_PARAMETER_GET_ORDER_BY_UNRESOLVED_TYPE'
    else:
        return None
    return {
        'status': level,
        'http_exposed': bool(endpoints),
        'parameter_sort_refs': parameter_refs,
        'parameter_types': sorted(set(
            item.get('receiver_type') for item in parameter_refs
            if item.get('receiver_type'))),
        'sort_parameter_capabilities': selectable}



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
    changed_xml = {}
    for ledger in ledgers:
        paths = set()
        for change in ledger.get('final_changes', []):
            for path in (change.get('path'), change.get('old_path')):
                if path and path.endswith('.xml'):
                    paths.add(path)
        changed_xml[ledger['project']] = paths

    for project, entry in sorted(entries.items()):
        # Consumer evidence only needs HEAD. BASE content is required solely for
        # changed MyBatis XML in projects that are part of this comparison.
        sides = ('base', 'head') if project in changed_xml else ('head',)
        for side in sides:
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
                    if side == 'base' and item['path'] not in changed_xml.get(project, set()):
                        continue
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
        xml_pairs = []
        for file_info in ledger.get('final_files', []):
            new_path = file_info.get('path')
            old_path = file_info.get('old_path') or new_path
            if (new_path and old_path and new_path.endswith('.xml')
                    and old_path in old and new_path in new):
                xml_pairs.append((old_path, new_path))
        if not xml_pairs:
            xml_pairs = [(path, path) for path in sorted(set(old) & set(new))
                         if path.endswith('.xml')]

        for old_path, path in sorted(set(xml_pairs)):
            before = selects(old[old_path], old_path)
            after = selects(new[path], path)
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
                endpoints = controller_endpoints(new, callers)
                if not declaration:
                    omissions.append({
                        'project': project, 'path': path, 'select': key[1],
                        'reason': 'MAPPER_METHOD_NOT_BOUND', 'namespace': key[0]})

                for column in sorted(set(previous['columns']) - set(current['columns'])):
                    old_item = next(
                        item for item in previous.get('projection_items', [])
                        if item.get('output') == column)
                    change_kind, replacement_column, sql_sort_assessment = (
                        _column_change_assessment(
                            old_item, current.get('projection_items', [])))
                    bound_direct, bound_dynamic = [], []
                    sort_capabilities = []
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
                                capability = _sort_parameter_capability(
                                    new, item['receiver_type'])
                                if capability and capability not in sort_capabilities:
                                    sort_capabilities.append(capability)

                    project_refs = []
                    for source_path, source in new.items():
                        if Path(source_path).suffix.lower() in EXTS:
                            project_refs.extend(scan_references(source, source_path, column))

                    external_sort = _external_sort_capability(
                        bound_dynamic, endpoints, sort_capabilities)

                    if external_sort and external_sort.get('http_exposed'):
                        status = 'EXTERNAL_REQUEST_DYNAMIC_SORT_CAPABILITY'
                        priority = 'HIGH'
                    elif bound_direct:
                        status = 'BOUND_CALLER_LITERAL_SORT'
                        priority = 'HIGH'
                    elif external_sort:
                        status = 'BACKEND_DYNAMIC_SORT_CAPABILITY'
                        priority = 'MEDIUM'
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
                        'column_change_kind': change_kind,
                        'replacement_column': replacement_column,
                        'deleted_projection': old_item,
                        'sql_sort_assessment': sql_sort_assessment,
                        'change_ids': _change_ids(
                            ledger, path, previous, current),
                        'old': previous,
                        'new': current,
                        'mapper_declaration': declaration,
                        'mapper_callers': callers[:MAX_REFERENCES],
                        'controller_endpoints': endpoints[:MAX_REFERENCES],
                        'bound_sort_references': bound_direct[:MAX_REFERENCES],
                        'dynamic_sort_references': bound_dynamic[:MAX_REFERENCES],
                        'external_sort_capability': external_sort,
                        'sort_parameter_capabilities': sort_capabilities,
                        'project_references': project_refs[:MAX_REFERENCES],
                        'priority': priority,
                        'status': status,
                        'limitation': (
                            'SELECT projection removal does not prove ORDER BY invalid. '
                            'Risk is raised when backend sorting can be controlled by '
                            'method/request parameters; frontend path reconstruction is not required.')}
                    candidates.append(candidate)

                    for name, values in (
                            ('mapper_callers', callers),
                            ('controller_endpoints', endpoints),
                            ('project_references', project_refs),
                            ('sort_parameter_capabilities', sort_capabilities)):
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
