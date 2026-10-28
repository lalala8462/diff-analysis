"""Read-only fixed-SHA configuration identity evidence for database and Redis.

No network connections or credentials are used. Endpoint matches are source
configuration observations, never attestations of deployed runtime instances.
"""
import hashlib
import re
from collections import defaultdict
from pathlib import Path

from .gitops import git
from .source_index import source_snapshot_index

RULES_VERSION = "resource-instance-evidence-v1"
MAX_FILES = 300
MAX_BYTES = 3_000_000
MAX_BLOB = 100_000
CONFIG_SUFFIXES = {".properties", ".yml", ".yaml"}
PATTERN = re.compile(r"^\s*([\w.-]+)\s*[:=]\s*(.*?)\s*$")
DYNAMIC = re.compile(r"\$\{|\$\(|#\{")
JDBC = re.compile(
    r"^jdbc:(mysql|mariadb|postgresql)://([^/?#]+)(?:/([^?#]+))?|"
    r"^jdbc:oracle:thin:@(?://)?([^/:]+):(\d+)(?:[:/]([^?#]+))?", re.I)


def _fingerprint(kind, endpoint):
    return hashlib.sha256((kind + "\x00" + endpoint.lower()).encode("utf-8")).hexdigest()


def _profile(path):
    name = Path(path).name
    if name == "application.properties" or name in ("application.yml", "application.yaml"):
        return "default"
    match = re.match(r"application-([A-Za-z0-9_-]+)\.(?:properties|yml|yaml)$", name)
    return match.group(1) if match else None


def _config(text, path):
    result = []
    suffix = Path(path).suffix
    if suffix == ".properties":
        for line in text.splitlines():
            if not line or line.lstrip().startswith(("#", "!")):
                continue
            m = PATTERN.match(line)
            if m:
                result.append((m.group(1).lower(), m.group(2).strip().strip("\"'")))
    else:
        stack = []
        for line in text.splitlines():
            content = line.split("#", 1)[0].rstrip()
            match = re.match(r"^(\s*)([\w.-]+):(?:\s+(.*))?$", content)
            if not match:
                continue
            spaces, key, value = match.groups()
            indent = len(spaces.replace("\t", "    "))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            parent = ".".join(x[1] for x in stack)
            if value is not None and value.strip():
                result.append(((parent + "." if parent else "") + key.lower(),
                               value.strip().strip("\"'")))
            else:
                stack.append((indent, key.lower()))
    return result


def _jdbc_identity(raw):
    m = JDBC.match(raw)
    if not m:
        return None
    if m.group(1):
        return m.group(1).lower() + "://" + m.group(2).lower() + "/" + (m.group(3) or "").lower()
    return "oracle://" + m.group(4).lower() + ":" + m.group(5) + "/" + (m.group(6) or "").lower()


def _endpoints(pairs, profile, path):
    outcome, gaps = [], []
    values = defaultdict(list)
    for key, value in pairs:
        if value:
            values[key].append(value)
    for key, vals in values.items():
        if not (key.endswith((".url", ".jdbc-url")) and
                ("datasource" in key or "jdbc" in key)):
            continue
        for value in vals:
            if DYNAMIC.search(value):
                gaps.append({"path": path, "reason": "DYNAMIC_JDBC_ENDPOINT",
                             "profile": profile})
                continue
            identity = _jdbc_identity(value)
            if not identity:
                gaps.append({"path": path, "reason": "UNSUPPORTED_JDBC_ENDPOINT",
                             "profile": profile})
                continue
            match = re.search(r"\.dynamic\.datasource\.([\w-]+)\.(?:jdbc-)?url$", key)
            label = match.group(1) if match else "default"
            outcome.append({"kind": "DATABASE", "profile": profile,
                            "route": label, "identity_hash": _fingerprint("DATABASE", identity),
                            "source_path": path, "configuration_only": True})
    for prefix in ("spring.redis.", "spring.data.redis."):
        def single(name):
            items = values.get(prefix + name, [])
            return items[0] if len(items) == 1 else None
        host, port, database = single("host"), single("port"), single("database")
        sentinel_master = single("sentinel.master")
        sentinel_nodes = single("sentinel.nodes")
        if not host and sentinel_master and sentinel_nodes:
            if (database is None or any(
                    DYNAMIC.search(x) for x in
                    (sentinel_master, sentinel_nodes, database))):
                gaps.append({"path": path, "reason": "INCOMPLETE_SENTINEL_ENDPOINT",
                             "profile": profile})
                continue
            nodes = ",".join(sorted(x.strip().lower()
                                    for x in sentinel_nodes.split(",") if x.strip()))
            endpoint = "sentinel://" + sentinel_master.lower() + "@" + nodes + "/" + database
            outcome.append({"kind": "REDIS", "profile": profile, "route": "default",
                            "identity_hash": _fingerprint("REDIS", endpoint),
                            "source_path": path, "configuration_only": True,
                            "topology": "SENTINEL"})
            continue
        if not host:
            continue
        if any(DYNAMIC.search(x) for x in (host, port or "", database or "")):
            gaps.append({"path": path, "reason": "DYNAMIC_REDIS_ENDPOINT",
                         "profile": profile})
            continue
        if not port or database is None:
            gaps.append({"path": path, "reason": "INCOMPLETE_REDIS_ENDPOINT",
                         "profile": profile})
            continue
        identity = "redis://" + host.lower() + ":" + port + "/" + database
        outcome.append({"kind": "REDIS", "profile": profile, "route": "default",
                        "identity_hash": _fingerprint("REDIS", identity),
                        "source_path": path, "configuration_only": True})
    return outcome, gaps


def project_instances(repo, project, sha, cache_dir):
    index, _ = source_snapshot_index(repo, project, sha, cache_dir,
                                     extensions=CONFIG_SUFFIXES,
                                     rules_version=RULES_VERSION)
    items, gaps, total = [], list(index.get("omissions", [])), 0
    selected = [f for f in index["files"] if _profile(f["path"]) is not None]
    for i, file in enumerate(selected):
        if i >= MAX_FILES or total >= MAX_BYTES:
            gaps.append({"reason": "INSTANCE_CONFIG_SCAN_BUDGET"})
            break
        try:
            size = int(git(repo, "cat-file", "-s", file["blob_sha"]).strip())
            if size > MAX_BLOB or total + size > MAX_BYTES:
                gaps.append({"path": file["path"], "reason": "INSTANCE_CONFIG_TOO_LARGE"})
                continue
            text = git(repo, "cat-file", "blob", file["blob_sha"]).decode("utf-8", "replace")
            total += len(text.encode("utf-8"))
            found, problems = _endpoints(_config(text, file["path"]),
                                         _profile(file["path"]), file["path"])
            items.extend(found)
            gaps.extend(problems)
        except (OSError, RuntimeError, ValueError) as exc:
            gaps.append({"path": file["path"], "reason": "INSTANCE_CONFIG_READ_ERROR",
                         "type": type(exc).__name__})
    return {"project": project, "sha": sha, "endpoints": items, "omissions": gaps}


def _single(source, kind, hint):
    options = [x for x in source.get("endpoints", []) if x["kind"] == kind]
    if hint:
        options = [x for x in options if x["route"] == hint]
    if not options:
        return None, "NO_ENDPOINT_EVIDENCE"
    # Never compare different named application profiles.
    grouped = defaultdict(set)
    for x in options:
        grouped[x["profile"]].add(x["identity_hash"])
    if len(grouped) != 1:
        return None, "MULTIPLE_CONFIG_PROFILES"
    profile, values = next(iter(grouped.items()))
    if len(values) != 1:
        return None, "AMBIGUOUS_ENDPOINTS"
    return (profile, next(iter(values))), None


def attach_instance_evidence(entries, database, shared, cache_dir):
    """Enrich existing candidate records, without turning config into runtime proof."""
    projects, gaps = {}, []
    for project, item in sorted(entries.items()):
        try:
            projects[project] = project_instances(item["repo"], project, item["head"],
                                                 Path(cache_dir) / "indexes")
            gaps.extend({"project": project, **x}
                        for x in projects[project]["omissions"])
        except (ValueError, OSError, RuntimeError) as exc:
            projects[project] = {"endpoints": []}
            gaps.append({"project": project, "reason": "INSTANCE_INDEX_FAILED",
                         "type": type(exc).__name__})
    for result, kind, source_field, consumer_field in (
            (database, "DATABASE", "producer_uses", "consumer_uses"),
            (shared, "REDIS", "source_uses", "consumer_uses")):
        for row in result["impacts"]:
            if kind == "REDIS" and row["kind"] not in ("REDIS_KEY", "REDIS_VALUE_DTO"):
                continue
            producer = row["producer_project"]
            consumer = row["consumer_project"]
            a_hints = {x["datasource_hint"] for x in row[source_field]
                       if x.get("datasource_hint")}
            b_hints = {x["datasource_hint"] for x in row[consumer_field]
                       if x.get("datasource_hint")}
            if len(a_hints) > 1 or len(b_hints) > 1:
                row["instance_evidence"] = {"status": "AMBIGUOUS_SOURCE_ROUTES"}
                continue
            left, err_a = _single(projects[producer], kind,
                                  next(iter(a_hints), None))
            right, err_b = _single(projects[consumer], kind,
                                   next(iter(b_hints), None))
            if err_a or err_b:
                row["instance_evidence"] = {"status": "CONFIG_IDENTITY_UNKNOWN",
                                            "reasons": sorted(set(x for x in (err_a, err_b) if x))}
            elif left[0] != right[0]:
                row["instance_evidence"] = {"status": "PROFILE_MISMATCH_UNCONFIRMED",
                                            "producer_profile": left[0],
                                            "consumer_profile": right[0]}
            else:
                row["instance_evidence"] = {
                    "status": ("CONFIG_ENDPOINT_MATCH_NOT_DEPLOYMENT_PROOF"
                               if left[1] == right[1] else
                               "CONFIG_ENDPOINT_MISMATCH_UNCONFIRMED"),
                    "profile": left[0], "producer_hash": left[1][:16],
                    "consumer_hash": right[1][:16]}
    return {"schema_version": RULES_VERSION, "projects": len(projects),
            "omissions": gaps, "limitation": "Only explicit fixed-SHA config; "
            "no connection, environment, active profile, instance or routing attested"}
