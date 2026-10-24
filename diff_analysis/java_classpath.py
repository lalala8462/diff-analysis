"""Opt-in, hash-pinned binary classpaths for Java symbol attribution.

Never downloads dependencies or builds the analyzed project. Each JAR is read
as local input and tied to an exact project/side/commit; configuration is not
a claim that binaries match production runtime.
"""
import hashlib
import json
import os
import re
from pathlib import Path


def _digest(path):
    h = hashlib.sha256()
    with path.open("rb") as fd:
        for chunk in iter(lambda: fd.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_classpaths(manifest_path):
    if not manifest_path:
        return {}
    path = Path(manifest_path).expanduser().resolve()
    if path.stat().st_size > 1_000_000:
        raise ValueError("Classpath manifest exceeds 1MB limit")
    value = json.loads(path.read_text("utf-8"))
    entries = value.get("projects")
    if value.get("schema_version") != "java-classpath-v1" or not isinstance(entries, list):
        raise ValueError("Invalid java-classpath-v1 manifest")
    result = {}
    for entry in entries:
        project = entry.get("project")
        base, head = entry.get("base_sha"), entry.get("head_sha")
        if (not isinstance(project, str) or
                not all(isinstance(x, str) and re.fullmatch(r"[a-fA-F0-9]{40,64}", x)
                        for x in (base, head))):
            raise ValueError("Java classpath requires project and exact pinned SHA")
        for side, sha in (("BASE", base), ("HEAD", head)):
            jars = entry.get(side.lower())
            if not isinstance(jars, list) or len(jars) > 512:
                raise ValueError("Java classpath entries limited to 512 pinned JARs per side")
            files = []
            for jar in jars:
                if not isinstance(jar, dict):
                    raise ValueError("Classpath JAR must be object")
                uri, expected = jar.get("path"), jar.get("sha256")
                if (not isinstance(uri, str) or
                        not isinstance(expected, str) or
                        not re.fullmatch(r"[a-fA-F0-9]{64}", expected)):
                    raise ValueError("Classpath JAR must have path and SHA-256")
                candidate = Path(uri).expanduser().resolve()
                if (not candidate.is_file() or candidate.suffix.lower() != ".jar"
                        or candidate.stat().st_size > 1_000_000_000):
                    raise ValueError("Classpath entry is not an allowed local JAR")
                if _digest(candidate).lower() != expected.lower():
                    raise ValueError("Classpath JAR changed: " + candidate.name)
                files.append(str(candidate))
            key = (project, side, sha.lower())
            if key in result:
                raise ValueError("Duplicate project/side/SHA classpath declaration")
            fingerprint = hashlib.sha256(json.dumps(
                [sha.lower(), [str(Path(p).name) for p in files],
                 [j["sha256"].lower() for j in jars]],
                separators=(",", ":")).encode("utf-8")).hexdigest()
            result[key] = {"classpath": os.pathsep.join(files),
                           "fingerprint": fingerprint,
                           "jar_count": len(files), "status": "USER_SUPPLIED_HASH_PINNED"}
    return result


def for_project(classpaths, project, base, head):
    return {
        "BASE": classpaths.get((project, "BASE", base.lower())),
        "HEAD": classpaths.get((project, "HEAD", head.lower()))}
