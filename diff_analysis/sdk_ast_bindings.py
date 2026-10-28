"""JDK 8 javac SDK consumer binding against immutable producer source snapshots.

Never compile business bytecode, invoke annotation processors or run business code.
An absent/incomplete classpath yields explicit omissions, not proof of no impact.
"""
import hashlib
import json
import os
import posixpath
import re
import subprocess
import tempfile
from pathlib import Path

from .gitops import git, safe_source_path
from .scanner import java_toolchain, ScannerError
from .source_index import source_snapshot_index

MAX_SOURCE_FILES = 1200
MAX_SDK_FILES = 250
MAX_SOURCE_BYTES = 10_000_000
MAX_BLOB = 300_000
RUN_TIMEOUT = 100
VERSION = "jdk8-sdk-source-bindings-v2"


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def _source_path(path):
    for prefix in ("src/main/java/", "src/test/java/"):
        if prefix in path:
            return path.rsplit(prefix, 1)[-1]
    return path


def _materialize(repo, sha, files, root, limits, gaps):
    count, total = 0, 0
    seen = set()
    for meta in files:
        path = meta["path"]
        if not safe_source_path(path, windows=True):
            gaps.append({"path": path, "reason": "AST_UNSAFE_PATH"})
            continue
        output = _source_path(path)
        if output in seen:
            gaps.append({"path": path, "reason": "AST_DUPLICATE_SOURCE_PATH"})
            continue
        if count >= limits:
            gaps.append({"reason": "AST_FILE_BUDGET_EXHAUSTED", "limit": limits})
            break
        try:
            size = int(git(repo, "cat-file", "-s", meta["blob_sha"]).strip())
            if size > MAX_BLOB or size + total > MAX_SOURCE_BYTES:
                gaps.append({"path": path, "reason": "AST_BYTES_BUDGET_EXHAUSTED"})
                continue
            raw = git(repo, "cat-file", "blob", meta["blob_sha"])
            if b"\x00" in raw:
                gaps.append({"path": path, "reason": "AST_BINARY_SOURCE"})
                continue
            destination = root / output
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
            count += 1
            total += len(raw)
            seen.add(output)
        except (ValueError, OSError, RuntimeError) as exc:
            gaps.append({"path": path, "reason": "AST_SOURCE_MISSING",
                         "error_type": type(exc).__name__})
    return count


def _method_arity(parameters):
    if not parameters.strip():
        return 0
    depth, count = 0, 1
    for char in parameters:
        if char == "<":
            depth += 1
        elif char == ">":
            depth -= 1
        elif char == "," and depth == 0:
            count += 1
    return count if depth == 0 else -1


def _matches(event, contract):
    if event.get("owner") != contract["type"]:
        return False
    kind, _, member = contract["identity"].partition(":")
    name = member.split("/")[0] if kind == "METHOD" else member
    if event.get("member") != name:
        return False
    if kind == "METHOD":
        if event.get("member_kind") != "METHOD":
            return False
        return _method_arity(event.get("parameters", "")) == int(member.rsplit("/", 1)[-1])
    return event.get("member_kind") == ("ENUM_CONSTANT" if kind == "ENUM" else "FIELD")


def _build_scanner(cache):
    javac, java, tools = java_toolchain()
    source = Path(__file__).resolve().parent / "java" / "SdkBindingScanner.java"
    if not source.is_file():
        raise ScannerError("JDK 8 SDK binding scanner source unavailable")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    target = Path(cache) / digest
    compiled = target / "SdkBindingScanner.class"
    if not compiled.is_file():
        target.mkdir(parents=True, exist_ok=True)
        argv = [javac, "-encoding", "UTF-8", "-source", "8", "-target", "8"]
        if tools is not None:
            argv += ["-classpath", str(tools)]
        argv += ["-d", str(target), str(source)]
        p = subprocess.run(argv, capture_output=True, text=True, errors="replace",
                           timeout=35)
        if p.returncode:
            raise ScannerError("SDK javac bridge compilation: " + p.stderr[:400])
    return java, target, tools


def attributed_uses(sdk, consumer, entries, snapshots, contracts, cache_dir):
    """Run attribution with BASE and HEAD SDK sources and pinned consumer HEAD."""
    if not contracts:
        return [], []
    gaps, findings = [], []
    try:
        java, compiled, tools = _build_scanner(Path(cache_dir) / "bridge")
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return [], [{"reason": "SDK_JAVAC_UNAVAILABLE", "detail": type(exc).__name__}]
    consumer_entry = entries[consumer]
    try:
        idx, _ = source_snapshot_index(
            consumer_entry["repo"], consumer, consumer_entry["head"],
            Path(cache_dir) / "indexes", extensions={".java"}, rules_version=VERSION)
    except (OSError, ValueError, RuntimeError) as exc:
        return [], [{"reason": "SDK_AST_CONSUMER_INDEX_FAILED",
                     "detail": type(exc).__name__}]
    gaps.extend(idx.get("omissions", []))
    consumer_files = idx["files"]
    module = posixpath.dirname(sdk["module"])
    module_prefix = module + "/" if module else ""
    for side in ("base", "head"):
        sha = sdk.get(side + "_sha")
        if not sha:
            continue
        producer = entries[sdk["project"]]
        inventory = snapshots[sdk["project"]].get(side, {})
        matching = [path for path in inventory.get("names", [])
                    if path.startswith(module_prefix) and path.endswith(".java")]
        if not matching:
            gaps.append({"reason": "SDK_AST_SOURCE_NOT_INDEXED", "side": side})
            continue
        # Resolve blob ids directly against immutable tree rather than worktree.
        try:
            sdk_index, _ = source_snapshot_index(
                producer["repo"], sdk["project"], sha,
                Path(cache_dir) / "indexes", extensions={".java"}, rules_version=VERSION)
            sdk_files = [f for f in sdk_index["files"] if f["path"] in set(matching)]
            gaps.extend({"side": side, **gap} for gap in sdk_index.get("omissions", []))
        except (OSError, ValueError, RuntimeError) as exc:
            gaps.append({"reason": "SDK_AST_SOURCE_INDEX_FAILED",
                         "side": side, "detail": type(exc).__name__})
            continue
        with tempfile.TemporaryDirectory(prefix="sdk-ast-") as tmp:
            sdk_root, use_root = Path(tmp) / "sdk", Path(tmp) / "consumer"
            sdk_root.mkdir()
            use_root.mkdir()
            if not _materialize(producer["repo"], sha, sdk_files, sdk_root, MAX_SDK_FILES, gaps):
                continue
            if not _materialize(consumer_entry["repo"], consumer_entry["head"],
                                consumer_files, use_root, MAX_SOURCE_FILES, gaps):
                continue
            classpath = os.pathsep.join([str(compiled)] + ([str(tools)] if tools else []))
            consumer_paths = {_source_path(meta["path"]): meta["path"]
                              for meta in consumer_files}
            sdk_paths = {_source_path(meta["path"]): meta["path"]
                         for meta in sdk_files}
            for owner_set in [",".join(sorted(set(c["type"] for c in contracts)))]:
                # javac resolves exactly the owner in this SDK package; do not
                # accept same-name types from unrelated packages.
                argv = [java, "-Dfile.encoding=UTF-8", "-cp", classpath,
                        "SdkBindingScanner", str(sdk_root), str(use_root), owner_set]
                try:
                    p = subprocess.run(argv, capture_output=True, text=True,
                                       errors="replace", timeout=RUN_TIMEOUT)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    gaps.append({"reason": "SDK_AST_EXECUTION_FAILED", "side": side,
                                 "detail": type(exc).__name__})
                    continue
                if p.returncode:
                    gaps.append({"reason": "SDK_AST_JAVAC_FAILED", "side": side,
                                 "detail": p.stderr[-250:]})
                    continue
                declarations, consumer_events = [], []
                for line in p.stdout.splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        gaps.append({"reason": "SDK_AST_INVALID_EVENT"})
                        continue
                    if event.get("kind") == "gap":
                        gaps.append({"side": side, "reason": "SDK_AST_ATTRIBUTION_DIAGNOSTIC",
                                     "code": event.get("detail"), "line": event.get("line")})
                    elif event.get("kind") == "declaration":
                        event["original_path"] = sdk_paths.get(event["path"], event["path"])
                        declarations.append(event)
                    elif event.get("kind") == "binding":
                        event["original_path"] = consumer_paths.get(event["path"], event["path"])
                        consumer_events.append(event)
                # Match the changed declaration to its attributed SDK element.
                # This prevents same-arity overloads and shadow types being
                # incorrectly marked as exact javac bindings.
                for contract in contracts:
                    declaration = contract.get("old" if side == "base" else "new")
                    if not declaration:
                        continue
                    candidates = [
                        d for d in declarations
                        if d["original_path"] == contract["path"] and
                        _matches(d, contract) and
                        abs(d["line"] - declaration["line"]) <= 2]
                    if len(candidates) != 1:
                        gaps.append({"reason": "SDK_AST_DECLARATION_UNRESOLVED",
                                     "side": side, "path": contract["path"],
                                     "identity": contract["identity"],
                                     "candidates": len(candidates)})
                        continue
                    symbol = candidates[0]
                    for event in consumer_events:
                        if (event["owner"] != symbol["owner"] or
                                event["member"] != symbol["member"] or
                                event["member_kind"] != symbol["member_kind"] or
                                event.get("parameters", "") != symbol.get("parameters", "")):
                            continue
                        findings.append({
                            "contract": contract,
                            "consumer_path": event["original_path"],
                            "consumer_sha": consumer_entry["head"],
                            "line": event["line"],
                            "excerpt": event["owner"] + "." + event["member"],
                            "binding_status": "JAVAC_ATTRIBUTED_SDK_SOURCE",
                            "producer_side": side,
                            "binding_owner": event["owner"],
                            "binding_parameters": event.get("parameters", ""),
                            "source_binding": "PINNED_SOURCE_NOT_DEPLOYED_ARTIFACT"})
    seen, unique = set(), []
    for item in findings:
        key = (item["contract"]["path"], item["contract"]["identity"],
               item["consumer_path"], item["line"], item["producer_side"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique, gaps
