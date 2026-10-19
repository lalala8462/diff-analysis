"""JDK javac AST scan; conservative call resolution with explicit unknowns."""
import json
import re
import subprocess
from collections import defaultdict, deque
from pathlib import Path


class ScannerError(RuntimeError):
    pass


def compile_scanner(cache):
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parent / "java" / "JavaAstScanner.java"
    target = cache / "JavaAstScanner.class"
    if not target.exists() or target.stat().st_mtime < source.stat().st_mtime:
        p = subprocess.run(
            ["javac", "-encoding", "UTF-8", "-source", "8", "-target", "8",
             "-d", str(cache), str(source)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if p.returncode:
            raise ScannerError("JDK javac compilation failed: " + p.stderr[:1000])
    return cache


def scan_tree(root, cache):
    cache = compile_scanner(cache)
    # Java 8-17 on Windows may default to the console code page rather than UTF-8.
    p = subprocess.run(
        ["java", "-Dfile.encoding=UTF-8", "-cp", str(cache),
         "JavaAstScanner", str(root)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode:
        raise ScannerError("Java parser failed: " + p.stderr[:1500])
    methods, calls, errors = {}, [], []
    for line in p.stdout.splitlines():
        event = json.loads(line)
        if event["kind"] == "method":
            methods[event["symbol"]] = event
        elif event["kind"] == "call":
            calls.append(event)
        else:
            errors.append(event)
    return methods, calls, errors


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
    by_name = defaultdict(list)
    for key, m in methods.items():
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
        if not recv or recv in ("this", "super"):
            potentials = by_class_name[(methods[owner]["class"], name)]
        elif typ:
            simple = re.sub(r"<.*", "", typ).split(".")[-1].split("$")[-1].strip()
            potentials = by_class_simple[(simple, name)]
        else:
            potentials = []
        candidates = [x for x in potentials if int(methods[x]["arity"]) == arity]
        if len(candidates) == 1:
            reverse[candidates[0]].add(owner)
        else:
            unresolved.append({"owner": owner, "name": name, "receiver": recv,
                               "arity": arity, "path": call["path"], "line": call["line"],
                               "candidate_count": len(candidates)})
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
