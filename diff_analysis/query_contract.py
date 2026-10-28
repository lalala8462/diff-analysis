"""Read-only fixed-SHA query projection and sorting contract candidates."""
import re
from pathlib import Path
from .gitops import git
from .source_index import source_snapshot_index

VERSION = "query-contract-v1"
EXTS = {".xml", ".java", ".vue", ".js", ".jsx", ".ts", ".tsx"}
SELECT = re.compile(r'<select\b([^>]*)>(.*?)</select\s*>', re.I | re.S)
ATTR = re.compile(r'\b(id|namespace)\s*=\s*["\x27]([^"\x27]+)', re.I)
MAPPER = re.compile(r'<mapper\b([^>]*)>', re.I)
SORT = re.compile(r'\b(?:sortName|sortBy|orderBy|prop)\b\s*[:=]\s*["\x27]([A-Za-z_]\w*)|setSortName\s*\(\s*["\x27]([A-Za-z_]\w*)["\x27]\s*\)|\bORDER\s+BY\s+([A-Za-z_]\w*)', re.I)
DYNAMIC = re.compile(r'<(?:if|choose|when|foreach|include|trim|bind)\b|\$\{|#\{', re.I)


def projection(sql):
    """Conservative top-level SELECT projection parser; no inferred columns."""
    sql = re.sub(r'/\\*[\\s\\S]*?\\*/|--[^\\n]*', ' ', sql)
    start = re.search(r'\\bselect\\b', sql, re.I)
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
        alias = re.search(r'\\bAS\\s+([A-Za-z_]\\w*)$', part, re.I)
        simple = re.fullmatch(r'(?:[A-Za-z_]\\w*\\.)?([A-Za-z_]\\w*)', part)
        if alias or simple:
            cols.append((alias or simple).group(1).lower())
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
        if DYNAMIC.search(body):
            gaps.append('DYNAMIC_SQL')
        result[(namespace, attrs['id'])] = {
            'path': path, 'line': text.count('\n', 0, match.start()) + 1,
            'columns': cols, 'gaps': gaps, 'sql_excerpt': body[:400]}
    return result


def scan_references(text, path, column):
    found = []
    for match in SORT.finditer(text):
        value = next((g for g in match.groups() if g), '')
        if value.lower() != column:
            continue
        line = text.count('\n', 0, match.start()) + 1
        found.append({'path': path, 'line': line,
                      'evidence': text.splitlines()[line - 1].strip()[:250]})
    if Path(path).suffix.lower() == '.java':
        # Fixed default PageHelper order expressions, e.g. startPage(1, 10, "state desc").
        for match in re.finditer(r'\b(?:startPage|orderBy)\s*\([^;\n]*?["\x27]([A-Za-z_]\w*)\s+(?:asc|desc)["\x27]', text, re.I):
            if match.group(1).lower() == column:
                line = text.count('\n', 0, match.start()) + 1
                found.append({'path': path, 'line': line,
                              'evidence': text.splitlines()[line - 1].strip()[:250]})
    return found


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
                    extensions=EXTS, rules_version=VERSION, max_files=4000)
                omissions.extend({'project': project, 'side': side, **gap}
                                 for gap in index['omissions'])
                files = {}
                for item in index['files']:
                    raw = git(entry['repo'], 'cat-file', 'blob', item['blob_sha'])
                    if len(raw) > 500000:
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
                a, b = before[key], after[key]
                if a['gaps'] or b['gaps']:
                    omissions.append({'project': project, 'path': path,
                                      'select': key[1], 'reason': 'INCOMPLETE_PROJECTION'})
                    continue
                for col in sorted(set(a['columns']) - set(b['columns'])):
                    refs = []
                    for (consumer, side), sources in snapshots.items():
                        if side != 'head' or consumer != project:
                            continue
                        for source_path, source in sources.items():
                            if Path(source_path).suffix.lower() not in EXTS - {'.xml'}:
                                continue
                            refs.extend({'project': consumer, 'sha': entries[consumer]['head'], **ref}
                                        for ref in scan_references(source, source_path, col))
                    if refs:
                        candidates.append({
                            'kind': 'QUERY_RESULT_SORT_CONTRACT_CANDIDATE',
                            'project': project, 'base_sha': ledger['base'],
                            'head_sha': ledger['head'], 'namespace': key[0],
                            'select_id': key[1], 'path': path, 'deleted_column': col,
                            'old': a, 'new': b, 'references': refs[:20],
                            'status': 'SAME_PROJECT_UNVERIFIED_BINDING',
                            'limitation': 'Missing SELECT output does not prove ORDER BY invalid; references are lexical candidates'})
                        if len(refs) > 20:
                            omissions.append({'project': project, 'path': path,
                                              'reason': 'REFERENCE_LIMIT', 'column': col})
    return {'schema_version': VERSION, 'candidates': candidates, 'omissions': omissions}
