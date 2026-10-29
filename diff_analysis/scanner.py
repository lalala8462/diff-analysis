"""JDK javac AST scan; conservative call resolution with explicit unknowns."""
import json
import os
import re
import shutil
import subprocess
from collections import defaultdict, deque
from pathlib import Path


SCANNER_RULES = 'java-scopes-v2'


def compatible_source_scope(caller, callee):
    """源码方法只能使用所属编译范围，不把其他模块的同名实现视为绑定。"""
    if any(not isinstance(m.get('source_scope'), str) for m in (caller, callee)):
        raise ValueError('方法事实必须包含源码范围 source_scope')
    left, right = caller['source_scope'], callee['source_scope']
    return (left == right or
            (left.endswith('src/test/java') and
             right == left[:-len('test/java')] + 'main/java'))


class ScannerError(RuntimeError):
    pass


def _java_executable(name):
    """Use the same JDK for javac and java; prefer JAVA_HOME when valid."""
    home = os.environ.get("JAVA_HOME")
    if home:
        executable = Path(home) / "bin" / (name + (".exe" if os.name == "nt" else ""))
        if executable.is_file():
            return str(executable)
    return shutil.which(name) or name


def _tools_jar(javac_path):
    """Find JDK 8 compiler API, not on the user classpath by default."""
    candidates = []
    if os.environ.get("JAVA_HOME"):
        candidates.append(Path(os.environ["JAVA_HOME"]) / "lib" / "tools.jar")
    resolved = shutil.which(javac_path) or javac_path
    if Path(resolved).is_file():
        candidates.append(Path(resolved).resolve().parent.parent / "lib" / "tools.jar")
    return next((path for path in candidates if path.is_file()), None)


def java_toolchain():
    """Resolve matching Java commands and JDK 8's optional compiler jar."""
    javac = _java_executable("javac")
    java = _java_executable("java")
    try:
        p = subprocess.run([javac, "-version"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ScannerError("Cannot run javac; install a full JDK 8 and verify JAVA_HOME/PATH") from exc
    version = (p.stdout + " " + p.stderr).strip()
    match = re.search(r"\bjavac\s+(\d+)(?:\.(\d+))?", version)
    if p.returncode or not match:
        raise ScannerError("Unable to identify javac version: " + version[:200])
    major = int(match.group(2)) if match.group(1) == "1" else int(match.group(1))
    if major < 8:
        raise ScannerError("Java 8+ is required; JDK 8 is the supported target")
    tools = _tools_jar(javac) if major == 8 else None
    if major == 8 and tools is None:
        raise ScannerError(
            "JDK 8 javac detected, but lib/tools.jar was not found. "
            "Set JAVA_HOME to the full JDK 8 installation (not a JRE), "
            "or repair JAVA_HOME to point to an installed full JDK 8.")
    return javac, java, tools


def compile_scanner(cache, toolchain=None):
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parent / "java" / "JavaAstScanner.java"
    behavior_source = source.with_name("BehaviorFacts.java")
    target = cache / "JavaAstScanner.class"
    javac, _, tools = toolchain or java_toolchain()
    if (not target.exists() or not (cache / 'BehaviorFacts.class').exists()
            or target.stat().st_mtime < max(source.stat().st_mtime, behavior_source.stat().st_mtime)):
        command = [javac, "-J-Dfile.encoding=UTF-8", "-encoding", "UTF-8",
                   "-source", "8", "-target", "8"]
        if tools is not None:
            command += ["-classpath", str(tools)]
        command += ["-d", str(cache), str(source), str(behavior_source)]
        p = subprocess.run(command, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if p.returncode:
            raise ScannerError(
                "JDK javac compilation failed (" + str(javac) + "): " +
                p.stderr[:1400] + "\nVerify full JDK 8/tools.jar, java -version and javac -version.")
    return cache


def scan_tree(root, cache, classpath=None):
    toolchain = java_toolchain()
    cache = compile_scanner(cache, toolchain)
    _, java, tools = toolchain
    runtime_cp = str(cache) + (os.pathsep + str(tools) if tools else "")
    scanner_argv = [java, "-Dfile.encoding=UTF-8", "-cp", runtime_cp,
                    "JavaAstScanner", str(root)]
    if classpath:
        scanner_argv.append(classpath)
    p = subprocess.run(
        scanner_argv,
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode:
        raise ScannerError("Java parser failed: " + p.stderr[:1500])
    methods, calls, errors = {}, [], []
    duplicates = set()
    for line in p.stdout.splitlines():
        event = json.loads(line)
        if event["kind"] == "method":
            symbol = event['symbol']
            if symbol in methods or symbol in duplicates:
                first = methods.pop(symbol, None)
                duplicates.add(symbol)
                for item in ([first] if first else []) + [event]:
                    errors.append({'kind': 'error', 'path': item['path'], 'symbol': symbol,
                                   'reason': 'DUPLICATE_METHOD_SYMBOL',
                                   'error': 'DUPLICATE_METHOD_SYMBOL'})
            else:
                methods[symbol] = event
        elif event["kind"] == "call":
            calls.append(event)
        else:
            errors.append(event)
    if not methods and not errors and any(Path(root).rglob('*.java')):
        errors.append({'kind': 'error', 'path': '', 'reason': 'EMPTY_METHOD_FACTS',
                       'error': '存在 Java 源码但没有可用方法事实'})
    return methods, calls, errors


def scan_coverage(root, methods, errors, skipped=()):
    """方法事实和诊断共同构成可复用扫描结果，空字典不代表扫描成功。"""
    diagnostics = [{'reason': e.get('reason', 'JAVA_ATTRIBUTION_INCOMPLETE'), **e} for e in errors]
    diagnostics += [{'reason': 'SOURCE_SKIPPED', 'path': s.split(' (', 1)[0], 'detail': s}
                    for s in skipped]
    paths = {p.relative_to(root).as_posix() for p in Path(root).rglob('*.java')}
    skipped_paths = {s.split(' (', 1)[0] for s in skipped if s.split(' (', 1)[0].endswith('.java')}
    files = []
    for path in sorted(paths | skipped_paths):
        gaps = [d for d in diagnostics if not d.get('path') or d['path'] == path or
                path.startswith(d['path'].rstrip('/') + '/')]
        files.append({'path': path, 'status': 'PARTIAL' if gaps else 'COMPLETE', 'diagnostics': gaps})
    return {'rules_version': SCANNER_RULES,
            'status': 'PARTIAL' if diagnostics else 'COMPLETE',
            'java_file_count': len(paths), 'files': files,
            'method_count': len(methods), 'diagnostics': diagnostics}


def method_changes(old_methods, new_methods, hunks_by_path):
    """Determine changed method bodies/signatures by old/new modified ranges."""
    changed = set()
    for symbol in set(old_methods) | set(new_methods):
        old = old_methods.get(symbol)
        new = new_methods.get(symbol)
        if old is None or new is None:
            if any((old or new)["path"] == f for f in hunks_by_path):
                changed.add(symbol)
            continue
        path = new["path"]
        for h in hunks_by_path.get(path, []):
            a1 = h["new_start"]; a2 = a1 + max(1, h["new_count"]) - 1
            b1 = h["old_start"]; b2 = b1 + max(1, h["old_count"]) - 1
            if ((a1 <= new["end"] and a2 >= new["start"])
                    or (b1 <= old["end"] and b2 >= old["start"])):
                changed.add(symbol)
    return changed


def build_graph(methods, calls):
    """Returns reverse graph; ambiguous names remain unresolved, not fabricated edges."""
    by_class_name = defaultdict(list)
    by_class_simple = defaultdict(list)
    by_binding = defaultdict(list)
    by_name = defaultdict(list)
    for key, m in methods.items():
        if m.get("binding_id"):
            by_binding[m["binding_id"]].append(key)
        by_class_name[(m["class"], m["name"])].append(key)
        by_class_simple[(m["class"].split(".")[-1].split("$")[-1], m["name"])].append(key)
        by_name[m["name"]].append(key)
    reverse = defaultdict(set)
    unresolved = []
    for call in calls:
        owner = call["owner"]
        if owner not in methods:
            continue
        recv = call["receiver"]
        typ = call["receiver_type"]
        name = call["name"]
        arity = call["arity"]
        target = call.get("target_binding_id")
        if target:
            potentials = by_binding[target]
        elif not recv or recv in ("this", "super"):
            potentials = by_class_name[(methods[owner]["class"], name)]
        elif typ:
            simple = re.sub(r"<.*", "", typ).split(".")[-1].split("$")[-1].strip()
            potentials = by_class_simple[(simple, name)]
        else:
            potentials = []
        candidates = [x for x in potentials if int(methods[x]["arity"]) == arity
                      and compatible_source_scope(methods[owner], methods[x])]
        if len(candidates) == 1:
            reverse[candidates[0]].add(owner)
        else:
            unresolved.append({"owner": owner, "name": name, "receiver": recv,
                               "arity": arity, "receiver_type": typ,
                               "path": call["path"], "line": call["line"],
                               "candidate_count": len(candidates),
                               "reason": ("ATTRIBUTED_TARGET_NOT_INDEXED" if target and not candidates
                                          else "AMBIGUOUS_CALL" if candidates else
                                          "STATIC_TARGET_NOT_FOUND")})
    return reverse, unresolved


def callers_of(changed, reverse, max_nodes=10000):
    hits, queue = {}, deque((x, [x]) for x in sorted(changed))
    limited = False
    while queue:
        item, path = queue.popleft()
        if item in hits:
            continue
        if len(hits) >= max_nodes:
            limited = True
            break
        hits[item] = path
        for caller in sorted(reverse.get(item, [])):
            if caller not in hits:
                queue.append((caller, path + [caller]))
    return hits, limited
