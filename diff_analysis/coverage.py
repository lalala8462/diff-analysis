"""固定事实的覆盖投影；只读已有产物，不触发源码扫描或网络。"""
import json
from pathlib import Path

from .review_workflow import _hash, _write_json
from .scanner import SCANNER_RULES


VERSION = 'analysis-coverage-v1'
DEPENDENCY_ARTIFACTS = {
    'database': 'database_impact_latest.json',
    'query_contract': 'query_contract_latest.json',
    'external_influence': 'external_influence_latest.json',
    'shared_resources': 'shared_resource_latest.json',
    'instance_scope': 'instance_scope_latest.json',
    'behavior': 'behavior_impact_latest.json'}
STATUSES = {'COMPLETE', 'PARTIAL', 'FAILED', 'NOT_EXECUTED', 'UNKNOWN'}


def _read(root, name):
    path = root / name
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text('utf-8'))
        if not isinstance(value, dict):
            raise ValueError('覆盖产物必须为对象')
        return value
    except (ValueError, OSError):
        return {'status': 'FAILED', 'omissions': [{'reason': 'ARTIFACT_UNREADABLE', 'artifact': name}]}


def _diagnostics(items):
    rows = []
    for item in items or []:
        if not isinstance(item, dict):
            raise ValueError('诊断必须使用当前结构化对象格式')
        row = dict(item)
        if not isinstance(row.get('reason'), str):
            raise ValueError('诊断必须包含 reason')
        # 刷新次数和时间不是证据变化。
        for name in ('created_at_utc', 'updated_at_utc'):
            row.pop(name, None)
        rows.append(row)
    return [_row for _, _row in sorted({_hash(row): row for row in rows}.items())]


def _combine(statuses):
    statuses = list(statuses)
    if not statuses:
        return 'UNKNOWN'
    if all(s == 'COMPLETE' for s in statuses):
        return 'COMPLETE'
    if all(s == 'FAILED' for s in statuses):
        return 'FAILED'
    if all(s == 'UNKNOWN' for s in statuses):
        return 'UNKNOWN'
    if all(s == 'NOT_EXECUTED' for s in statuses):
        return 'NOT_EXECUTED'
    return 'PARTIAL'


def _stage(scopes):
    return {'status': _combine(s['status'] for s in scopes), 'scopes': scopes}


def _record(project, sha, side, status, diagnostics=(), rules=None):
    rows = _diagnostics(diagnostics)
    if status not in STATUSES:
        status = 'UNKNOWN'
    if rows and status == 'COMPLETE':
        status = 'PARTIAL'
    if status in ('UNKNOWN', 'FAILED', 'NOT_EXECUTED') or (status == 'PARTIAL' and not rows):
        rows.append({'reason': 'COVERAGE_' + status})
    rows = [{'project': project, 'sha': sha, 'side': side, **row} for row in rows]
    return {'project': project, 'sha': sha, 'side': side, 'status': status,
            'rules_version': rules, 'diagnostics': rows}


def _matches(gap, project, sha, side):
    owners = [gap[k] for k in ('project', 'producer_project', 'consumer_project', 'producer', 'consumer')
              if gap.get(k)]
    return (not owners or project in owners) and (not gap.get('sha') or gap['sha'] == sha) and (
        not gap.get('side') or str(gap['side']).upper() == side)


def refresh_coverage(run_dir, release=None, dependency_scope=None, report_status=None):
    root = Path(run_dir)
    release = release if release is not None else (_read(root, 'analysis.json') or {})
    ledgers = release.get('ledgers', [])
    stages = {}
    stages['git'] = _stage([_record(l['project'], l.get('head'), 'HEAD', 'COMPLETE', l.get('warnings', []))
                            for l in ledgers])
    failures = release.get('stage_failures', [])
    for failure in failures:
        if failure.get('stage') in ('GIT_LEDGER', 'PREPARE'):
            stages['git']['scopes'].append(_record(failure.get('project'), failure.get('head'), 'HEAD',
                                                   'FAILED', [failure]))
    stages['git'] = _stage(stages['git']['scopes'])
    java = []
    for ledger in ledgers:
        project = ledger['project']
        for side in ('BASE', 'HEAD'):
            info = release.get('java_coverage', {}).get(project, {}).get(side)
            failed = [f for f in failures if f.get('project') == project and f.get('stage') == 'STATIC_SCAN']
            status = (info.get('status', 'UNKNOWN') if info else
                      'NOT_EXECUTED' if failed and all(f.get('error_type') == 'NOT_RERUN' for f in failed) else
                      'FAILED' if failed else 'UNKNOWN')
            gaps = list(info.get('diagnostics') or []) if info else failed
            rules = info.get('rules_version') if info else None
            if info is not None and (rules != SCANNER_RULES or not isinstance(info.get('diagnostics'), list)):
                gaps = [{'reason': 'INVALID_JAVA_COVERAGE', 'detail': '扫描事实不符合当前规则和诊断格式'}]
                status = 'FAILED'
            java.append(_record(project, ledger[side.lower()], side, status, gaps, rules))
    stages['java'] = _stage(java)
    unified = _read(root, 'unified_graph_latest.json')
    impact = _read(root, 'unified_impact_latest.json')
    gaps = list((unified or {}).get('omissions', [])) + list((impact or {}).get('omissions', []))
    for graph in release.get('relations', {}).get('graphs', []):
        owner = {'project': graph['project'], 'sha': graph.get('snapshot_sha'), 'side': graph.get('side')}
        gaps.extend({**owner, **item} for item in graph.get('unresolved_calls', []))
        if graph.get('coverage', {}).get('truncated'):
            gaps.append({**owner, 'reason': 'RELATION_EDGE_LIMIT'})
        if graph.get('coverage', {}).get('graph_complete') is False:
            java_info = release.get('java_coverage', {}).get(graph['project'], {}).get(graph.get('side'), {})
            if java_info.get('diagnostics'):
                gaps.extend({**owner, **item} for item in java_info['diagnostics'])
            elif java_info.get('status') != 'COMPLETE' or graph.get('coverage', {}).get('truncated'):
                gaps.append({**owner, 'reason': 'RELATION_GRAPH_INCOMPLETE'})
    if (impact or {}).get('coverage', {}).get('truncated'):
        gaps = list(gaps) + [{'reason': 'UNIFIED_PROPAGATION_LIMIT'}]
    graph_failed = [f for f in failures if f.get('stage') == 'UNIFIED_GRAPH']
    graph_status = ('FAILED' if graph_failed or any((x or {}).get('status') == 'FAILED' for x in (unified, impact)) else
                    'COMPLETE' if 'omissions' in (unified or {}) and 'coverage' in (impact or {}) else 'UNKNOWN')
    stages['unified'] = _stage([_record(l['project'], l.get(side.lower()), side, graph_status,
        [g for g in _diagnostics(gaps + graph_failed) if _matches(g, l['project'], l.get(side.lower()), side)],
        (unified or {}).get('schema_version')) for l in ledgers for side in ('BASE', 'HEAD')])

    scope = dependency_scope if dependency_scope is not None else _read(root, 'dependency_scope.json')
    projects = (scope or {}).get('projects') or [
        {'project': l['project'], 'base': l.get('base'), 'head': l.get('head')} for l in ledgers]
    maven_gaps = _diagnostics(((scope or {}).get('gaps') or []) + ((scope or {}).get('omissions') or []))
    maven_status = ('FAILED' if (scope or {}).get('status') == 'FAILED' else
                    'UNKNOWN' if scope is None else
                    'COMPLETE' if isinstance(scope.get('gaps'), list) and scope.get('coverage_rules_version') == 'maven-sdk-declarations-v1' else 'FAILED')
    stages['maven_sdk'] = _stage([_record(p['project'], p.get(side.lower()), side, maven_status,
        [g for g in maven_gaps if _matches(g, p['project'], p.get(side.lower()), side)],
        (scope or {}).get('coverage_rules_version')) for p in projects for side in ('BASE', 'HEAD') if p.get(side.lower())])
    # 准备失败的登记项目没有进入源码范围，但缺口仍须计入阶段覆盖。
    scanned_projects = {p['project'] for p in projects}
    for gap in maven_gaps:
        if gap.get('project') and gap['project'] not in scanned_projects:
            stages['maven_sdk']['scopes'].append(_record(
                gap['project'], gap.get('sha'), gap.get('side', 'HEAD'), 'PARTIAL', [gap],
                (scope or {}).get('coverage_rules_version')))
    stages['maven_sdk'] = _stage(stages['maven_sdk']['scopes'])
    for name, filename in DEPENDENCY_ARTIFACTS.items():
        raw = dependency_scope.get(name) if dependency_scope is not None else _read(root, filename)
        declared = raw.get('status', 'COMPLETE') if raw is not None else 'UNKNOWN'
        gaps = _diagnostics((raw or {}).get('omissions', []))
        rules = (raw or {}).get('rules_version' if name == 'behavior' else 'schema_version')
        if raw is not None:
            valid = isinstance(raw.get('omissions'), list) and isinstance(raw.get('schema_version'), str)
            if name == 'behavior':
                from .behavior_analysis import RULES_VERSION
                valid = valid and rules == RULES_VERSION
            if not valid:
                gaps += [{'reason': 'INVALID_ANALYSIS_FORMAT', 'artifact': filename}]
                declared = 'FAILED'
        rows = []
        for p in projects:
            for side in ('BASE', 'HEAD'):
                sha = p.get(side.lower())
                if not sha or (name == 'instance_scope' and side == 'BASE'):
                    continue
                relevant = [g for g in gaps if _matches(g, p['project'], sha, side)]
                # 总体 PARTIAL 可由其他项目的局部缺口导致，不传播成无关项目失败。
                status = 'COMPLETE' if declared == 'PARTIAL' and gaps and not relevant else declared
                rows.append(_record(p['project'], sha, side, status, relevant, rules))
        stages[name] = _stage(rows)
    report = report_status or (release.get('report_generation') or {}).get('status', 'UNKNOWN')
    stages['report'] = {'status': report, 'required': False, 'scopes': []}
    batch = ('FAILED' if not ledgers else 'COMPLETE' if all(
        s['status'] == 'COMPLETE' for n, s in stages.items() if n != 'report') else 'PARTIAL')
    result = {'schema_version': VERSION, 'status': batch, 'stages': stages,
              'warning': '覆盖表示扫描范围，不表示业务正确或审核通过'}
    result['fingerprint'] = _hash([VERSION, stages])
    _write_json(root / 'coverage_latest.json', result)
    return result


def question_coverage(coverage, question, change_index):
    """覆盖变化按问题实际关联的项目、侧别和位置传播。"""
    locations = set()
    for cid in question.get('change_ids', []):
        item = change_index.get(cid)
        if not item:
            continue
        ledger, change = item
        for side, field in (('BASE', 'old_path'), ('HEAD', 'path')):
            locations.add((ledger['project'], ledger.get(side.lower()), side, change.get(field)))
    project = question.get('consumer_project')
    if project:
        sha = question.get('consumer_sha', question.get('sha'))
        uses = question.get('consumer_locations', []) or [{}]
        for use in uses:
            side = use.get('side', question.get('side'))
            locations.add((project, use.get('sha', sha), str(side).upper() if side else None, use.get('path')))
    values = []
    for name, stage in coverage['stages'].items():
        if name == 'report':
            continue
        for scope in stage['scopes']:
            targets = [loc for loc in locations if loc[0] == scope['project'] and
                       (not loc[1] or loc[1] == scope['sha']) and (not loc[2] or loc[2] == scope['side'])]
            if not targets:
                continue
            gaps = [g for g in scope['diagnostics'] if not g.get('path') or
                    any(not loc[3] or loc[3] == g['path'] or loc[3].startswith(g['path'].rstrip('/') + '/') for loc in targets)]
            status = scope['status']
            if scope['diagnostics'] and not gaps and status == 'PARTIAL':
                status = 'COMPLETE'
            # 单元身份由分组指纹处理；同一文件缺口不因分组编号变化而扩散。
            gaps = _diagnostics([{k: v for k, v in g.items() if k != 'unit_id'} for g in gaps])
            values.append([name, scope['project'], scope['sha'], scope['side'],
                           scope['rules_version'], status, gaps])
    return _hash([VERSION, values])
