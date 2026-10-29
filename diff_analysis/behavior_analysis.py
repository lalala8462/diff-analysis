"""固定版本行为变化与直接消费者检查，不将未知关系当成已绑定链路。"""
import hashlib
import json
import tempfile
from pathlib import Path

from .behavior_flow import Flow, MAX_STATES, FLOW_RULES
from .gitops import git
from .scanner import scan_tree, SCANNER_RULES
from .source_index import source_snapshot_index


VERSION = 'behavior-impact-v1'
RULES_VERSION = FLOW_RULES + ':' + SCANNER_RULES
MAX_FILES = 4000
MAX_FILE_BYTES = 500000
MAX_BYTES = 20_000_000
MAX_CANDIDATES = 10000


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def semantics_from_registry(manifest):
    raw = json.loads(Path(manifest).read_text('utf-8-sig')).get('analysis_semantics', {}) if manifest else {}
    if not isinstance(raw, dict) or set(raw) - {'results', 'effects'}:
        raise ValueError('analysis_semantics 仅支持 results/effects 声明')
    result = {'results': [], 'effects': []}
    seen_results = set()
    for kind in result:
        rules = raw.get(kind, [])
        if not isinstance(rules, list):
            raise ValueError('analysis_semantics 规则必须为数组')
        for rule in rules:
            if not isinstance(rule, dict):
                raise ValueError('语义规则必须为对象')
            allowed = ({'method', 'type', 'member', 'failure_values', 'success_values'} if kind == 'results'
                       else {'method', 'operation', 'resource_argument'})
            if set(rule) - allowed:
                raise ValueError('语义规则含未知字段')
            if kind == 'results':
                if bool(rule.get('method')) == bool(rule.get('type')):
                    raise ValueError('结果规则必须选择 method 或 type')
                if rule.get('type') and ('.' not in rule['type'] or not rule.get('member')):
                    raise ValueError('类型规则须使用完整类型名及成员')
                for name in ('failure_values', 'success_values'):
                    values = rule.get(name, [])
                    if not isinstance(values, list) or any(type(v) not in (str, int, bool, type(None)) for v in values):
                        raise ValueError('结果值仅支持 JSON 标量数组')
                if not rule.get('failure_values') or set(map(digest, rule['failure_values'])) & set(map(digest, rule.get('success_values', []))):
                    raise ValueError('失败值不能为空或与成功值重叠')
                identity = (rule.get('method'), rule.get('type'), rule.get('member'))
                if identity in seen_results:
                    raise ValueError('同一结果契约不能重复声明')
                seen_results.add(identity)
            elif rule.get('operation') not in ('DATABASE_WRITE', 'REDIS_WRITE', 'MQ_SEND'):
                raise ValueError('未知副作用 operation')
            method = rule.get('method')
            if method and (not isinstance(method, str) or '#' not in method or not method.endswith(')') or '.' not in method.partition('#')[0]):
                raise ValueError('method 必须为完整类型#方法(完整参数类型)')
            if kind == 'effects':
                if not method or ('resource_argument' in rule and (type(rule['resource_argument']) is not int or rule['resource_argument'] < 0)):
                    raise ValueError('副作用规则须含方法签名及非负参数位置')
            result[kind].append(rule)
    return result


def snapshot(entry, project, side, cache_dir, review=None):
    sha = entry[side]
    index, _ = source_snapshot_index(entry['repo'], project, sha, Path(cache_dir) / 'indexes',
                                     extensions={'.java', '.xml', '.sql'}, rules_version=VERSION, max_files=MAX_FILES)
    gaps = list(index['omissions'])
    files, total = {}, 0
    for item in index['files']:
        raw = git(entry['repo'], 'cat-file', 'blob', item['blob_sha'])
        if len(raw) > MAX_FILE_BYTES or total + len(raw) > MAX_BYTES:
            gaps.append({'reason': 'SOURCE_BUDGET', 'path': item['path']})
            continue
        total += len(raw)
        files[item['path']] = raw.decode('utf-8', 'replace')
    if review is not None:
        field = 'old_methods' if side == 'base' else 'methods'
        coverage = review.get('java_coverage', {}).get(side.upper())
        supplied = review.get(field)
        if (review.get(side) != sha or not isinstance(supplied, dict) or
                not isinstance(coverage, dict) or coverage.get('rules_version') != SCANNER_RULES or
                not isinstance(coverage.get('diagnostics'), list) or
                coverage.get('status') not in ('COMPLETE', 'PARTIAL') or
                any(m.get('behavior', {}).get('version') != 'behavior-ast-v2' or
                    not isinstance(m.get('source_scope'), str) for m in supplied.values())):
            raise ValueError('方法事实必须包含当前规则的源码范围、行为 AST 和分侧诊断，且 SHA 一致')
        methods = {s: m for s, m in supplied.items() if m['path'] in files}
        gaps += coverage['diagnostics']
        if coverage['status'] != 'COMPLETE' and not coverage['diagnostics']:
            gaps.append({'reason': 'JAVA_COVERAGE_' + coverage['status']})
        if not methods and any(path.endswith('.java') for path in files):
            gaps.append({'reason': 'EMPTY_METHOD_FACTS', 'detail': '存在 Java 源码但没有可用方法事实'})
    else:
        from .scanner import java_toolchain
        from .review_workflow import _write_json
        javac, java, tools = java_toolchain()
        sources = Path(__file__).parent / 'java'
        tool_identity = [(str(p), Path(p).stat().st_mtime_ns if Path(p).is_file() else None)
                         for p in (javac, java, tools) if p]
        cache_key = digest([RULES_VERSION, sha, project, MAX_FILES, MAX_BYTES, MAX_FILE_BYTES, tool_identity,
                            [(name, digest((sources / name).read_text('utf-8'))) for name in
                             ('BehaviorFacts.java', 'JavaAstScanner.java')]])
        cache_file = Path(cache_dir) / 'facts' / (cache_key + '.json')
        cached = None
        if cache_file.is_file():
            try:
                record = json.loads(cache_file.read_text('utf-8'))
                if record.get('key') == cache_key and record.get('digest') == digest(record.get('payload')):
                    cached = record['payload']
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if cached is not None:
            return {'sha': sha, 'methods': cached['methods'], 'files': files,
                    'gaps': gaps + cached['gaps'], 'classpath': {'status': 'SOURCE_ONLY'}}
        # 临时目录由标准库管理，业务工作树不检出、不修改。
        with tempfile.TemporaryDirectory(prefix='behavior-') as folder:
            for path, text in files.items():
                if not (path.endswith('.java') or path == 'pom.xml' or path.endswith('/pom.xml')):
                    continue
                target = Path(folder) / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding='utf-8')
            methods, _, errors = scan_tree(folder, Path(cache_dir) / 'scanner')
            errors = [{'reason': 'JAVA_ATTRIBUTION_GAP', **e} for e in errors]
            gaps += errors
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {'methods': methods, 'gaps': errors}
            _write_json(cache_file, {'key': cache_key, 'payload': payload, 'digest': digest(payload)})
    return {'sha': sha, 'methods': methods, 'files': files, 'gaps': gaps,
            'classpath': (review or {}).get('java_classpath', {}).get(side.upper(), {'status': 'SOURCE_ONLY'})}


def touches(change, method, side):
    region = change.get('old_range' if side == 'base' else 'new_range') or {}
    start, count = region.get('start', 0), region.get('count', 0)
    return (change.get('old_path' if side == 'base' else 'path') == method['path']
            and count > 0 and start <= method['end'] and start + count - 1 >= method['start'])


def _issues(states):
    issues = []
    for state in states:
        for event in state.events:
            if event['kind'] == 'REQUIREMENT':
                issues.append({**event, 'family': 'PRECONDITION',
                               'assumption': {'NONNULL': '使用前必须非空', 'NONZERO': '整数除数必须非零',
                                              'ARRAY_BOUNDS': '下标必须在数组范围内'}[event['requirement']]})
            elif event['kind'] == 'RESULT_DOMAIN':
                issues.append({**event, 'family': 'RESULT_HANDLING'})
        if state.stop == 'throw' and state.value.call:
            call = next((e for e in state.events if e['kind'] == 'CALL' and e.get('call') == state.value.call), None)
            if call:
                issues.append({**call, 'family': 'RESULT_HANDLING', 'exception': state.value.dtype,
                               'condition': state.path, 'unknown': state.uncertain,
                               'assumption': '调用方需要处理新增或变化的显式异常'})
        failures = [e for e in state.events if e['kind'] == 'OUTCOME' and e.get('failure')]
        for outcome in failures:
            if state.stop == 'throw' or state.value.failure or state.value.call == outcome['call']:
                continue
            if state.value.known and outcome.get('known') and type(state.value.constant) is type(outcome.get('value')) and state.value.constant == outcome['value']:
                continue
            position = state.events.index(outcome)
            effects = [e for e in state.events[position + 1:] if e['kind'] == 'EFFECT']
            if not effects and outcome['call'] in state.checked_calls:
                continue
            issues.append({**outcome, 'family': 'RESULT_HANDLING', 'effects': effects,
                           'assumption': '失败结果需要处理或向上传递',
                           'condition': state.path, 'unknown': state.uncertain})
    return issues


def covered_by_prior(issue, previous):
    def identity(item):
        return [item['family'], item['symbol'], item.get('requirement'), item.get('expression'), item.get('call'),
                item.get('exception'), item.get('accepted'),
                [(e.get('operation'), e.get('resource'), e.get('binding')) for e in item.get('effects', [])]]

    def predicates(item):
        return {digest({k: v for k, v in p.items() if k != 'line'}) for p in item.get('condition', [])}

    return any(identity(old) == identity(issue) and predicates(old) <= predicates(issue)
               and (not old.get('unknown') or issue.get('unknown')) for old in previous)


def analyze_methods(project, entry, ledger, old, new, semantics, adapters=None):
    candidates, gaps = [], []
    old_methods, new_methods = old['methods'], new['methods']
    changes = ledger.get('final_changes', [])
    changed = {}
    for side, methods in (('base', old_methods), ('head', new_methods)):
        for symbol, method in methods.items():
            ids = [c['id'] for c in changes if touches(c, method, side)]
            if ids:
                changed.setdefault(symbol, set()).update(ids)
    bindings = {m.get('binding_id'): sym for sym, m in {**old_methods, **new_methods}.items() if sym in changed and m.get('binding_id')}
    from .behavior_resources import calls_in
    for symbol, method in new_methods.items():
        linked = {bindings[c['binding']] for c in calls_in(method.get('behavior', {})) if c.get('binding') in bindings}
        if symbol in changed:
            linked.add(symbol)
        if not linked:
            continue
        new_flow = Flow(new_methods, semantics, (adapters or {}).get('head'))
        new_states = new_flow.run(method)
        previous = old_methods.get(symbol)
        old_flow = Flow(old_methods, semantics, (adapters or {}).get('base'))
        old_states = old_flow.run(previous) if previous else []
        old_issues = _issues(old_states)
        gaps += new_flow.gaps + old_flow.gaps
        for issue in _issues(new_states):
            if covered_by_prior(issue, old_issues):
                continue
            source = next((s for s in sorted(linked) if s == issue['symbol']), None)
            if source is None:
                source = next((s for s in sorted(linked) if (new_methods.get(s) or old_methods[s]).get('binding_id') in
                               [issue.get('call')] + issue.get('related_calls', [])), None)
            if source is None and symbol in changed:
                source = symbol
            if source is None:
                continue
            producer = new_methods.get(source) or old_methods[source]
            ids = sorted(changed[source])
            family = issue['family']
            if any(c in bindings for c in [issue.get('call')] + issue.get('related_calls', [])) and issue['symbol'] == symbol and source != symbol:
                family = 'RESULT_HANDLING'
            candidates.append({
                'kind': family, 'producer_project': project, 'consumer_project': project,
                'producer_base_sha': entry.get('base'), 'producer_head_sha': entry['head'],
                'consumer_sha': entry['head'], 'source_symbol': source, 'consumer_symbol': symbol,
                'resource': producer['class'] + '#' + producer['name'], 'change_ids': ids,
                'source_uses': [{'path': producer['path'], 'start': max(producer['start'], (c.get('new_range') or {}).get('start', producer['start'])),
                                 'end': min(producer['end'], (c.get('new_range') or {}).get('start', producer['end']) + max(1, (c.get('new_range') or {}).get('count', 1)) - 1),
                                 'side': 'head'} for c in changes if c['id'] in ids and touches(c, producer, 'head')],
                'consumer_uses': [{'path': issue['path'], 'start': issue['line'], 'end': issue['line'], 'sha': entry['head']}],
                'change_fact': '新路径出现未被支持的校验排除的前置要求' if family == 'PRECONDITION' else '返回结果变化影响调用方的后续路径',
                'assumption': issue['assumption'], 'trigger': issue.get('condition', []),
                'possible_impact': ('输入可能不满足 ' + issue.get('requirement', '') if family == 'PRECONDITION' else
                                    '新增可空结果被使用，或失败结果未处理后继续执行'),
                'evidence': issue, 'evidence_level': 'PARTIAL' if issue['unknown'] else 'BOUND_STATIC_PATH',
                'input_influence_level': 'MEDIUM', 'relation': 'SAME_METHOD' if source == symbol else 'DIRECT_JAVAC_CALL',
                'unknown_reasons': ['路径存在不支持的结构'] if issue['unknown'] else []})
    return candidates, gaps


def analyze_bound_consumers(entries, ledgers, snapshots, semantics, adapters):
    """只传播唯一的完整 javac 符号，兼容对照不冒充运行链路。"""
    from .behavior_resources import calls_in
    rows, gaps = [], []
    for ledger in ledgers:
        producer = ledger['project']
        if ledger.get('role') == 'EVIDENCE':
            continue
        old = snapshots.get((producer, 'base'), {}).get('methods', {})
        new = snapshots.get((producer, 'head'), {}).get('methods', {})
        for symbol, target in new.items():
            binding = target.get('binding_id')
            changes = [c for c in ledger['final_changes'] if touches(c, target, 'head')]
            if not binding or not changes or not target.get('behavior', {}).get('body'):
                continue
            owners = {p for (p, side), snap in snapshots.items() if side == 'head' and
                      any(m.get('binding_id') == binding for m in snap['methods'].values())}
            if owners != {producer}:
                gaps.append({'project': producer, 'symbol': symbol, 'reason': 'CROSS_PROJECT_SYMBOL_AMBIGUOUS'})
                continue
            for (consumer, side), snapshot in snapshots.items():
                if side != 'head' or consumer == producer:
                    continue
                consumer_old = snapshots.get((consumer, 'base'), snapshot)
                for caller_symbol, caller in snapshot['methods'].items():
                    if not any(c.get('binding') == binding for c in calls_in(caller.get('behavior', {}))):
                        continue
                    new_flow = Flow({**new, **snapshot['methods']}, semantics, adapters[consumer].get('head'))
                    new_states = new_flow.run(caller)
                    old_flow = Flow({**old, **consumer_old['methods']}, semantics, adapters[consumer].get('base', adapters[consumer].get('head')))
                    previous = consumer_old['methods'].get(caller_symbol)
                    old_states = old_flow.run(previous) if previous else []
                    gaps += [{'project': consumer, **g} for g in old_flow.gaps + new_flow.gaps]
                    for issue in _issues(new_states):
                        if (issue['symbol'] != symbol and binding not in [issue.get('call')] + issue.get('related_calls', [])) or covered_by_prior(issue, _issues(old_states)):
                            continue
                        family = 'RESULT_HANDLING' if issue['symbol'] != symbol else issue['family']
                        rows.append({
                            'kind': family, 'producer_project': producer, 'consumer_project': consumer,
                            'producer_base_sha': entries[producer].get('base'), 'producer_head_sha': entries[producer]['head'],
                            'consumer_sha': snapshot['sha'], 'source_symbol': symbol, 'consumer_symbol': caller_symbol,
                            'resource': target['class'] + '#' + target['name'], 'change_ids': sorted(c['id'] for c in changes),
                            'source_uses': [{'path': target['path'], 'start': max(target['start'], c['new_range']['start']),
                                             'end': min(target['end'], c['new_range']['start'] + c['new_range']['count'] - 1), 'side': 'head'} for c in changes],
                            'consumer_uses': [{'path': caller['path'], 'start': next(c['line'] for c in calls_in(caller['behavior']) if c.get('binding') == binding),
                                               'end': next(c['line'] for c in calls_in(caller['behavior']) if c.get('binding') == binding),
                                               'sha': snapshot['sha'], 'project': consumer}],
                            'change_fact': '完整符号绑定的跨项目直接消费者出现新增路径要求',
                            'assumption': issue['assumption'], 'trigger': issue['condition'],
                            'possible_impact': '实参不满足新增要求，或调用方不能处理新增结果',
                            'evidence': issue, 'evidence_level': 'PARTIAL' if issue['unknown'] else 'BOUND_STATIC_PATH',
                            'input_influence_level': 'MEDIUM', 'relation': 'DIRECT_CROSS_PROJECT_JAVAC_SYMBOL',
                            'unknown_reasons': ['源码归属不证明实际 JAR 或生产部署；单版本消费者不证明升级过程']})
    return rows, gaps


def analyze_behavior(entries, ledgers, cache_dir, manifest=None, reviews=None, database=None, shared=None):
    from .behavior_resources import resource_adapters, analyze_persistence
    result = {'schema_version': VERSION, 'rules_version': RULES_VERSION,
              'status': 'COMPLETE', 'candidates': [], 'omissions': []}
    result['limits'] = {'states': MAX_STATES, 'files': MAX_FILES, 'single_file_bytes': MAX_FILE_BYTES,
                        'total_bytes': MAX_BYTES, 'candidates': MAX_CANDIDATES, 'flow_steps': 20000}
    try:
        semantics = semantics_from_registry(manifest)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result.update(status='FAILED', omissions=[{'reason': 'SEMANTICS_INVALID', 'detail': str(exc)[:300]}])
        return result
    result['analysis_fingerprint'] = digest([RULES_VERSION, semantics, result['limits'],
                                             {p: [e.get('base'), e.get('head')] for p, e in entries.items()}])
    by_review = {r['project']: r for r in reviews or []}
    snapshots = {}
    adapters = {}
    for project, entry in sorted(entries.items()):
        adapters[project] = {}
        for side in ('base', 'head'):
            if not entry.get(side):
                continue
            try:
                snap = snapshot(entry, project, side, cache_dir, by_review.get(project))
                snapshots[(project, side)] = snap
                adapter, gaps = resource_adapters(project, snap)
                adapters[project][side] = adapter
                result['omissions'] += [{'project': project, 'side': side, **g} for g in snap['gaps'] + gaps]
            except (OSError, ValueError, RuntimeError) as exc:
                result['omissions'].append({'project': project, 'side': side, 'reason': 'BEHAVIOR_SCAN_FAILED', 'detail': str(exc)[:300]})
    from .behavior_resources import calls_in
    all_bindings = {m.get('binding_id') for s in snapshots.values() for m in s['methods'].values()}
    all_bindings |= {c.get('binding') for s in snapshots.values() for m in s['methods'].values() for c in calls_in(m.get('behavior', {}))}
    all_types = {m['class'] for s in snapshots.values() for m in s['methods'].values()}
    for rules in semantics.values():
        for rule in rules:
            if (rule.get('method') and rule['method'] not in all_bindings) or (rule.get('type') and rule['type'] not in all_types):
                result['omissions'].append({'reason': 'SEMANTICS_BINDING_UNAVAILABLE', 'rule': rule})
    for ledger in ledgers:
        project = ledger['project']
        if ledger.get('role') == 'EVIDENCE':
            continue
        old, new = snapshots.get((project, 'base')), snapshots.get((project, 'head'))
        if not old or not new:
            result['omissions'].append({'project': project, 'reason': 'BEHAVIOR_COMPARISON_UNAVAILABLE'})
            continue
        rows, gaps = analyze_methods(project, entries[project], ledger, old, new, semantics, adapters[project])
        result['candidates'] += rows
        result['omissions'] += [{'project': project, **g} for g in gaps]
    cross, gaps = analyze_bound_consumers(entries, ledgers, snapshots, semantics, adapters)
    result['candidates'] += cross
    result['omissions'] += gaps
    persistence, gaps = analyze_persistence(entries, ledgers, snapshots, adapters, database or {}, shared or {}, semantics)
    result['candidates'] += persistence
    result['omissions'] += gaps
    unique = {}
    for row in result['candidates']:
        row['status'] = 'NEEDS_REVIEW'
        row['id'] = 'B-' + digest([VERSION, row['kind'], row['producer_project'], row['consumer_project'],
                                   row['producer_base_sha'], row['producer_head_sha'], row['consumer_sha'],
                                   row['source_symbol'], row.get('consumer_symbol'), row['resource'], row['evidence']])[:20]
        unique[row['id']] = row
    result['candidates'] = [unique[k] for k in sorted(unique)][:MAX_CANDIDATES]
    if len(unique) > MAX_CANDIDATES:
        result['omissions'].append({'reason': 'BEHAVIOR_CANDIDATE_BUDGET'})
    result['omissions'] = list({digest(g): g for g in result['omissions']}.values())
    if result['omissions']:
        result['status'] = 'PARTIAL'
    result['classpath'] = {p + ':' + side: s['classpath'] for (p, side), s in snapshots.items()}
    result['analysis_fingerprint'] = digest([result['analysis_fingerprint'], result['classpath']])
    scope_fingerprints = {}
    for row in result['candidates']:
        projects = tuple(sorted({row['producer_project'], row['consumer_project']}))
        if projects not in scope_fingerprints:
            related = [s for (p, _), s in snapshots.items() if p in projects]
            bindings = {m.get('binding_id') for s in related for m in s['methods'].values()}
            bindings |= {c.get('binding') for s in related for m in s['methods'].values()
                         for c in calls_in(m.get('behavior', {}))}
            types = {m['class'] for s in related for m in s['methods'].values()}
            rules = {kind: [r for r in items if (r.get('method') and r['method'] in bindings) or
                           (r.get('type') and r['type'] in types)] for kind, items in semantics.items()}
            # 批次其他项目的版本和 classpath 不影响当前候选的事实身份。
            inputs = {p: [entries[p].get('base'), entries[p].get('head'),
                           {side: result['classpath'].get(p + ':' + side) for side in ('base', 'head')}]
                      for p in projects if p in entries}
            scope_fingerprints[projects] = digest([RULES_VERSION, rules, result['limits'], inputs])
        row['facts_fingerprint'] = digest([row, scope_fingerprints[projects]])
    return result
