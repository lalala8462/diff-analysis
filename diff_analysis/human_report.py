"""离线人工审核页面；仅渲染固定 Git 对象和静态候选。"""
import difflib
import html
import json
import re
from collections import defaultdict

from .release_analysis import _show_lines
from .maven_inventory import analyze
from .gitops import git


JAVA_TOKEN = re.compile(r'''\s+|//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|(?:0[bB][01_]+[lL]?|0[xX][0-9a-fA-F_]+(?:\.[0-9a-fA-F_]*)?(?:[pP][+-]?[\d_]+)?[fFdDlL]?|(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?[\d_]+)?[fFdDlL]?)|(?:[^\W\d]|[$_])[\w$]*|(?:>>>=|>>=|<<=|>>>|>>|<<|==|!=|<=|>=|&&|\|\||\+\+|--|\+=|-=|\*=|/=|%=|&=|\|=|\^=|->|::|\.\.\.)|\S''')
JAVA_IMPORT = re.compile(r'^[ \t]*import[ \t]+(?:static[ \t]+)?[\w$]+(?:\.[\w$]+)*(?:\.\*)?[ \t]*;[ \t]*$')


def _java_line_tokens(lines):
    """保留字面量和注释；不能可靠词法化时不忽略任何差异。"""
    source = '\n'.join(lines)
    if re.search(r'\\u+[0-9a-fA-F]{4}', source) or '"""' in source:
        return None
    result = [[] for _ in lines]
    line = 0
    imports = set()
    tokens = []
    for match in JAVA_TOKEN.finditer(source):
        token = match.group()
        start = line
        line += token.count('\n')
        if token.isspace():
            continue
        if token in ('"', "'") or token == '/*':
            return None
        if token == '/' and source[match.start():].startswith('/*'):
            return None
        end = line
        tokens.append((start, end, token))
        if start < len(result):
            result[start].append(token)
        if end > start:
            for i in range(start + 1, min(end + 1, len(result))):
                result[i].append(None)
    for i, text in enumerate(lines):
        if JAVA_IMPORT.fullmatch(text) and result[i] and result[i][0] == 'import' and None not in result[i]:
            imports.add(i)
    return result, imports


def _noise_changes(ledgers, repos):
    ignored = {}
    for ledger in ledgers:
        repo = repos.get(ledger['project'])
        if repo is None:
            continue
        for file in ledger['final_files']:
            if not file['path'].endswith('.java') or file['status'][:1] not in ('M', 'R'):
                continue
            old, olderr = _show_lines(repo, ledger['base'], file.get('old_path', file['path']))
            new, newerr = _show_lines(repo, ledger['head'], file['path'])
            if olderr or newerr:
                continue
            a, b = _java_line_tokens(old), _java_line_tokens(new)
            if a is None or b is None:
                continue
            for change in ledger['final_changes']:
                if change['path'] != file['path']:
                    continue
                selected = []
                has_import = False
                safe = True
                for (tokens, imports), part in ((a, change['old_range']), (b, change['new_range'])):
                    start, count = part['start'], part['count']
                    if not count:
                        selected.append([])
                        continue
                    if start < 1 or start - 1 + count > len(tokens):
                        safe = False
                        break
                    fragment = []
                    for i in range(start - 1, start - 1 + count):
                        if None in tokens[i]:
                            safe = False
                            break
                        if i in imports:
                            has_import = True
                        else:
                            fragment.extend(tokens[i])
                    selected.append(fragment)
                if safe and len(selected) == 2 and selected[0] == selected[1] and (change['old_range']['count'] or change['new_range']['count']):
                    ignored[change['id']] = '仅 import 或空白格式' if has_import else '仅空白格式'
    return ignored


def esc(value):
    return html.escape(str(value if value is not None else '未知'), quote=True)


def attribution(change):
    manual = change.get('manual_attribution')
    if manual:
        return manual.get('issue_keys', []), '人工归属'
    if change.get('line_commit_candidates'):
        return change.get('line_issue_candidates', []), '变更行提交候选'
    return change.get('issue_candidates', []), '文件级提交候选'


def _inline(left, right):
    a, b = [], []
    for op, i, j, k, l in difflib.SequenceMatcher(None, left, right, autojunk=False).get_opcodes():
        x, y = esc(left[i:j]), esc(right[k:l])
        a.append(x if op == 'equal' else '<mark>' + x + '</mark>')
        b.append(y if op == 'equal' else '<mark>' + y + '</mark>')
    return ''.join(a), ''.join(b)


def _diff_rows(old, new):
    rows = []
    for op, a, b, c, d in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if op == 'equal':
            for i in range(b - a):
                rows.append((a+i+1, esc(old[a+i]), c+i+1, esc(new[c+i]), 'same'))
        else:
            for i in range(max(b-a, d-c)):
                left = old[a+i] if a+i < b else None
                right = new[c+i] if c+i < d else None
                x, y = _inline(left, right) if left is not None and right is not None else (esc(left) if left is not None else '', esc(right) if right is not None else '')
                rows.append((a+i+1 if left is not None else '', x, c+i+1 if right is not None else '', y, 'change'))
    changed = [i for i, row in enumerate(rows) if row[4] == 'change']
    return rows, changed


def _evidence_panel(change):
    old = change.get('before', [])
    new = change.get('after', [])
    rows, _ = _diff_rows([x['text'] for x in old], [x['text'] for x in new])
    keys, basis = attribution(change)
    parts = ['<template id="evidence-%s"><h2>%s</h2>' % (esc(change['id']), esc(change['id'])),
             '<p><b>项目：</b>%s<br><b>文件：</b>%s<br><b>工单：</b>%s（%s）</p>' %
             (esc(change['project']), esc(change['path']), esc('、'.join(keys) or 'EMPTY / 未归属'), esc(basis)),
             '<p><b>旧版：</b>%s<br><b>新版：</b>%s</p>' % (esc(change['base_sha']), esc(change['head_sha']))]
    if rows:
        parts.append('<p>以下为差异附近的代码窗口；滚动查看两侧。</p><div class="diffscroll"><table class="diff"><colgroup><col class="num-col"><col class="code-col"><col class="num-col"><col class="code-col"></colgroup><thead><tr><th colspan="2">旧版</th><th colspan="2">新版</th></tr></thead><tbody>')
        for ln, left, rn, right, kind in rows:
            oldnum = old[ln - 1]['line'] if isinstance(ln, int) else ''
            newnum = new[rn - 1]['line'] if isinstance(rn, int) else ''
            oldrange, newrange = change.get('old_range', {}), change.get('new_range', {})
            def in_range(number, value):
                return isinstance(number, int) and value.get('start', 0) <= number < value.get('start', 0) + value.get('count', 0)
            if kind == 'change' and not (in_range(oldnum, oldrange) or in_range(newnum, newrange)):
                kind = 'same'
                left = esc(old[ln - 1]['text']) if isinstance(ln, int) else ''
                right = esc(new[rn - 1]['text']) if isinstance(rn, int) else ''
            parts.append('<tr class="%s"><td class="num">%s</td><td class="code old">%s</td><td class="num">%s</td><td class="code new">%s</td></tr>' % (kind, oldnum, left, newnum, right))
        parts.append('</tbody></table></div>')
    else:
        parts.append('<p class="gap">没有可展示的文本代码窗口，请查看文件状态和原始证据。</p>')
    status = change.get('context_status', {})
    if status.get('old') or status.get('new'):
        parts.append('<p class="gap">代码窗口状态：旧版 %s；新版 %s。</p>' % (esc(status.get('old') or '可读取'), esc(status.get('new') or '可读取')))
    parts.append('<p><a href="evidence/%s.md" target="_blank" rel="noopener">打开原始证据文件</a></p></template>' % esc(change['id']))
    return ''.join(parts)


def _evidence_link(change_id):
    return '<a href="evidence/%s.md" data-evidence="%s">%s</a>' % (esc(change_id), esc(change_id), esc(change_id))


def _consumer_panel(panel_id, alert):
    context = alert['consumer_context']
    old, new = context['base']['lines'], context['head']['lines']
    rows, _ = _diff_rows([x['text'] for x in old], [x['text'] for x in new])
    parts = ['<template id="evidence-%s"><h2>消费者依赖声明</h2>' % esc(panel_id),
             '<p><b>项目：</b>%s<br><b>文件：</b>%s<br><b>SDK：</b>%s:%s</p>' %
             (esc(alert['consumer']), esc(alert['path']), esc(alert['sdk']['group']), esc(alert['sdk']['artifact'])),
             '<p><b>消费者比较旧版依赖：</b>%s（%s）<br><b>消费者比较新版依赖：</b>%s（%s）<br><b>SDK 项目本次比较旧版：</b>%s（%s）<br><b>SDK 项目本次比较新版：</b>%s（%s）</p>' %
             (esc(alert['old_version']), esc((alert.get('consumer_base_sha') or '未提供')[:12]), esc(alert['new_version']), esc((alert.get('consumer_head_sha') or '未知')[:12]), esc(alert['sdk']['old_version']), esc((alert['sdk'].get('base_sha') or '未知')[:12]), esc(alert['sdk']['new_version']), esc((alert['sdk'].get('head_sha') or '未知')[:12])),
             '<p>SDK 版本取自本次指定的 Git 修订号，不代表生产或最新发布版本。</p>',
             '<p>以下为固定版本 POM 的依赖声明窗口；属性或继承解析结果见上方版本。</p>']
    if rows:
        parts.append('<div class="diffscroll"><table class="diff"><colgroup><col class="num-col"><col class="code-col"><col class="num-col"><col class="code-col"></colgroup><thead><tr><th colspan="2">消费者旧版</th><th colspan="2">消费者新版</th></tr></thead><tbody>')
        for ln, left, rn, right, kind in rows:
            oldnum = old[ln - 1]['line'] if isinstance(ln, int) else ''
            newnum = new[rn - 1]['line'] if isinstance(rn, int) else ''
            parts.append('<tr class="%s"><td class="num">%s</td><td class="code old">%s</td><td class="num">%s</td><td class="code new">%s</td></tr>' % (kind, oldnum, left, newnum, right))
        parts.append('</tbody></table></div>')
    parts.append('<p class="gap">旧版：%s；新版：%s。</p></template>' % (esc(context['base']['status']), esc(context['head']['status'])))
    return ''.join(parts)


def _group_sdk_alerts(alerts):
    """同一 SDK 与消费者项目合并展示，保留声明和各引用位置。"""
    groups, result = {}, []
    for alert in alerts:
        sdk = alert.get('sdk')
        consumer = alert.get('consumer')
        if not sdk or not consumer:
            result.append(alert)
            continue
        key = (sdk.get('project'), sdk.get('module'), sdk.get('group'), sdk.get('artifact'),
               sdk.get('base_sha'), sdk.get('head_sha'), consumer)
        if key not in groups:
            groups[key] = {'kind': 'SDK 与消费者项目', 'sdk': sdk, 'consumer': consumer,
                           'declarations': [], 'references': []}
            result.append(groups[key])
        target = groups[key]['declarations'] if alert.get('consumer_context') else groups[key]['references']
        target.append(alert)
    return result


def _file_evolution(repo, ledger, path, changes):
    """列出比较范围内的文件提交及其第一父提交差异。"""
    history = ledger.get('history', {})
    if repo is None or history.get('status') != 'COMPLETE':
        return {'status': 'UNKNOWN', 'events': [], 'linear': False}
    candidates = {sha for c in changes for sha in c.get('commit_candidates', [])}
    candidates.update(sha for c in changes for sha in c.get('line_commit_candidates', []))
    final_lines = {sha for c in changes for sha in c.get('line_commit_candidates', [])}
    paths = list(dict.fromkeys([c.get('old_path', path) for c in changes] + [path]))
    events, linear = [], True
    for commit in history.get('commits', []):
        sha = commit['sha']
        if sha not in candidates:
            continue
        if events:
            try:
                git(repo, 'merge-base', '--is-ancestor', events[-1]['sha'], sha)
            except (OSError, RuntimeError):
                linear = False
        try:
            patch = git(repo, 'show', '--first-parent', '--no-color', '--format=',
                        '--find-renames', '--unified=3', sha, '--', *paths).decode('utf-8', 'replace')
            if 'new file mode ' in patch:
                operation = '新增文件'
            elif 'deleted file mode ' in patch:
                operation = '删除文件'
            elif 'rename from ' in patch:
                operation = '重命名或修改文件'
            elif patch:
                operation = '修改文件'
            else:
                operation = '操作未确认'
            truncated = len(patch) > 20000
            events.append({'sha': sha, 'subject': commit['subject'], 'operation': operation,
                           'patch': patch[:20000], 'truncated': truncated,
                           'final_lines': sha in final_lines, 'merge': len(commit.get('parents', [])) > 1})
        except (OSError, RuntimeError):
            events.append({'sha': sha, 'subject': commit['subject'], 'operation': '差异读取失败',
                           'patch': '', 'truncated': False, 'final_lines': sha in final_lines,
                           'merge': len(commit.get('parents', [])) > 1})
    return {'status': 'COMPLETE' if events and all(e['operation'] not in ('操作未确认', '差异读取失败') for e in events) else 'PARTIAL',
            'events': events, 'linear': linear}


def _file_view(repo, ledger, path, changes, review, ignored_changes=()):
    info = next((f for f in ledger['final_files'] if f['path'] == path), {})
    oldpath = info.get('old_path', path)
    if repo is None:
        return '<p class="gap">从底账恢复：未读取完整 Git 文件，请查看原有证据窗口。</p>'
    status = info.get('status', '')
    old, olderr = (None, 'ABSENT') if status.startswith('A') else _show_lines(repo, ledger['base'], oldpath)
    new, newerr = (None, 'ABSENT') if status.startswith('D') else _show_lines(repo, ledger['head'], path)
    if olderr == 'ABSENT': old = []
    if newerr == 'ABSENT': new = []
    if old is None or new is None:
        return '<p class="gap">无法展示完整代码：旧版 %s；新版 %s。请查看底账。</p>' % (esc(olderr or '可读取'), esc(newerr or '可读取'))
    rows, changed = _diff_rows(old, new)
    def in_change(number, changes, side):
        if not isinstance(number, int):
            return False
        for change in changes:
            part = change[side + '_range']
            if part['start'] <= number < part['start'] + part['count']:
                return True
        return False
    filtered = []
    for ln, left, rn, right, kind in rows:
        is_ignored = in_change(ln, ignored_changes, 'old') or in_change(rn, ignored_changes, 'new')
        is_visible = in_change(ln, changes, 'old') or in_change(rn, changes, 'new')
        if kind == 'change' and is_ignored and not is_visible:
            kind = 'same'
            left = esc(old[ln - 1]) if isinstance(ln, int) else ''
            right = esc(new[rn - 1]) if isinstance(rn, int) else ''
        filtered.append((ln, left, rn, right, kind))
    rows = filtered
    changed = [i for i, row in enumerate(rows) if row[4] == 'change']
    if not changed:
        return '<p>文件事件：%s；无可展示的文本差异。</p>' % esc(info.get('status'))
    methods = []
    for side, key in (('old_methods', 'old'), ('methods', 'new')):
        for method in (review or {}).get(side, {}).values():
            if method.get('path') == (oldpath if key == 'old' else path):
                methods.append((key, method.get('start', 0), method.get('end', 0)))
    chunks = ['<div class="controls"><button onclick="more(this,\'before\')">展开上方代码</button> <button onclick="more(this,\'after\')">展开下方代码</button> <button onclick="showMethods(this)">显示变化所在完整方法</button> <button onclick="showAll(this)">显示完整文件</button> <button onclick="jump(this,-1)">上一处</button> <button onclick="jump(this,1)">下一处</button> <span class="position"></span></div>',
              '<div class="diffscroll"><table class="diff"><colgroup><col class="num-col"><col class="code-col"><col class="num-col"><col class="code-col"></colgroup><thead><tr><th colspan="2">旧版 ' + esc(ledger['base'][:12]) + '</th><th colspan="2">新版 ' + esc(ledger['head'][:12]) + '</th></tr></thead><tbody>']
    change_ranges = [(c.get('old_range', {}), c.get('new_range', {})) for c in changes]
    relevant_methods = []
    for side, start, end in methods:
        idx = 0 if side == 'old' else 1
        if any(r[idx].get('start', 0) <= end and r[idx].get('start', 0) + max(r[idx].get('count', 0), 1) - 1 >= start for r in change_ranges):
            relevant_methods.append((side, start, end))
    distances = [len(rows)] * len(rows)
    directions = ['after'] * len(rows)
    last = -len(rows)
    for i in range(len(rows)):
        if rows[i][4] == 'change': last = i
        distances[i] = i - last
    last = 2 * len(rows)
    for i in range(len(rows)-1, -1, -1):
        if rows[i][4] == 'change': last = i
        if last - i < distances[i]:
            distances[i] = last - i
            directions[i] = 'before'
    for i, (ln, left, rn, right, kind) in enumerate(rows):
        distance = distances[i]
        method = any((start <= (ln if side == 'old' else rn) <= end) for side, start, end in relevant_methods if isinstance((ln if side == 'old' else rn), int))
        chunks.append('<tr class="%s" data-distance="%d" data-direction="%s" data-method="%d"%s><td class="num">%s</td><td class="code old">%s</td><td class="num">%s</td><td class="code new">%s</td></tr>' %
                      (kind, distance, directions[i], method, ' hidden' if distance > 10 else '', ln, left, rn, right))
    chunks.append('</tbody></table></div>')
    if not relevant_methods:
        chunks.append('<p class="gap">方法范围未解析；可展开完整文件。</p>')
    return ''.join(chunks)


STYLE = '''
body{font:14px "Segoe UI",sans-serif;background:#f4f6fa;color:#182334;margin:0}header{background:#192c46;color:white;padding:24px 4%}main{max-width:1500px;margin:auto;padding:22px}h1{margin:0 0 8px}h2{margin-top:32px}section,.card{background:white;border:1px solid #dfe5ee;border-radius:9px;padding:16px;margin:12px 0}.alert{border-left:5px solid #e25956}.gap{color:#805500}small{color:#607087}button,input,select{padding:7px;border:1px solid #cbd3dd;border-radius:5px}button{cursor:pointer}.filters{display:flex;gap:8px;flex-wrap:wrap}.file summary{cursor:pointer;font-weight:600}.controls{padding:12px 0}.diffscroll{overflow:auto;max-height:680px;border-radius:5px;background:#202329}.diff{border-collapse:collapse;table-layout:fixed;width:100%;min-width:1200px;color:#c6d2e0;font:13px Consolas,monospace}.diff th{position:sticky;top:0;background:#303641;padding:8px;text-align:left}.diff td{vertical-align:top}.diff .num{width:48px;text-align:right;color:#8190a2;padding:0 8px;user-select:none}.diff .code{width:calc(50% - 48px);white-space:pre;padding:1px 8px}.change .old{background:#542d36}.change .new{background:#214b3e}.change mark{background:#9e4d55;color:white}.change .new mark{background:#387f5b}a{color:#1769aa}details{margin:10px 0}.commit{border-top:1px solid #eee;padding:7px}.badge{background:#e7eef8;border-radius:4px;padding:3px 6px}tr[hidden],details[hidden],article[hidden]{display:none}
.evidence-panel{position:fixed;top:0;right:0;bottom:0;width:min(54vw,900px);box-sizing:border-box;background:#fff;box-shadow:-6px 0 24px #0003;z-index:10;display:flex;flex-direction:column}.evidence-panel[hidden]{display:none}.evidence-heading{display:flex;justify-content:space-between;align-items:center;padding:14px 20px;background:#192c46;color:white}.evidence-heading strong{font-size:16px}.evidence-body{padding:0 20px 24px;overflow:auto;overflow-wrap:anywhere}.evidence-body h2{margin-top:20px}.evidence-body .diffscroll{max-height:none}.evidence-heading button{background:white;color:#182334}@media(max-width:700px){.evidence-panel{width:100%}}
.diff{min-width:960px}.diff col.num-col{width:48px}.diff col.code-col{width:calc(50% - 48px)}
.commit-diff{overflow:auto;max-height:420px;padding:12px;background:#202329;color:#c6d2e0;white-space:pre;font:13px Consolas,monospace}
@media(min-width:901px){body.evidence-open main{width:30%;max-width:none;box-sizing:border-box;margin:0;overflow-wrap:anywhere}body.evidence-open .evidence-panel{width:70vw}body.evidence-open main .diffscroll{max-width:100%}}
@media(max-width:900px){.evidence-panel{width:100%}}
'''
SCRIPT = '''
function rows(b){return [...b.closest('.file').querySelectorAll('tbody tr')]}
function more(b,d){let box=b.closest('.file');let n=Number(box.dataset[d]||10)+10;box.dataset[d]=n;rows(b).forEach(r=>{if(r.dataset.direction===d&&Number(r.dataset.distance)<=n)r.hidden=false})}
function showAll(b){rows(b).forEach(r=>r.hidden=false)}
function showMethods(b){rows(b).forEach(r=>{if(r.dataset.method==='1')r.hidden=false})}
function jump(b,step){let all=rows(b).filter(r=>r.classList.contains('change'));let starts=all.filter((r,i)=>i===0||r.previousElementSibling!==all[i-1]);if(!starts.length)return;let box=b.closest('.file');let n=(Number(box.dataset.jump||0)+step+starts.length)%starts.length;box.dataset.jump=n;starts[n].hidden=false;starts[n].scrollIntoView({block:'center'});box.querySelector('.position').textContent=(n+1)+' / '+starts.length}
function filter(){let q=document.getElementById('query').value.toLowerCase(),p=document.getElementById('project').value,t=document.getElementById('ticket').value;document.querySelectorAll('[data-search]').forEach(x=>{x.hidden=!(x.dataset.search.includes(q)&&(!p||x.dataset.project===p)&&(!t||x.dataset.tickets.split(' ').includes(t)))})}
let evidenceTrigger=null;
function preserveEvidencePosition(change){let top=evidenceTrigger&&evidenceTrigger.getBoundingClientRect().top;change();if(evidenceTrigger&&top!==null)window.scrollBy(0,evidenceTrigger.getBoundingClientRect().top-top)}
function closeEvidence(){let panel=document.getElementById('evidence-panel');preserveEvidencePosition(()=>{panel.hidden=true;document.body.classList.remove('evidence-open')});document.getElementById('evidence-body').replaceChildren();if(evidenceTrigger)evidenceTrigger.focus({preventScroll:true})}
function showEvidenceFile(button){let full=button.parentElement.querySelector('.evidence-full-file');if(!full)return;full.hidden=!full.hidden;button.textContent=full.hidden?'显示完整文件左右对照':'收起完整文件左右对照'}
document.addEventListener('click',function(event){let link=event.target.closest('a[data-evidence]');if(!link||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey)return;let template=document.getElementById('evidence-'+link.dataset.evidence);if(!template)return;event.preventDefault();evidenceTrigger=link;let panel=document.getElementById('evidence-panel'),body=document.getElementById('evidence-body');body.replaceChildren(template.content.cloneNode(true));let original=[...document.querySelectorAll('details.file a[data-evidence]')].find(a=>a.dataset.evidence===link.dataset.evidence);let table=original&&original.closest('details.file').querySelector('.diffscroll');if(table){let controls=document.createElement('div'),button=document.createElement('button'),full=table.cloneNode(true);full.classList.add('evidence-full-file');full.hidden=true;full.querySelectorAll('tr[hidden]').forEach(row=>row.hidden=false);button.type='button';button.textContent='显示完整文件左右对照';button.addEventListener('click',()=>showEvidenceFile(button));controls.append(button,full);let context=body.querySelector('.diffscroll');if(context)context.before(controls);else body.append(controls)}preserveEvidencePosition(()=>{panel.hidden=false;document.body.classList.add('evidence-open')});body.scrollTop=0;document.getElementById('evidence-close').focus({preventScroll:true})});
document.addEventListener('keydown',function(event){if(event.key==='Escape'&&!document.getElementById('evidence-panel').hidden)closeEvidence()});
'''


def write_html(root, release, specs, reviews, manifest=None):
    repos = {project: repo for repo, project, _, _ in specs}
    ignored = _noise_changes(release['ledgers'], repos)
    filtered_ledgers = [{**ledger, 'final_changes': [c for c in ledger['final_changes'] if c['id'] not in ignored]}
                        for ledger in release['ledgers']]
    sdk = analyze(specs, filtered_ledgers, manifest)
    reviewmap = {r['project']: r for r in reviews}
    allfiles, tickets = [], set()
    for ledger in filtered_ledgers:
        byfile = defaultdict(list)
        for c in ledger['final_changes']:
            byfile[c['path']].append(c)
        for path, changes in byfile.items():
            labels = set()
            for c in changes:
                keys, _ = attribution(c)
                labels.update(keys or ['EMPTY'])
            tickets.update(labels)
            allfiles.append((ledger, path, changes, labels))
    alerts = _group_sdk_alerts(sdk['alerts'])
    contract_impacts = sdk.get('contract_impacts', [])
    table_result = sdk.get('database', {})
    table_impacts = table_result.get('impacts', [])
    ticket_groups = {}
    for ledger, path, changes, labels in allfiles:
        known = labels - {'EMPTY'}
        if path.endswith('.java') and len(known) > 1:
            project = ledger['project']
            if project not in ticket_groups:
                ticket_groups[project] = {'kind': '项目中的多工单交叉', 'project': project, 'classes': []}
                alerts.append(ticket_groups[project])
            line_shas = {sha for c in changes for sha in c.get('line_commit_candidates', [])}
            line_commits = [commit for commit in ledger.get('history', {}).get('commits', [])
                            if commit['sha'] in line_shas]
            ticket_groups[project]['classes'].append({
                'path': path, 'tickets': sorted(known),
                'file_status': changes[0].get('file_status', ''),
                'line_commits': line_commits,
                'evolution': _file_evolution(repos.get(project), ledger, path, changes),
                'change_ids': [c['id'] for c in changes],
                'attribution': ['%s：%s（%s）' % (c['id'], '、'.join(attribution(c)[0]) or '未归属', attribution(c)[1]) for c in changes]})
    sdk['human_priority_count'] = len(alerts) + len(contract_impacts) + len(table_impacts)
    out = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>版本差异审核</title><style>', STYLE, '</style></head><body><header><h1>版本差异审核</h1><div>固定版本最终净差异 · 静态候选需人工确认 · 不代表实际部署</div></header><main>']
    out.append('<section><h2>比较范围与覆盖</h2>')
    versions = {item['project']: item for item in release.get('comparisons', [])}
    for led in release['ledgers']:
        phase = next((p.get('static_scan') for p in release.get('project_phase_status', []) if p['project'] == led['project']), '未知')
        version = versions.get(led['project'])
        label = ('%s → %s　｜　' % (esc(version['old_version']), esc(version['new_version']))) if version else ''
        out.append('<p><b>%s</b>　%s%s → %s　｜　%d 个差异　｜　提交历史 %s　｜　静态扫描 %s</p>' % (esc(led['project']), label, esc(led['base']), esc(led['head']), len(led['final_changes']), esc(led['history']['status']), esc(phase)))
    for p in sdk['projects']:
        if p['source'] in ('登记固定版本', '远端默认分支'):
            out.append('<p>%s：%s %s　固定 SHA %s（仅单一版本，无法判断升级过程或实际部署）</p>' % (esc(p['project']), esc(p['source']), esc(p.get('branch') or ''), esc(p['head'])))
    for gap in sdk['gaps']:
        out.append('<p class="gap">⚠ ' + esc(gap) + '</p>')
    table_gaps = table_result.get('omissions', [])
    if table_gaps:
        out.append('<p class="gap">数据库资源索引有 %d 处解析或扫描缺口；详情见 database_impact_latest.json。未发现同表关联不等于无依赖。</p>' % len(table_gaps))
        for gap in table_gaps[:15]:
            out.append('<p class="gap">表索引缺口：%s</p>' % esc(json.dumps(gap, ensure_ascii=False)))

    out.append('<p class="gap">仅检查本次输入及列出的取证项目；默认分支不代表实际部署。间接依赖、外部父 POM、反射和动态调用可能未覆盖。</p>')
    out.append('<p>人工视图已折叠 %d 处 Java 纯 import 或空白格式差异；原始底账和证据保留。无法可靠判断或混有代码变化时仍展示。</p></section>' % len(ignored))
    out.append('<section><h2>优先确认 <span class="badge">%d 项</span></h2>' % sdk['human_priority_count'])
    panels = []
    for index, a in enumerate(alerts):
        if a['kind'] == '项目中的多工单交叉':
            tickets = sorted({ticket for item in a['classes'] for ticket in item['tickets']})
            out.append('<article class="card alert"><b>项目中的多工单交叉：%s</b><p><b>为什么重点展示：</b>同一项目中有 %d 个类的最终差异分别关联多个工单候选，需要核对是否相互影响。</p><p>涉及工单：%s</p><p>按类查看：</p>' % (
                esc(a['project']), len(a['classes']), esc('、'.join(tickets))))
            for item in a['classes']:
                out.append('<details><summary>%s　·　%s%s</summary>' % (
                    esc(item['path']), esc('、'.join(item['tickets'])),
                    '　·　新增文件' if item['file_status'].startswith('A') else ''))
                if item['file_status'].startswith('A'):
                    if len(item['line_commits']) > 1:
                        out.append('<p>旧版不存在此文件；新版文件中有来自多个提交的保留行。工单候选依据这些行的最后修改提交，不能仅据此认定存在冲突。</p>')
                    else:
                        out.append('<p>旧版不存在此文件；工单归属仅为候选，不能仅据此认定存在冲突。</p>')
                if item['line_commits']:
                    out.append('<p>保留行的提交候选：</p><ul>')
                    for commit in item['line_commits']:
                        out.append('<li>%s　%s</li>' % (esc(commit['sha'][:12]), esc(commit['subject'])))
                    out.append('</ul>')
                evolution = item['evolution']
                out.append('<p><b>比较范围内的文件演进：</b></p>')
                if evolution['events']:
                    if not evolution['linear']:
                        out.append('<p class="gap">部分提交不存在可确认的祖先关系，列表为拓扑顺序，不能认定实际先后。</p>')
                    out.append('<ol>')
                    for event in evolution['events']:
                        out.append('<li><b>%s</b>　%s　%s%s%s' % (
                            esc(event['operation']), esc(event['sha'][:12]), esc(event['subject']),
                            '　（最终有保留行）' if event['final_lines'] else '　（最终保留行未确认）',
                            '　（合并提交，仅与第一父提交比较）' if event['merge'] else ''))
                        if event['patch']:
                            out.append('<details><summary>查看该提交对此文件的差异</summary><pre class="commit-diff">%s</pre>%s</details>' % (
                                esc(event['patch']), '<p class="gap">差异超过 20000 字符，已截断。</p>' if event['truncated'] else ''))
                        out.append('</li>')
                    out.append('</ol><p>这些提交说明中间演进；本次上线差异仍以所选两个版本的最终对比为准。</p>')
                if evolution['status'] != 'COMPLETE':
                    out.append('<p class="gap">文件演进证据不完整，不能确定全部新增及后续修改过程。</p>')
                out.append('<p>%s</p><p>差异证据：%s</p></details>' % (
                    esc('；'.join(item['attribution'])),
                    '　'.join(_evidence_link(i) for i in item['change_ids'])))
            out.append('<p>待确认：工单归属及差异是否相互影响；关联不代表已发现冲突。</p></article>')
            continue
        s = a.get('sdk', {})
        evidence = ('SDK %s / %s · %s:%s · 本次比较 %s → %s（%s → %s）' % (s.get('project'), s.get('module'), s.get('group'), s.get('artifact'), s.get('old_version'), s.get('new_version'), (s.get('base_sha') or '未知')[:12], (s.get('head_sha') or '未知')[:12])) if s else ''
        if a['kind'] == 'SDK 与消费者项目':
            out.append('<article class="card alert"><b>SDK 与消费者项目：%s</b><p><b>为什么重点展示：</b>%s</p><p>%s</p>' % (
                esc(a['consumer']), esc('SDK 发生变化，需核对该项目的依赖声明和相关变更类。'), esc(evidence)))
            for number, declaration in enumerate(a['declarations']):
                panel_id = 'consumer-%d-%d' % (index, number)
                panels.append(_consumer_panel(panel_id, declaration))
                out.append('<p><b>依赖声明：</b>%s　%s → %s<br>%s<br><a href="#evidence-panel" data-evidence="%s">查看消费者依赖声明（旧版和新版）</a></p>' % (
                    esc(declaration['path']), esc(declaration.get('old_version')), esc(declaration.get('new_version')),
                    esc(declaration['reason']), esc(panel_id)))
            if not a['declarations']:
                out.append('<p>未发现需要单独提示的依赖版本差异；以下为相关变更类线索。</p>')
            if s.get('changes'):
                out.append('<p>SDK 差异证据：' + '　'.join(_evidence_link(i) for i in s['changes']) + '</p>')
            if a['references']:
                out.append('<details><summary>相关变更类和引用（%d 处）</summary>' % len(a['references']))
                for ref in a['references']:
                    out.append('<p><b>%s</b><br>工单候选：%s<br>引用线索：%s<br>变化证据：%s</p>' % (
                        esc(ref.get('path')), esc('、'.join(ref.get('tickets', [])) or '未归属'),
                        esc('、'.join(ref.get('imports', [])) or '未提供'),
                        '　'.join(_evidence_link(i) for i in ref.get('change_ids', []))))
                out.append('</details>')
            out.append('<p>待确认：是否需要配套修改或升级；源码引用不证明运行时调用或缺陷。</p></article>')
            continue
        ids = '、'.join(s.get('changes', []) + a.get('change_ids', []))
        out.append('<article class="card alert"><b>%s</b><p><b>为什么重点展示：</b>%s</p><p>%s / %s</p>' % tuple(map(esc, [a['kind'], a['reason'], a.get('consumer', s.get('project', '')), a.get('path', s.get('module', ''))])))
        if s:
            out.append('<small>%s<br>依赖版本：%s → %s<br>引用：%s<br>工单：SDK %s；消费方 %s</small><p>待确认：是否需要配套修改或升级；静态关联不是缺陷结论。</p>' % tuple(map(esc, [evidence, a.get('old_version', '—'), a.get('new_version', '—'), ', '.join(a.get('imports', [])) or '见依赖声明', ', '.join(s.get('tickets', [])) or '未归属', ', '.join(a.get('tickets', [])) or '未归属'])))
        else:
            out.append('<small>%s</small><p>待确认：这些差异是否相互影响，工单归属是否准确。</p>' % esc('；'.join(a.get('attribution', []))))
        if a.get('consumer_context'):
            panel_id = 'consumer-' + str(index)
            panels.append(_consumer_panel(panel_id, a))
            out.append('<p><a href="#evidence-panel" data-evidence="%s">查看消费者依赖声明（旧版和新版）</a></p>' % esc(panel_id))
        if ids:
            out.append('<p>SDK 和代码变化证据：' + '　'.join(_evidence_link(i) for i in s.get('changes', []) + a.get('change_ids', [])) + '</p>')
        out.append('</article>')
    for finding in contract_impacts:
        s = finding['sdk']
        out.append('<article class="card alert"><b>SDK 契约与消费者项目：%s</b>'
                   '<p>SDK %s / %s · %s:%s</p>'
                   '<p>生产方 %s → %s；消费方固定版本 %s；声明依赖版本 %s</p>'
                   '<p class="gap">静态类型作用域内的成员使用候选，不证明实际类型绑定、契约不兼容或生产部署。</p>' %
                   (esc(finding['consumer']), esc(s['project']), esc(s['module']),
                    esc(s['group']), esc(s['artifact']),
                    esc((s.get('base_sha') or '')[:12]), esc((s.get('head_sha') or '')[:12]),
                    esc((finding.get('consumer_head_sha') or '')[:12]),
                    esc(str(finding.get('dependency_version') or '未知'))))
        by_change = {}
        for use in finding['uses']:
            c = use['contract']
            key = (c['path'], c['identity'], c['status'])
            by_change.setdefault(key, []).append(use)
        for (path, member, status), uses in sorted(by_change.items()):
            c = uses[0]['contract']
            out.append('<details><summary>%s · %s · %s（%d 处用法）</summary>' %
                       (esc(path), esc(member), esc(status), len(uses)))
            out.append('<p>原契约：%s；新契约：%s</p><p>生产方差异：%s</p>' %
                       (esc(str((c['old'] or {}).get('signature') or '不存在')),
                        esc(str((c['new'] or {}).get('signature') or '不存在')),
                        ' '.join(_evidence_link(i) for i in c['change_ids'])))
            for use in uses:
                out.append('<p>%s:%d（%s） <code>%s</code></p>' %
                           (esc(use['consumer_path']), use['line'],
                            esc(use['binding_status']), esc(use['excerpt'])))
            out.append('</details>')
        out.append('<p>待确认：实际类型绑定、API 兼容性和是否需要调整消费者。</p></article>')
    for item in table_impacts:
        out.append('<article class="card alert"><b>跨项目数据库表：%s</b>'
                   '<p>变化项目：%s（%s → %s） · 消费项目：%s（%s）</p>'
                   '<p>匹配等级：%s；未核实数据库实例、Schema、环境及部署版本。</p>' %
                   (esc(item['table']), esc(item['producer_project']),
                    esc(item['producer_base_sha'][:12]),
                    esc(item['producer_head_sha'][:12]),
                    esc(item['consumer_project']), esc(item['consumer_sha'][:12]),
                    esc(item['status'])))
        out.append('<p>变化差异证据：%s</p>' %
                   ' '.join(_evidence_link(change_id) for change_id in item['change_ids']))
        out.append('<details><summary>变化方表访问（%d 处）</summary>' %
                   len(item['producer_uses']))
        for use in item['producer_uses']:
            out.append('<p>%s · %s · %s:%d–%d · %s · 数据源线索：%s</p>' %
                       (esc(use['side']), esc(use['role']), esc(use['path']),
                        use['start'], use['end'], esc(use['table']),
                        esc(use.get('datasource_hint') or '未知')))
        out.append('</details><details><summary>其他项目的表使用位置（%d 处）</summary>' %
                   len(item['consumer_uses']))
        for use in item['consumer_uses']:
            out.append('<p>%s · %s:%d–%d · %s · 数据源线索：%s</p>' %
                       (esc(use['role']), esc(use['path']), use['start'], use['end'],
                        esc(use['table']), esc(use.get('datasource_hint') or '未知')))
        out.append('</details><p>待确认：是否为同一数据库/实例/环境、真实读写路径及表结构兼容性。同名表不是确定依赖。</p></article>')
    if not alerts and not contract_impacts and not table_impacts:
        out.append('<p>未发现重点候选；不代表没有影响，请检查覆盖缺口。</p>')
    out.append('</section><section><h2>程序关注的场景</h2><ul><li>同类多工单：汇总最终差异工单候选，文件级归因需复核。</li><li>SDK 变化：检查本次变更项目 API/SDK 模块源码和模块版本。</li><li>消费者升级：仅在 SDK 的两个固定版本之间版本发生变化时比较登记项目的声明；只有单版本时不能判断升级。不检查第三方依赖。</li><li>SDK 契约旧用法：展示固定 SHA 的字段、方法或枚举变化及消费者使用候选，import 本身不是重点。</li><li>跨项目数据库表：从变化 SQL/DDL/显式映射位置关联其他固定版本的同名表，均需核实数据源与实例。</li><li>解析不足：展示未解析父 POM、依赖、扫描和项目范围。</li></ul></section>')
    out.append('<section><h2>最终差异</h2><div class="filters"><input id="query" placeholder="搜索项目、类、工单" oninput="filter()"><select id="project" onchange="filter()"><option value="">全部项目</option>')
    for project in sorted({l['project'] for l, _, _, _ in allfiles}): out.append('<option>' + esc(project) + '</option>')
    out.append('</select><select id="ticket" onchange="filter()"><option value="">全部工单</option>')
    for ticket in sorted(tickets): out.append('<option>' + esc(ticket) + '</option>')
    out.append('</select></div>')
    for led, path, changes, labels in allfiles:
        project = led['project']
        search = (project + ' ' + path + ' ' + ' '.join(labels)).lower()
        out.append('<details class="file" data-search="%s" data-project="%s" data-tickets="%s"><summary>%s / %s　<span class="badge">%s</span>　%d 处</summary>' % (esc(search), esc(project), esc(' '.join(sorted(labels))), esc(project), esc(path), esc('、'.join(sorted(labels))), len(changes)))
        for c in changes:
            keys, basis = attribution(c)
            out.append('<p>%s　%s：%s</p>' % (_evidence_link(c['id']), esc(basis), esc('、'.join(keys) or 'EMPTY / 未归属')))
        originals = next(l for l in release['ledgers'] if l['project'] == project and l['base'] == led['base'] and l['head'] == led['head'])
        suppressed = [c for c in originals['final_changes'] if c['path'] == path and c['id'] in ignored]
        out.append(_file_view(repos.get(project), led, path, changes, reviewmap.get(project), suppressed))
        out.append('</details>')
    out.append('<details><summary>已折叠的 import / 空白格式差异（%d 处）</summary>' % len(ignored))
    for led in release['ledgers']:
        for c in led['final_changes']:
            if c['id'] in ignored:
                out.append('<p>%s / %s　%s　%s</p>' % (esc(led['project']), esc(c['path']), esc(ignored[c['id']]), _evidence_link(c['id'])))
    out.append('</details>')
    out.append('</section><section><h2>比较范围内的提交</h2><p>提交历史用于解释来源，最终差异以两端代码为准。</p>')
    for led in release['ledgers']:
        out.append('<details><summary>%s · %s · %s 条</summary>' % (esc(led['project']), esc(led['history']['status']), esc(led['history']['total'])))
        for c in led['history']['commits']:
            out.append('<div class="commit"><b>%s</b>　%s　%s</div>' % (esc(', '.join(c['issue_keys']) or 'EMPTY'), esc(c['sha'][:12]), esc(c['subject'])))
        out.append('</details>')
    out.append('</section><p><a href="report.md">Markdown 证据入口</a> · <a href="ai_review.md">AI 输入材料</a> · <a href="analysis.json">结构化底账</a></p></main>')
    out.append('<aside id="evidence-panel" class="evidence-panel" aria-label="差异证据详情" hidden><div class="evidence-heading"><strong>差异证据详情</strong><button id="evidence-close" onclick="closeEvidence()" aria-label="关闭详情">关闭 ×</button></div><div id="evidence-body" class="evidence-body"></div></aside>')
    out.extend(panels)
    for led in release['ledgers']:
        for change in led['final_changes']:
            out.append(_evidence_panel(change))
    out.append('<script>' + SCRIPT + '</script></body></html>')
    (root / 'report.html').write_text(''.join(out), encoding='utf-8')
    return sdk
