"""行为事实展示及精确审核归属；不修改固定版本事实。"""
import html
import json
from collections import defaultdict
from pathlib import Path

from .resource_review import _owners, _intersect
from .review_workflow import _state, _write_json, _question, _hash


LABELS = {'PRECONDITION': '前置条件与校验覆盖', 'RESULT_HANDLING': '返回结果与副作用假设',
          'PERSISTENCE_COMPATIBILITY': '读写闭环与历史兼容'}


def group_behavior_cards(candidates):
    groups = defaultdict(list)
    for row in candidates:
        groups[(row['producer_project'], row['consumer_project'],
                row['resource'] if row['kind'] == 'PERSISTENCE_COMPATIBILITY' else row['source_symbol'])].append(row)
    return [groups[k] for k in sorted(groups)]


def render_behavior(candidates):
    esc = lambda value: html.escape(str(value), quote=True)
    out = ['<details class="behavior"><summary>行为影响：%s（%d 项）</summary>' %
           (esc(candidates[0]['resource']), len(candidates))]
    for item in candidates:
        out.append('<details><summary>%s · %s</summary>' % (esc(LABELS[item['kind']]), esc(item['id'])))
        for label, field in (('变化事实', 'change_fact'), ('依赖假设', 'assumption'),
                             ('可能影响', 'possible_impact'), ('证据强度', 'evidence_level'),
                             ('输入影响程度', 'input_influence_level')):
            out.append('<p><b>%s：</b>%s</p>' % (label, esc(item[field])))
        out.append('<p>触发条件：%s</p>' % esc(json.dumps(item.get('trigger'), ensure_ascii=False)))
        out.append('<p>版本：%s → %s；消费者 %s @ %s；关系 %s</p>' % tuple(esc(v) for v in (
            item.get('producer_base_sha'), item['producer_head_sha'], item['consumer_project'], item['consumer_sha'], item['relation'])))
        out.append('<p>原始差异：' + ' '.join('<a href="#evidence-panel" data-evidence="%s">%s</a>' % (esc(cid), esc(cid)) for cid in item['change_ids']) + '</p>')
        for use in item['source_uses'] + item['consumer_uses']:
            out.append('<p>%s:%s · %s</p>' % (esc(use['path']), esc(use['start']), esc(use.get('sha', use.get('side', '')))))
        out.append('<pre style="white-space:pre-wrap;overflow-wrap:anywhere;max-height:28rem;overflow:auto">%s</pre>' %
                   esc(json.dumps(item['evidence'], ensure_ascii=False, indent=2)))
        if item.get('unknown_reasons'):
            out.append('<p>待确认：%s</p>' % esc('；'.join(item['unknown_reasons'])))
        out.append('</details>')
    return ''.join(out) + '</details>'


def refresh_behavior_review(run_dir, release, units=None):
    root = Path(run_dir)
    path = root / 'behavior_impact_latest.json'
    if not path.is_file():
        return {'available': False, 'status': 'UNKNOWN'}
    from .coverage import _read
    raw = _read(root, 'behavior_impact_latest.json')
    from .behavior_analysis import VERSION, RULES_VERSION
    if raw.get('schema_version') != VERSION or raw.get('rules_version') != RULES_VERSION:
        raise ValueError('行为产物必须使用当前规则和结构格式')
    state = _state(root)
    if units is None and state['group_revisions']:
        units = state['group_revisions'][-1]['units']
    owners = _owners(release, units)
    changes = {c['id']: c for l in release['ledgers'] for c in l['final_changes']}
    grouped, gaps = defaultdict(dict), []
    for candidate in raw.get('candidates', []):
        for cid in candidate['change_ids']:
            change = changes.get(cid)
            if not change:
                gaps.append({'reason': 'SOURCE_DIFF_NOT_IN_RELEASE', 'candidate_id': candidate['id'], 'diff_id': cid})
                continue
            for unit in owners.get(cid, []):
                gid = unit.get('primary_group')
                if not gid or unit.get('unassigned'):
                    continue
                matched = unit.get('range_basis') == 'WHOLE_RAW_CHANGE' or any(
                    use.get('side') == 'head' and use['path'] == change['path'] and
                    _intersect(unit.get('new_range'), use) for use in candidate['source_uses'])
                if not matched:
                    gaps.append({'reason': 'SOURCE_NOT_IN_EXACT_UNIT_RANGE', 'candidate_id': candidate['id'], 'unit_id': unit['id']})
                    continue
                grouped[gid].setdefault(candidate['id'], {'fact': candidate, 'change_ids': []})['change_ids'].append(cid)
    questions, groups = [], {}
    for gid, candidates in sorted(grouped.items()):
        groups[gid] = {'fingerprint': _hash([raw.get('rules_version'), candidates]),
                       'candidate_ids': sorted(candidates)}
        for bid, record in sorted(candidates.items()):
            candidate = record['fact']
            question = _question('BEHAVIOR_IMPACT', gid + ':' + bid,
                                 LABELS[candidate['kind']] + '：' + candidate['assumption'],
                                 [gid], record['change_ids'], ['DIFF', 'METHOD', 'BEHAVIOR_PATH', 'CONSUMER'],
                                 '核实触发路径、关系及目标版本；静态候选不等于已确认缺陷')
            question.update(behavior_candidate_id=bid, behavior_fingerprint=candidate.get('facts_fingerprint', _hash(candidate)),
                            consumer_project=candidate['consumer_project'], consumer_sha=candidate['consumer_sha'],
                            consumer_locations=candidate['consumer_uses'], trigger=candidate['trigger'])
            questions.append(question)
    result = {'schema_version': 'behavior-review-v1', 'status': raw.get('status', 'UNKNOWN'),
              'groups': groups, 'questions': questions, 'coverage_gaps': gaps,
              'facts_fingerprint': _hash(raw)}
    _write_json(root / 'behavior_review_latest.json', result)
    return {'available': True, 'groups': len(groups), 'questions': len(questions), 'coverage_gaps': len(gaps)}
