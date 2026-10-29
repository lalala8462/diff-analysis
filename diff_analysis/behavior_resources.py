"""共享资源上的显式字段和值流；同名/前缀只作为候选关联。"""
import re
from .behavior_flow import Flow, key, LITERALS


def calls_in(value):
    if isinstance(value, dict):
        if value.get('kind') == 'METHOD_INVOCATION':
            yield value
        for child in value.values():
            yield from calls_in(child)
    elif isinstance(value, list):
        for child in value:
            yield from calls_in(child)


def _constant(node):
    if node and node.get('constant_known'):
        return node['constant']
    if node and node.get('kind') in LITERALS:
        return node.get('value')
    return None


def sql_facts(sql, path, line=1):
    """仅接受显式表列及简单取值，不将不完整投影视为完整结构。"""
    gaps, rows = [], []
    if re.search(r'\$\{|<(?:if|choose|foreach|include|trim|set|where)\b', sql, re.I):
        return [], [{'reason': 'DYNAMIC_SQL_VALUE_FLOW', 'path': path, 'line': line}]
    clean = re.sub(r'/\*[\s\S]*?\*/|--[^\n]*',
                   lambda m: re.sub(r'[^\n]', ' ', m.group()), sql)
    structural = re.sub(r"'(?:''|[^'])*'", "''", clean)
    if re.search(r'\b(?:JOIN|UNION)\b', structural, re.I) or len(re.findall(r'\bSELECT\b', structural, re.I)) > 1:
        return [], [{'reason': 'COMPLEX_SQL_VALUE_FLOW', 'path': path, 'line': line}]
    # 标识符规范化仅去除 SQL 引号，不猜数据源或 Schema。
    ident = r'[`"\w.]+'

    def norm(value):
        return value.replace('`', '').replace('"', '').lower()

    def value(raw):
        raw = raw.strip()
        if re.fullmatch(r"'(?:''|[^'])*'", raw):
            return True, raw[1:-1].replace("''", "'")
        if re.fullmatch(r'-?\d+', raw):
            return True, int(raw)
        if raw.upper() == 'NULL':
            return True, None
        match = re.fullmatch(r'#\{([\w.]+)(?:,[^}]*)?\}', raw)
        return False, match.group(1) if match else ''

    def row(table, column, role, raw=None, **extra):
        known, val = value(raw) if raw is not None else (False, None)
        position = clean.lower().find(column.lower())
        return {'kind': 'DATABASE', 'table': norm(table), 'field': norm(column), 'role': role,
                'known': known, 'value': val, 'path': path, 'line': line + clean[:max(0, position)].count('\n'), **extra}

    update = re.search(r'\bUPDATE\s+(' + ident + r')\s+SET\s+(.+?)(?:\bWHERE\b|$)', clean, re.I | re.S)
    insert = re.search(r'\bINSERT\s+INTO\s+(' + ident + r')\s*\(([^)]+)\)\s*VALUES\s*\(([^)]+)\)', clean, re.I | re.S)
    select = re.search(r'\bSELECT\s+(.+?)\s+FROM\s+(' + ident + r')\b(.*)', clean, re.I | re.S)
    if update:
        for assignment in re.split(r',\s*(?![^{}]*})', update.group(2)):
            pair = re.fullmatch(r'\s*(' + ident + r')\s*=\s*(.*?)\s*', assignment, re.S)
            if pair:
                rows.append(row(update.group(1), pair.group(1), 'WRITE', pair.group(2)))
    elif insert:
        columns = insert.group(2).split(',')
        values = re.split(r',\s*(?![^{}]*})', insert.group(3))
        if len(columns) == len(values):
            rows += [row(insert.group(1), c.strip(), 'WRITE', v) for c, v in zip(columns, values)]
    elif select:
        projection, table, tail = select.groups()
        for expression in projection.split(','):
            match = re.fullmatch(r'\s*(' + ident + r')(?:\s+(?:AS\s+)?(' + ident + r'))?\s*', expression, re.I)
            if match:
                rows.append(row(table, match.group(1).rsplit('.', 1)[-1], 'READ',
                                output=norm(match.group(2) or match.group(1).rsplit('.', 1)[-1])))
            else:
                gaps.append({'reason': 'SQL_PROJECTION_UNKNOWN', 'path': path, 'line': line})
        for match in re.finditer(r'(' + ident + r')\s+(?:IN\s*\(([^)]+)\)|=\s*(\x27[^\x27]*\x27|-?\d+))', tail, re.I):
            values = [value(v) for v in (match.group(2) or match.group(3)).split(',')]
            if all(known for known, _ in values):
                rows.append(row(table, match.group(1).rsplit('.', 1)[-1], 'DOMAIN',
                                accepted=[v for _, v in values], fallback=False))
    else:
        gaps.append({'reason': 'SQL_SHAPE_UNKNOWN', 'path': path, 'line': line})
    if not rows and not gaps:
        gaps.append({'reason': 'SQL_VALUE_MAPPING_UNKNOWN', 'path': path, 'line': line})
    return rows, gaps


def sql_inventory(snap):
    rows, gaps, mapper = [], [], {}
    for path, source in snap['files'].items():
        if path.endswith('.xml'):
            namespace = re.search(r'<mapper\b[^>]*\bnamespace=["\x27]([^"\x27]+)', source)
            result_maps = {}
            for mapping in re.finditer(r'<resultMap\b([^>]*)>(.*?)</resultMap>', source, re.I | re.S):
                mid = re.search(r'\bid=["\x27]([^"\x27]+)', mapping.group(1))
                properties = {}
                for field in re.finditer(r'<(?:id|result)\b([^>]*)/?>', mapping.group(2), re.I):
                    attrs = dict(re.findall(r'(column|property)=["\x27]([^"\x27]+)', field.group(1)))
                    if 'column' in attrs and 'property' in attrs:
                        properties[attrs['column'].lower()] = attrs['property']
                if mid:
                    result_maps[mid.group(1)] = properties
            for m in re.finditer(r'<(select|insert|update|delete)\b([^>]*)>(.*?)</\1\s*>', source, re.I | re.S):
                sid = re.search(r'\bid=["\x27]([^"\x27]+)', m.group(2))
                facts, missing = sql_facts(m.group(3), path, source[:m.start(3)].count('\n') + 1)
                for fact in facts:
                    fact['mapper'] = namespace.group(1) + '#' + sid.group(1) if namespace and sid else ''
                    result_map = re.search(r'\bresultMap=["\x27]([^"\x27]+)', m.group(2))
                    if result_map and fact['role'] == 'READ':
                        fact['output'] = result_maps.get(result_map.group(1), {}).get(fact['field'], fact.get('output', fact['field']))
                rows += facts
                gaps += missing
                if namespace and sid:
                    mapper[namespace.group(1) + '#' + sid.group(1)] = facts
        elif path.endswith('.sql'):
            offset = 0
            for statement in source.split(';'):
                if statement.strip():
                    facts, missing = sql_facts(statement, path, source[:offset].count('\n') + 1)
                    rows += facts
                    gaps += missing
                offset += len(statement) + 1
        elif path.endswith('.java'):
            # 注解 SQL 绑定到 AST 方法范围，普通字符串仅保留 SQL 字段证据。
            for method in snap['methods'].values():
                if method['path'] != path:
                    continue
                text = '\n'.join(source.splitlines()[method['start'] - 1:method['end']])
                for match in re.finditer(r'@(Select|Insert|Update|Delete)\s*\(\s*"((?:\\.|[^"\\])*)"\s*\)', text):
                    sql = match.group(2).replace('\\"', '"')
                    facts, missing = sql_facts(sql, path, method['start'])
                    for fact in facts:
                        fact['mapper'] = method['class'] + '#' + method['name']
                    rows += facts
                    gaps += missing
                    mapper[method['class'] + '#' + method['name']] = facts
                for call in calls_in(method.get('behavior', {})):
                    for arg in call.get('arguments', []):
                        if arg['kind'] == 'STRING_LITERAL' and re.match(r'\s*(SELECT|UPDATE|INSERT)\b', arg.get('value', ''), re.I):
                            owner = call.get('binding', '').partition('#')[0]
                            if owner not in ('java.sql.Statement', 'java.sql.PreparedStatement',
                                             'org.springframework.jdbc.core.JdbcTemplate', 'org.springframework.jdbc.core.JdbcOperations'):
                                gaps.append({'reason': 'SQL_LITERAL_WITHOUT_BOUND_SINK', 'path': path, 'line': call['line']})
                                continue
                            facts, missing = sql_facts(arg['value'], path, arg['line'])
                            for fact in facts:
                                fact['call_offset'] = call['offset']
                                fact['line'] = arg['line']
                            rows += facts
                            gaps += missing
    return rows, gaps, mapper


def resource_adapters(project, snap):
    from .shared_resources import extract_resources
    adapters, gaps = {}, []
    sql, sql_gaps, mapper = sql_inventory(snap)
    gaps += sql_gaps
    snap['sql_facts'] = sql
    snap['redis_facts'] = []
    for path, source in snap['files'].items():
        if not path.endswith('.java'):
            continue
        observations, _ = extract_resources(source, path)
        keys = [o for o in observations if o['kind'] == 'REDIS_KEY']
        for method in snap['methods'].values():
            if method['path'] != path:
                continue
            for call in calls_in(method.get('behavior', {})):
                name = key(call['select']).split('.')[-1]
                args = call['arguments']
                resource = _constant(args[0]) if args else None
                matched = [o for o in keys if o['start'] <= call['line'] <= o['end'] and o['resource'] == resource and o['exact']]
                owner = call.get('binding', '').partition('#')[0]
                mq_methods = {'org.springframework.amqp.rabbit.core.RabbitTemplate': {'convertAndSend', 'send'},
                              'org.springframework.kafka.core.KafkaTemplate': {'send'},
                              'org.springframework.jms.core.JmsTemplate': {'send', 'convertAndSend'},
                              'javax.jms.JMSProducer': {'send'}}
                if name in mq_methods.get(owner, set()):
                    adapters[(path, call['offset'])] = {'role': 'WRITE', 'resource': resource,
                        'resource_identity': ['MQ', resource], 'operation': 'MQ_SEND',
                        'symbol': method['symbol'], 'line': call['line'], 'path': path}
                if not matched and owner in ('org.springframework.data.redis.core.ValueOperations',
                                              'redis.clients.jedis.Jedis', 'redis.clients.jedis.UnifiedJedis'):
                    role = 'READ' if name == 'get' else 'WRITE' if name in ('set', 'setex') else None
                    if role and isinstance(resource, str):
                        matched = [{'role': role}]
                if matched and isinstance(resource, str):
                    observation = matched[0]
                    if observation['role'] not in ('READ', 'WRITE'):
                        continue
                    adapter = {'role': observation['role'], 'resource': resource,
                               'resource_identity': ['REDIS', resource], 'operation': 'REDIS_WRITE',
                               'value_argument': 2 if name.lower() == 'setex' else 1 if observation['role'] == 'WRITE' else None,
                               'symbol': method['symbol'], 'line': call['line'], 'path': path}
                    adapters[(path, call['offset'])] = adapter
                    snap['redis_facts'].append(adapter)
                    gaps.append({'reason': 'REDIS_SERIALIZATION_AND_INSTANCE_UNVERIFIED', 'path': path, 'line': call['line']})
                elif any(o['start'] <= call['line'] <= o['end'] for o in keys):
                    gaps.append({'reason': 'REDIS_KEY_NOT_EXACT', 'path': path, 'line': call['line']})
                binding = call.get('binding', '')
                facts = mapper.get(binding.partition('(')[0], [])
                if not facts:
                    facts = [f for f in sql if f.get('call_offset') == call['offset'] and f['path'] == path]
                if facts:
                    table = facts[0]['table']
                    writes = any(f['role'] == 'WRITE' for f in facts)
                    adapters[(path, call['offset'])] = {
                        'role': 'WRITE' if writes else 'READ', 'operation': 'DATABASE_WRITE',
                        'resource': table, 'resource_identity': ['DATABASE', table],
                        'symbol': method['symbol'], 'line': call['line'], 'path': path, 'sql_facts': facts}
    return adapters, gaps


def _located(change, fact, side):
    region = change.get('old_range' if side == 'base' else 'new_range') or {}
    path = change.get('old_path' if side == 'base' else 'path')
    return (path == fact['path'] and region.get('count', 0) > 0 and
            region['start'] <= fact.get('line', 0) < region['start'] + region['count'])


def _same_table(left, right):
    if '.' in left and '.' in right:
        return left == right
    return left.rsplit('.', 1)[-1] == right.rsplit('.', 1)[-1]


def analyze_persistence(entries, ledgers, snapshots, adapters, database, shared, semantics):
    rows, gaps = [], []
    links = []
    for item in database.get('impacts', []):
        links.append(('DATABASE', item['producer_project'], item['consumer_project'], item['table'], item))
    for item in shared.get('impacts', []):
        if item['kind'] == 'REDIS_KEY':
            links.append(('REDIS', item['producer_project'], item['consumer_project'], item['resource'], item))
    # 同项目资源也参与闭环检查，但不伪造跨项目资源绑定。
    for project, snap in [(p, s) for (p, side), s in snapshots.items() if side == 'head']:
        resources = {('DATABASE', f['table']) for f in snap.get('sql_facts', [])}
        resources |= {('REDIS', f['resource']) for f in snap.get('redis_facts', [])}
        for kind, resource in resources:
            links.append((kind, project, project, resource, {}))
    led_by_project = {l['project']: l for l in ledgers if l.get('role') != 'EVIDENCE'}
    flow_cache = {}

    def states(project, side):
        if (project, side) not in flow_cache:
            snap = snapshots.get((project, side))
            records = []
            if snap:
                flow = Flow(snap['methods'], semantics, adapters[project].get(side))
                for method in snap['methods'].values():
                    # 仅分析有资源访问的根方法，控制扫描成本。
                    if any(a['symbol'] == method['symbol'] for a in adapters[project].get(side, {}).values()):
                        flow.steps = 0
                        method_states = flow.run(method)
                        tests = {}
                        for state in method_states:
                            records += state.events
                            for event in state.events:
                                if event['kind'] == 'READ_TEST':
                                    tests.setdefault(tuple(event['resource']), {})[event['expression']] = event
                        for resource, comparisons in tests.items():
                            defaults = [s for s in method_states if all(
                                any(p.get('expression') == expression and p.get('truth') != test['equal'] for p in s.path)
                                for expression, test in comparisons.items())]
                            if defaults and all(s.stop == 'throw' or s.value.failure or (s.value.known and s.value.constant is None) for s in defaults):
                                first = next(iter(comparisons.values()))
                                records.append({**first, 'kind': 'READ_DOMAIN', 'resource': list(resource),
                                                'accepted': [v for t in comparisons.values() for v in t['accepted']], 'fallback': False})
                gaps.extend({'project': project, 'side': side, **g} for g in flow.gaps)
            flow_cache[(project, side)] = records
        return flow_cache[(project, side)]

    def facts(project, side, kind, resource):
        snap = snapshots.get((project, side), {})
        if kind == 'DATABASE':
            result = [f for f in snap.get('sql_facts', []) if _same_table(f['table'], resource)]
            for event in states(project, side):
                if event['kind'] == 'SQL_WRITE_VALUE' and _same_table(event['resource'], resource):
                    result.append({**event['declaration'], 'known': event['value']['known'],
                                   'value': event['value']['value'], 'path': event['path'],
                                   'line': event['line'], 'symbol': event['symbol']})
                elif event['kind'] == 'READ_DOMAIN' and event['resource'][:2] == [kind, resource] and len(event['resource']) == 3:
                    result.append({**event, 'role': 'DOMAIN', 'field': event['resource'][2]})
                elif event['kind'] == 'REQUIREMENT' and event.get('resource', [])[:2] == [kind, resource] and len(event['resource']) == 3:
                    result.append({**event, 'role': 'REQUIREMENT', 'field': event['resource'][2]})
            return result
        result = []
        for event in states(project, side):
            if event['kind'] == 'WRITE_VALUE' and event['resource'] == resource:
                fields = event['fields'] or {'$value': event['value']}
                for field, v in fields.items():
                    source_path, source_line = (v.get('sources') or [[event['path'], event['line']]])[0]
                    result.append({'kind': kind, 'role': 'WRITE', 'field': field,
                                   'known': v['known'], 'value': v['value'], 'path': source_path,
                                   'line': source_line, 'symbol': event['symbol']})
            elif event['kind'] == 'READ_DOMAIN' and event['resource'][:2] == [kind, resource]:
                result.append({**event, 'role': 'DOMAIN', 'field': event['resource'][2] if len(event['resource']) > 2 else '$value'})
            elif event['kind'] == 'REQUIREMENT' and event.get('resource', [])[:2] == [kind, resource] and len(event['resource']) == 3:
                result.append({**event, 'role': 'REQUIREMENT', 'field': event['resource'][2]})
        return result

    for kind, producer, consumer, resource, link in links:
        if producer not in led_by_project:
            continue
        ledger = led_by_project[producer]
        instance = link.get('instance_evidence', {}).get('status', '')
        if (instance in ('CONFIG_ENDPOINT_MISMATCH_UNCONFIRMED', 'PROFILE_MISMATCH_UNCONFIRMED') or
                link.get('status') in ('DATASOURCE_HINTS_DIFFER_UNCONFIRMED', 'SCHEMA_HINTS_DIFFER_UNCONFIRMED', 'REDIS_INSTANCE_HINTS_DIFFER_UNCONFIRMED')):
            gaps.append({'reason': 'RESOURCE_INSTANCE_MISMATCH', 'producer': producer, 'consumer': consumer, 'resource': resource})
            continue
        combinations = [('head', 'head', 'CURRENT_WRITE_CURRENT_READ'),
                        ('base', 'head', 'OLD_POSSIBLE_WRITE_NEW_READ')]
        if entries.get(consumer, {}).get('base'):
            combinations.append(('head', 'base', 'NEW_WRITE_OLD_READ'))
        for write_side, read_side, mode in combinations:
            if (producer, write_side) not in snapshots or (consumer, read_side) not in snapshots:
                continue
            writes = [f for f in facts(producer, write_side, kind, resource) if f['role'] == 'WRITE']
            reads = [f for f in facts(consumer, read_side, kind, resource) if f['role'] in ('DOMAIN', 'REQUIREMENT')]
            old_writes = facts(producer, 'base', kind, resource)
            for read in reads:
                values = [w for w in writes if w['field'] == read['field']]
                if (not values and writes and kind == 'REDIS' and mode == 'OLD_POSSIBLE_WRITE_NEW_READ'
                        and read['role'] == 'REQUIREMENT'):
                    values = [{**writes[0], 'field': read['field'], 'known': True, 'value': None, 'missing_field': True}]
                for write in values:
                    incompatible = ((read['role'] == 'DOMAIN' and write['known'] and write['value'] not in read['accepted'] and not read.get('fallback'))
                                    or (read['role'] == 'REQUIREMENT' and write['known'] and
                                        ((read['requirement'] == 'NONNULL' and write['value'] is None) or
                                         (read['requirement'] == 'NONZERO' and type(write['value']) is int and write['value'] == 0))))
                    if not incompatible:
                        continue
                    old_same = any(w['role'] == 'WRITE' and w['field'] == write['field'] and w.get('known') == write['known'] and w.get('value') == write['value'] for w in old_writes)
                    ids = [c['id'] for c in ledger['final_changes'] if _located(c, write, write_side)]
                    if mode == 'OLD_POSSIBLE_WRITE_NEW_READ':
                        prior_reads = facts(consumer, 'base', kind, resource)
                        if any(r['role'] == read['role'] and r['field'] == read['field'] and
                               ((r['role'] == 'REQUIREMENT' and r.get('requirement') == read.get('requirement')) or
                                (r['role'] == 'DOMAIN' and write['value'] not in r['accepted'] and not r.get('fallback')))
                               for r in prior_reads if r['role'] in ('DOMAIN', 'REQUIREMENT')):
                            continue
                        read_ledger = led_by_project.get(consumer, {})
                        ids = [c['id'] for c in read_ledger.get('final_changes', []) if _located(c, read, 'head')]
                        if ids:
                            change_ledger = read_ledger
                        else:
                            continue
                    else:
                        change_ledger = ledger
                        if old_same or not ids:
                            continue
                    rows.append({
                        'kind': 'PERSISTENCE_COMPATIBILITY', 'producer_project': change_ledger['project'],
                        'consumer_project': consumer, 'producer_base_sha': change_ledger.get('base'),
                        'producer_head_sha': change_ledger['head'], 'consumer_sha': entries[consumer][read_side],
                        'source_symbol': write.get('symbol', write.get('mapper', write['path'])),
                        'consumer_symbol': read.get('symbol', read.get('mapper', read['path'])),
                        'resource': resource + '#' + write['field'], 'resource_kind': kind,
                        'change_ids': ids, 'version_combination': mode,
                        'writer_project': producer, 'writer_sha': entries[producer][write_side],
                        'reader_project': consumer, 'reader_sha': entries[consumer][read_side],
                        'source_uses': [{'path': (read if mode == 'OLD_POSSIBLE_WRITE_NEW_READ' else write)['path'],
                                         'start': (read if mode == 'OLD_POSSIBLE_WRITE_NEW_READ' else write)['line'],
                                         'end': (read if mode == 'OLD_POSSIBLE_WRITE_NEW_READ' else write)['line'], 'side': 'head'}],
                        'consumer_uses': [{'path': read['path'], 'start': read['line'], 'end': read['line'], 'sha': entries[consumer][read_side]}],
                        'change_fact': '显式写入取值与读取约束存在不匹配',
                        'assumption': '读取端的已识别字段/取值要求能够处理写入形态',
                        'trigger': {'field': write['field'], 'written_value': write['value'], 'mode': mode},
                        'possible_impact': '查询排除新状态、读取分支缺失或旧版可能写入的空值被解引用',
                        'evidence': {'write': write, 'read': read, 'resource_link': link},
                        'evidence_level': 'RESOURCE_CANDIDATE',
                        'input_influence_level': 'MEDIUM', 'relation': 'SHARED_RESOURCE',
                        'unknown_reasons': ['资源及配置匹配不证明实际部署；旧版形态不证明历史数据存在']})
    return rows, gaps
