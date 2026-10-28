"""固定 Git 版本的 Maven 声明检查，不运行 Maven 或项目代码。"""
import json
import posixpath
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .gitops import git, check_sha
from .architecture_sources import default_projects, isolate_specs, source_name
from .sdk_contracts import analyze_contract_uses
from .database_inventory import analyze_table_impacts
from .external_influence import analyze_external_influence
from .query_contract import analyze_query_contracts
from .shared_resources import analyze_shared_resources
from .instance_identity import attach_instance_evidence


def _text(node, name):
    child = node.find(name) if node is not None else None
    return (child.text or "").strip() if child is not None else ""


def _expand(value, props):
    for _ in range(20):
        new = re.sub(r"\$\{([^}]+)\}", lambda m: props.get(m.group(1), m.group(0)), value or "")
        if new == value:
            break
        value = new
    return None if not value or "${" in value else value


def _version(value):
    if not value or value.upper() in ('LATEST', 'RELEASE') or any(c in value for c in '[],()'):
        return None
    return value


def _dependency_context(repo, sha, path, artifact):
    if not sha:
        return {'status': '未提供此侧版本', 'lines': []}
    try:
        data = git(repo, 'show', sha + ':' + path)
        if len(data) > 2_000_000 or b'\x00' in data:
            return {'status': '文件过大或非文本', 'lines': []}
        lines = data.decode('utf-8', 'replace').splitlines()
        hit = next((i for i, line in enumerate(lines) if re.search(
            r'<artifactId>\s*' + re.escape(artifact) + r'\s*</artifactId>', line)), None)
        if hit is None:
            return {'status': '未找到依赖声明，请查看 POM', 'lines': []}
        start = max(0, hit - 4)
        end = min(len(lines), hit + 6)
        return {'status': '已读取声明附近的代码',
                'lines': [{'line': i + 1, 'text': lines[i]} for i in range(start, end)]}
    except (OSError, RuntimeError) as exc:
        return {'status': '无法读取：' + str(exc)[:200], 'lines': []}


def inventory(repo, sha):
    names = [x.decode('utf-8', 'replace') for x in git(repo, 'ls-tree', '-r', '--name-only', '-z', sha).split(b'\0') if x]
    raw, errors = {}, []
    for path in names:
        if path == 'pom.xml' or path.endswith('/pom.xml'):
            try:
                data = git(repo, 'show', sha + ':' + path)
                if len(data) > 2_000_000 or b'<!DOCTYPE' in data.upper():
                    raise ValueError('POM 过大或包含 DTD')
                node = ET.fromstring(data)
                for element in node.iter():
                    element.tag = element.tag.split('}')[-1]
                raw[path] = node
            except (ValueError, ET.ParseError, RuntimeError) as exc:
                errors.append(path + ': ' + str(exc))
    cache = {}
    def resolve(path, visiting=()):
        if path in cache:
            return cache[path]
        if path in visiting or path not in raw:
            return None
        root = raw[path]
        parent = root.find('parent')
        relative_node = parent.find('relativePath') if parent is not None else None
        relative = _text(parent, 'relativePath') if parent is not None else ''
        parent_path = posixpath.normpath(posixpath.join(posixpath.dirname(path), relative or '../pom.xml'))
        if parent_path != 'pom.xml' and not parent_path.endswith('/pom.xml'):
            parent_path = posixpath.join(parent_path, 'pom.xml')
        inherited = resolve(parent_path, visiting + (path,)) if parent is not None and parent_path in raw and not (relative_node is not None and not relative) else None
        if inherited:
            declared = [_expand(_text(parent, key), inherited['props']) for key in ('groupId', 'artifactId', 'version')]
            actual = [inherited['group'], inherited['artifact'], inherited['version']]
            if any(not expected or not found or expected != found for expected, found in zip(declared, actual)):
                inherited = None
        props = dict(inherited['props']) if inherited else {}
        properties = root.find('properties')
        if properties is not None:
            props.update({e.tag: (e.text or '').strip() for e in properties})
        group = _text(root, 'groupId') or _text(parent, 'groupId')
        version = _text(root, 'version') or _text(parent, 'version')
        artifact = _text(root, 'artifactId')
        props.update({'project.groupId': group, 'pom.groupId': group,
                      'project.version': version, 'pom.version': version,
                      'project.artifactId': artifact})
        group, artifact, version = (_expand(x, props) for x in (group, artifact, version))
        version = _version(version)
        props.update({'project.groupId': group or '', 'project.version': version or '',
                      'pom.groupId': group or '', 'pom.version': version or ''})
        managed = dict(inherited['managed']) if inherited else {}
        for d in root.findall('./dependencyManagement/dependencies/dependency'):
            g, a, v = (_expand(_text(d, x), props) for x in ('groupId', 'artifactId', 'version'))
            if g and a:
                managed[(g, a)] = _version(v)
        dependencies = list(inherited['dependencies']) if inherited else []
        for d in root.findall('./dependencies/dependency'):
            g, a, v = (_expand(_text(d, x), props) for x in ('groupId', 'artifactId', 'version'))
            if g and a:
                dependencies = [old for old in dependencies if (old['group'], old['artifact']) != (g, a)]
            dependencies.append({'group': g, 'artifact': a, 'version': _version(v) if _text(d, 'version') else managed.get((g, a)),
                                 'scope': _text(d, 'scope'), 'path': path})
        result = {'path': path, 'group': group, 'artifact': artifact, 'version': version,
                  'props': props, 'managed': managed, 'dependencies': dependencies,
                  'parent_unresolved': parent is not None and inherited is None}
        cache[path] = result
        return result
    modules = [resolve(p) for p in raw]
    return {'modules': modules, 'names': names, 'errors': errors}


def registry(path, specs):
    entries = {project: {'repo': repo, 'base': base, 'head': head, 'source': '比较项目'}
               for repo, project, base, head in specs if repo is not None}
    defaults, gaps = default_projects(specs)
    for item in defaults:
        entries[item['project']] = {k: item[k] for k in ('repo', 'base', 'head', 'source', 'branch')}
    if path:
        source = Path(path).expanduser().resolve()
        data = json.loads(source.read_text(encoding='utf-8-sig'))
        if not isinstance(data, dict) or not isinstance(data.get('projects'), list):
            raise ValueError('项目登记清单须包含 projects 数组')
        seen = set()
        for item in data['projects']:
            project, location, ref = item['project'], item['repo'], item['revision']
            if project in seen:
                raise ValueError('项目登记重复：' + project)
            seen.add(project)
            if project in entries:
                continue
            repo = Path(location).expanduser()
            if not repo.is_absolute():
                repo = source.parent / repo
            try:
                if not isinstance(ref, str) or not ref or ref.startswith('-') or '\n' in ref or '\r' in ref:
                    raise ValueError('无效修订号')
                sha = check_sha(repo, git(repo, 'rev-parse', '--verify', '--end-of-options', ref + '^{commit}').decode('ascii').strip())
                name = source_name(repo)
                if name and any(source_name(item[0]) == name for item in specs if item[0] is not None):
                    continue
                isolated, failures, active = isolate_specs([(repo, project, sha, sha)])
                if failures:
                    raise ValueError(failures[0]['reason'])
                if active:
                    repo = isolated[0][0]
                entries[project] = {'repo': repo.resolve(), 'base': None, 'head': sha, 'source': '登记固定版本'}
            except (OSError, ValueError, RuntimeError) as exc:
                gaps.append(project + '：登记版本不可读取：' + str(exc))
    elif not defaults and not gaps:
        gaps.append('未提供登记清单；只检查本次比较项目。未登记项目未扫描。')
    return entries, gaps


def analyze(specs, ledgers, manifest=None):
    entries, gaps = registry(manifest, specs)
    snapshots = {}
    for project, item in entries.items():
        snapshots[project] = {}
        for side in ('base', 'head'):
            sha = item[side]
            if not sha:
                continue
            try:
                snapshots[project][side] = inventory(item['repo'], sha)
                gaps.extend(project + ' ' + side + '：' + x for x in snapshots[project][side]['errors'])
                if not snapshots[project][side]['modules']:
                    gaps.append(project + ' ' + side + '：未发现可读取的 Maven POM；无法检查 Maven 依赖')
            except (OSError, RuntimeError, ValueError) as exc:
                gaps.append(project + ' ' + side + '：POM 扫描失败：' + str(exc))
    led_by_project = {x['project']: x for x in ledgers}
    sdks, alerts = [], []
    for project, sides in snapshots.items():
        oldmods = {m['path']: m for m in sides.get('base', {}).get('modules', [])}
        newmods = {m['path']: m for m in sides.get('head', {}).get('modules', [])}
        for module_path in sorted(set(oldmods) | set(newmods)):
            module = newmods.get(module_path) or oldmods[module_path]
            name = (module['artifact'] or posixpath.basename(posixpath.dirname(module['path']))).lower()
            if not (name == 'api' or name.endswith('-api') or name.endswith('-sdk') or name == 'sdk'):
                continue
            prefix = posixpath.dirname(module['path'])
            prefix = prefix + '/' if prefix else ''
            changes = [c for c in led_by_project.get(project, {}).get('final_changes', [])
                       if (c['path'].startswith(prefix) or c.get('old_path', '').startswith(prefix)) and (c['path'].endswith('.java') or c['path'] == module['path'] or c.get('old_path') == module['path'])]
            if not changes:
                continue
            old = oldmods.get(module['path'])
            sdk = {'project': project, 'module': module['path'], 'group': module['group'],
                   'artifact': module['artifact'], 'old_version': old['version'] if old else None,
                   'base_sha': entries[project]['base'], 'head_sha': entries[project]['head'],
                   'new_version': newmods.get(module_path, {}).get('version'), 'changes': [c['id'] for c in changes],
                   'tickets': sorted({key for c in changes for key in (c.get('manual_attribution', {}).get('issue_keys', []) if c.get('manual_attribution') else c.get('line_issue_candidates', []) if c.get('line_commit_candidates') else c.get('issue_candidates', []))})}
            sdk['java_changes'] = [
                {'id': c['id'], 'path': c['path'],
                 'old_path': c.get('old_path', c['path']),
                 'new_range': c.get('new_range'),
                 'old_range': c.get('old_range')}
                for c in changes if c['path'].endswith('.java')
                or c.get('old_path', '').endswith('.java')]
            sdk['java_change_paths'] = sorted({
                path for c in sdk['java_changes']
                for path in (c['path'], c['old_path'])
                if path.endswith('.java')})
            sdks.append(sdk)
            if not sdk['group'] or not sdk['artifact']:
                gaps.append(project + ' / ' + module['path'] + '：SDK 坐标不完整，无法匹配消费者')
                continue
            if not sdk['old_version'] or not sdk['new_version'] or sdk['old_version'] == sdk['new_version']:
                alerts.append({'kind': 'SDK自身版本', 'reason': 'SDK 源码或 POM 发生变化，但模块版本未变化或无法解析。', 'sdk': sdk})
            version_changed = bool(sdk['old_version'] and sdk['new_version'] and sdk['old_version'] != sdk['new_version'])
            if not sdk['old_version'] or not sdk['new_version']:
                gaps.append(project + ' / ' + module['path'] + '：本次 SDK 版本变化无法确定，未进行消费者版本对照')
            for consumer, csides in snapshots.items():
                if consumer == project:
                    continue
                def deps(side):
                    return [d for m in csides.get(side, {}).get('modules', []) for d in m['dependencies']
                            if (d['group'], d['artifact']) == (sdk['group'], sdk['artifact'])]
                before, after = deps('base'), deps('head')
                unresolved = [d for m in csides.get('head', {}).get('modules', []) for d in m['dependencies'] if not d['group'] or not d['artifact']]
                if unresolved:
                    gaps.append(consumer + '：存在无法解析坐标的依赖，可能遗漏 SDK 引用')
                if not before and not after:
                    continue
                for dep in (after or before) if version_changed else []:
                    prev = next((d for d in before if d['path'] == dep['path']), None)
                    current = next((d for d in after if d['path'] == dep['path']), None)
                    oldv, newv = (prev or {}).get('version'), (current or {}).get('version')
                    if newv and sdk['new_version'] and newv == sdk['new_version']:
                        continue
                    if not entries[consumer]['base']:
                        status = '仅固定版本，无法判断是否升级；当前声明版本与 SDK 新版不一致或无法确认'
                    elif not oldv or not newv or not sdk['new_version']:
                        status = '依赖版本未知或声明增删，待确认'
                    elif oldv == newv and newv == sdk['old_version']:
                        status = '消费者仍声明使用 SDK 旧版本，待确认是否需要升级'
                    elif oldv == newv:
                        status = '消费者依赖未变化且与 SDK 新版不一致，待确认'
                    else:
                        status = '消费者依赖已变化，但与 SDK 新版不一致，待确认'
                    alerts.append({'kind': 'SDK消费者', 'reason': status, 'sdk': sdk, 'consumer': consumer,
                                   'path': dep['path'], 'old_version': oldv, 'new_version': newv,
                                   'consumer_base_sha': entries[consumer]['base'], 'consumer_head_sha': entries[consumer]['head'],
                                   'consumer_context': {
                                       side: _dependency_context(entries[consumer]['repo'], entries[consumer][side], dep['path'], sdk['artifact'])
                                       for side in ('base', 'head')}})
    for project, sides in snapshots.items():
        for side, inv in sides.items():
            for m in inv['modules']:
                if m['parent_unresolved']:
                    gaps.append(project + ' ' + side + ' / ' + m['path'] + '：外部或无法解析的父 POM')
    # Cross-project table use is independent of Maven dependencies; keep it
    # in its own versioned inventory, alongside the dependency report scope.
    database = analyze_table_impacts(
        entries, ledgers,
        Path(__file__).resolve().parent.parent / '.diff-analysis' / 'cache' / 'table_snapshots')
    query_contract = analyze_query_contracts(
        entries, ledgers,
        Path(__file__).resolve().parent.parent / '.diff-analysis' / 'cache' / 'query_contract')
    external_influence = analyze_external_influence(
        entries, ledgers,
        Path(__file__).resolve().parent.parent / '.diff-analysis' / 'cache' / 'external_influence')
    shared = analyze_shared_resources(
        entries, ledgers,
        Path(__file__).resolve().parent.parent / '.diff-analysis' / 'cache' / 'shared_resources')
    instance_scope = attach_instance_evidence(
        entries, database, shared,
        Path(__file__).resolve().parent.parent / '.diff-analysis' / 'cache' / 'instance_indexes')
    contract = analyze_contract_uses(
        sdks, entries, snapshots, ledgers,
        Path(__file__).resolve().parent.parent / '.diff-analysis' / 'cache' / 'sdk_contract_source')
    gaps.extend('SDK contract: ' + json.dumps(item, ensure_ascii=False)
                for item in contract['gaps'])
    return {'sdks': sdks, 'alerts': alerts,
            'contract_changes': contract['changes'],
            'contract_impacts': contract['impacts'],
            'database': database, 'query_contract': query_contract,
            'external_influence': external_influence, 'shared_resources': shared,
            'instance_scope': instance_scope,
            'gaps': sorted(set(gaps)),
            'projects': [{'project': p, 'base': i['base'], 'head': i['head'], 'source': i['source'],
                          'branch': i.get('branch')} for p, i in entries.items()]}
