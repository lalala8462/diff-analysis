"""Read GitLab MR metadata with least-privilege API token; clone without token in URL."""
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, quote, unquote
from urllib.request import Request, urlopen
from .gitops import git, check_sha, GitError


class GitLabError(RuntimeError):
    pass


def parse_mr_url(raw, allowed_host, allow_http=False):
    url = urlsplit(raw)
    if (url.scheme != "https" and not (allow_http and url.scheme == "http")):
        raise GitLabError("MR URLs must use HTTPS (HTTP requires explicit opt-in)")
    if (url.hostname != allowed_host or url.username or url.password
            or url.query or url.fragment or not url.hostname):
        raise GitLabError("Untrusted GitLab MR URL/hostname")
    path = unquote(url.path)
    if any(x in ("..", ".") for x in path.split("/")):
        raise GitLabError("Invalid GitLab path")
    m = re.fullmatch(r"/(.+)/-/merge_requests/([1-9][0-9]*)/?", path)
    if not m:
        raise GitLabError("Expected URL ending with /-/merge_requests/<iid>")
    return (url.scheme + "://" + url.netloc, m.group(1), int(m.group(2)))


def gitlab_json(origin, api_path, token, timeout=30):
    req = Request(origin + "/api/v4/" + api_path,
                  headers={"PRIVATE-TOKEN": token, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception as e:
        raise GitLabError("GitLab API request failed: " + type(e).__name__) from e


def get_mr(url, host, token, allow_http=False):
    origin, path, iid = parse_mr_url(url, host, allow_http)
    encoded = quote(path, safe="")
    mr = gitlab_json(origin, "projects/" + encoded + "/merge_requests/" + str(iid), token)
    refs = mr.get("diff_refs") or {}
    head = refs.get("head_sha") or mr.get("sha")
    base = refs.get("base_sha")
    if not head or not base:
        raise GitLabError("MR does not expose pinned diff_refs head/base; refusing approximate diff")
    for sha in (head, base):
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha, re.I):
            raise GitLabError("Invalid MR commit SHA")
    return {"url": url, "origin": origin, "project": path, "iid": iid,
            "base": base, "head": head, "source_branch": mr.get("source_branch"),
            "target_branch": mr.get("target_branch"), "title": mr.get("title", "")}


def clone_mr(mr, dest, token):
    # Never embed token in clone URL or git CLI arguments.
    url = mr["origin"] + "/" + quote(mr["project"], safe="/-_.~") + ".git"
    dest = Path(dest)
    with tempfile.TemporaryDirectory(prefix="mr-askpass-") as d:
        askpass = Path(d) / "askpass.sh"
        askpass.write_text('#!/bin/sh\ncase "$1" in\n  *Username*) printf "oauth2" ;;\n  *) printf "%s" "$DIFF_ANALYSIS_GIT_TOKEN" ;;\nesac\n')
        askpass.chmod(0o700)
        env = os.environ.copy()
        env.update({"GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0",
                    "GIT_LFS_SKIP_SMUDGE": "1", "DIFF_ANALYSIS_GIT_TOKEN": token})
        p = subprocess.run(["git", "clone", "--quiet", "--no-checkout", "--no-tags", url, str(dest)],
                           capture_output=True, env=env)
        if p.returncode:
            raise GitLabError("Cannot clone GitLab project " + mr["project"])
        for sha in (mr["head"], mr["base"]):
            try:
                check_sha(dest, sha)
            except GitError:
                p = subprocess.run(["git", "-C", str(dest), "fetch", "--quiet", "--no-tags", "origin", sha],
                                   capture_output=True, env=env)
                if p.returncode:
                    raise GitLabError("Pinned commit unavailable (fork MR may need source-project access): " + sha[:12])
                check_sha(dest, sha)
    return dest
