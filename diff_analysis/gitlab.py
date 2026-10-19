"""Read GitLab MR metadata; authenticate Git over HTTPS on Windows and POSIX."""
from contextlib import contextmanager
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, quote, unquote
from urllib.request import Request, urlopen
from .gitops import check_sha, GitError


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


@contextmanager
def git_auth_environment(token):
    """Temporary platform-specific askpass launcher. Token is *only* in child env.

    Do not put the token in repository URLs, git command arguments or helper files.
    Git for Windows can invoke the .cmd launcher; POSIX uses a /bin/sh launcher.
    Both delegate the actual response to one UTF-8 Python program.
    """
    if not token:
        raise GitLabError("Empty GitLab token")
    with tempfile.TemporaryDirectory(prefix="mr-askpass-") as d:
        directory = Path(d)
        helper = directory / "askpass.py"
        helper.write_text(
            'import os, sys\n'
            'prompt = " ".join(sys.argv[1:]).lower()\n'
            'sys.stdout.write("oauth2" if "username" in prompt else '
            'os.environ["DIFF_ANALYSIS_GIT_TOKEN"])\n',
            encoding="utf-8")
        if os.name == "nt":
            launcher = directory / "askpass.cmd"
            launcher.write_text(
                '@echo off\r\n'
                '"%DIFF_ANALYSIS_PYTHON%" "%~dp0askpass.py" %*\r\n',
                encoding="ascii")
        else:
            launcher = directory / "askpass.sh"
            launcher.write_text(
                '#!/bin/sh\n'
                'exec "$DIFF_ANALYSIS_PYTHON" "$(dirname "$0")/askpass.py" "$@"\n',
                encoding="ascii")
            launcher.chmod(0o700)
        env = os.environ.copy()
        env.update({
            "GIT_ASKPASS": str(launcher),
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "DIFF_ANALYSIS_PYTHON": sys.executable,
            "DIFF_ANALYSIS_GIT_TOKEN": token,
        })
        yield env


def clone_mr(mr, dest, token):
    # Credentials are never embedded in the clone URL or process arguments.
    url = mr["origin"] + "/" + quote(mr["project"], safe="/-_.~") + ".git"
    dest = Path(dest)
    with git_auth_environment(token) as env:
        # Override any machine-level credential helper for this invocation only.
        # Avoid GCM popup dialogs, persistent token writes or prompting in CI.
        p = subprocess.run(
            ["git", "-c", "credential.helper=", "clone", "--quiet",
             "--no-checkout", "--no-tags", url, str(dest)],
            capture_output=True, env=env)
        if p.returncode:
            raise GitLabError(
                "Cannot clone GitLab project " + mr["project"] +
                " (check Git for Windows, read_repository permission, TLS and proxy)")
        for sha in (mr["head"], mr["base"]):
            try:
                check_sha(dest, sha)
            except GitError:
                p = subprocess.run(
                    ["git", "-c", "credential.helper=", "-C", str(dest),
                     "fetch", "--quiet", "--no-tags", "origin", sha],
                    capture_output=True, env=env)
                if p.returncode:
                    raise GitLabError(
                        "Pinned commit unavailable (fork MR may need source-project access): "
                        + sha[:12])
                check_sha(dest, sha)
    return dest
