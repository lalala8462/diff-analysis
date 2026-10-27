"""在独立 Git 副本中固定架构内项目，绝不切换原项目工作区。"""
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

WORKSPACE = Path('G:/Code/rhf-all')
COPIES = Path(__file__).resolve().parent.parent / 'project_code'


class SourceError(RuntimeError):
    pass


def mr_copy_path(project, copies=COPIES):
    name = re.sub(r'[^A-Za-z0-9_-]+', '-', project.rsplit('/', 1)[-1]).strip('-') or 'project'
    parent = copies / '_mr'
    parent.mkdir(parents=True, exist_ok=True)
    return parent / (name[:48] + '-' + uuid4().hex)


def _run(args, cwd=None, timeout=90):
    env = os.environ.copy()
    env['GIT_TERMINAL_PROMPT'] = '0'
    env['GCM_INTERACTIVE'] = 'Never'
    try:
        result = subprocess.run(['git', *map(str, args)], cwd=cwd, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceError('Git 不可用或操作超时（未回退到原项目）。') from exc
    if result.returncode:
        # 远端错误可能包含凭据或地址，不写入报告。
        raise SourceError('Git 操作失败，请检查仓库、远端、网络和认证（未回退到原项目）。')
    return result.stdout.decode('utf-8', 'replace').strip()


def project_names(workspace=WORKSPACE):
    text = (workspace / 'AGENTS.md').read_text(encoding='utf-8-sig')
    section = text.split('## 模块职责与任务入口', 1)[1].split('\n## ', 1)[0]
    names = re.findall(r'(?m)^\|\s*`([A-Za-z0-9][A-Za-z0-9_-]*)`\s*\|', section)
    if not names or len(names) != len(set(names)):
        raise SourceError('无法从架构说明获取唯一的项目清单。')
    return names


def source_name(repo, workspace=WORKSPACE, copies=COPIES):
    path = Path(repo).resolve()
    for root in (workspace.resolve(), copies.resolve()):
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if len(relative.parts) == 1:
            return relative.parts[0]
    return None


def _remote_identity(repo):
    try:
        url = _run(['-C', repo, 'remote', 'get-url', 'origin'])
    except SourceError:
        return None
    if '://' in url:
        parsed = urlsplit(url)
        if not parsed.hostname:
            return None
        value = parsed.hostname.lower() + '/' + parsed.path.strip('/')
    elif re.match(r'^[^/\\]+@[^:]+:', url):
        value = url.split('@', 1)[1].replace(':', '/', 1).rstrip('/')
    else:
        value = str(Path(url).resolve())
    return value[:-4] if value.endswith('.git') else value


def _matching_names(specs, names, workspace, copies):
    matches = [source_name(repo, workspace, copies) if repo is not None else None
               for repo, _, _, _ in specs]
    pending = [i for i, match in enumerate(matches) if match is None and specs[i][0] is not None]
    if not pending:
        return matches
    identities = {}
    for name in names:
        identity = _remote_identity(workspace / name)
        if identity:
            identities.setdefault(identity, []).append(name)
    for i in pending:
        identity = _remote_identity(specs[i][0])
        if identity and len(identities.get(identity, [])) == 1:
            matches[i] = identities[identity][0]
    return matches


def _copy(source, target):
    source = source.resolve()
    target = target.resolve()
    if target == source or source in target.parents or target in source.parents:
        raise SourceError('隔离目录与原仓库重叠。')
    top = Path(_run(['-C', source, 'rev-parse', '--show-toplevel'])).resolve()
    if top != source:
        raise SourceError('架构项目不是独立 Git 仓库。')
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        _run(['clone', '--no-checkout', '--no-hardlinks', '--', source, target], timeout=300)
        _run(['-C', target, 'config', '--local', 'diffanalysis.source', str(source)])
    else:
        try:
            saved = Path(_run(['-C', target, 'config', '--local', '--get', 'diffanalysis.source'])).resolve()
            top_copy = Path(_run(['-C', target, 'rev-parse', '--show-toplevel'])).resolve()
        except SourceError as exc:
            raise SourceError('隔离目录已存在但不属于本工具，未修改。') from exc
        if saved != source or top_copy != target:
            raise SourceError('隔离目录来源不匹配，未修改。')
    return target


def isolate_specs(specs, workspace=WORKSPACE, copies=COPIES):
    """本次指定的版本也从隔离 Git 副本读取。"""
    if not (workspace / 'AGENTS.md').is_file() and not any(
            source_name(repo, workspace, copies) for repo, _, _, _ in specs if repo is not None):
        return specs, [], False
    try:
        allowed = project_names(workspace)
    except (OSError, IndexError, SourceError) as exc:
        direct = [bool(source_name(repo, workspace, copies)) if repo is not None else False
                  for repo, _, _, _ in specs]
        if not any(direct):
            return specs, [], False
        retained = [spec for spec, related in zip(specs, direct) if not related]
        failures = [{'project': project, 'base': base, 'head': head, 'stage': 'ISOLATE',
                     'error_type': 'SourceError', 'reason': '无法读取架构项目清单：' + str(exc)[:200]}
                    for (_, project, base, head), related in zip(specs, direct) if related]
        return retained, failures, True
    names = _matching_names(specs, allowed, workspace, copies)
    if not any(names):
        return specs, [], False
    result, failures = [], []
    for (repo, project, base, head), name in zip(specs, names):
        if not name:
            result.append((repo, project, base, head))
            continue
        try:
            if name not in allowed:
                raise SourceError('项目不在架构服务清单中。')
            source = workspace / name
            target = _copy(source, copies / name)
            for sha in {base, head}:
                if not re.fullmatch(r'[a-fA-F0-9]{40,64}', sha):
                    raise SourceError('仅支持固定完整 SHA。')
                if Path(repo).resolve() != target:
                    _run(['-C', target, 'fetch', '--no-tags', '--no-write-fetch-head', '--no-recurse-submodules',
                          str(repo), '+' + sha + ':refs/diff-analysis/inputs/' + sha.lower()])
                else:
                    _run(['-C', target, 'cat-file', '-e', sha + '^{commit}'])
            result.append((target, project, base, head))
        except (OSError, SourceError) as exc:
            failures.append({'project': project, 'base': base, 'head': head,
                             'stage': 'ISOLATE', 'error_type': type(exc).__name__, 'reason': str(exc)[:300]})
    return result, failures, True


def default_projects(specs, workspace=WORKSPACE, copies=COPIES):
    if not (workspace / 'AGENTS.md').is_file():
        return [], []
    try:
        names = project_names(workspace)
    except (OSError, IndexError, SourceError) as exc:
        return [], ['无法读取架构项目清单：' + str(exc)[:200]]
    selected = set(_matching_names(specs, names, workspace, copies))
    def scan(name):
        if name in selected:
            return None, None
        source, target = workspace / name, copies / name
        try:
            target = _copy(source, target)
            url = _run(['-C', source, 'remote', 'get-url', 'origin'])
            if not url or '\n' in url or url.startswith('-'):
                raise SourceError('远端地址不可用。')
            parsed = urlsplit(url) if '://' in url else None
            if parsed and (parsed.password is not None or (parsed.scheme.lower() in ('http', 'https') and parsed.username is not None)):
                raise SourceError('远端地址内含认证信息，请使用 Git 凭据管理器。')
            listing = _run(['ls-remote', '--symref', '--', url, 'HEAD'])
            match = re.search(r'(?m)^ref:\s+(refs/heads/[^\s]+)\s+HEAD$', listing)
            if not match:
                raise SourceError('无法确定远端默认分支。')
            branch = match.group(1)
            _run(['check-ref-format', branch])
            dest = 'refs/remotes/diff-analysis/default'
            _run(['-C', target, 'fetch', '--no-tags', '--no-write-fetch-head', '--no-recurse-submodules',
                  '--', url, '+' + branch + ':' + dest], timeout=180)
            sha = _run(['-C', target, 'rev-parse', '--verify', dest + '^{commit}'])
            return {'project': 'architecture/' + name, 'repo': target, 'base': None,
                    'head': sha, 'source': '远端默认分支', 'branch': branch.removeprefix('refs/heads/')}, None
        except (OSError, SourceError) as exc:
            return None, name + '：默认分支扫描准备失败：' + str(exc)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(scan, names))
    return [x for x, _ in results if x], [e for _, e in results if e]
