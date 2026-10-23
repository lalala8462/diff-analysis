"""Conservative Extract Method behavior-risk assessment from pinned Git objects.

We compare concrete old source method and extracted new method *per commit*
rather than treating extracted method as behavior-preserving. Exact tokens only
show that the extracted body existed textually before; no semantic equivalence.
"""
import re

from .gitops import git

TOKEN = re.compile(
    r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|'
    r'[A-Za-z_$][\w$]*|\d+(?:\.\d+)?|==|!=|<=|>=|&&|\|\||'
    r'\+\+|--|->|[^\s]', re.S)
CONTROL = {"if", "else", "switch", "case", "throw", "catch", "finally",
           "while", "for", "synchronized"}
OPERATORS = {"==", "!=", "<", ">", "<=", ">=", "&&", "||",
             "+", "-", "*", "/", "%", "!", "?", ":", "++", "--"}


def _method_text(repo, sha, location, max_lines=300, max_bytes=50000):
    if not sha or not location or location["end"] - location["start"] > max_lines:
        return None
    try:
        # Git object lookups never execute checked-out project code.
        blob = git(repo, "show", sha + ":" + location["path"])
        if len(blob) > 2_000_000 or b"\x00" in blob:
            return None
        lines = blob.decode("utf-8", "replace").splitlines()
        if location["start"] > len(lines) or location["end"] > len(lines):
            return None
        portion = "\n".join(lines[location["start"] - 1:location["end"]])
        return portion if len(portion) <= max_bytes else None
    except (OSError, RuntimeError):
        return None


def _body(value):
    if not value:
        return None
    start = value.find("{")
    if start < 0:
        return None
    depth, quoted, escaped = 0, None, False
    for i in range(start, len(value)):
        char = value[i]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quoted:
                quoted = None
        elif char in "'\"":
            quoted = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return value[start + 1:i]
    return None


def _tokens(value):
    return [m.group() for m in TOKEN.finditer(value or "")
            if not (m.group().startswith("//") or m.group().startswith("/*"))]


def _contains(haystack, needle):
    if not needle:
        return False
    if len(needle) > len(haystack):
        return False
    return any(haystack[i:i + len(needle)] == needle
               for i in range(len(haystack) - len(needle) + 1))



def _conditions(tokens):
    """Extract complete control expression tokens, not a semantic AST."""
    found = []
    for i, token in enumerate(tokens):
        if token not in ("if", "while", "switch", "for", "catch"):
            continue
        if i + 1 >= len(tokens) or tokens[i + 1] != "(":
            continue
        depth, expr = 0, []
        for t in tokens[i + 1:]:
            expr.append(t)
            if t == "(":
                depth += 1
            elif t == ")":
                depth -= 1
                if depth == 0:
                    found.append(token + " " + " ".join(expr))
                    break
    return sorted(set(found))


def _find_caller_body(repo, commit, path, element):
    """Best-effort single-candidate method lookup; refuse overload ambiguities."""
    if not element:
        return None
    names = re.findall(r"([A-Za-z_$][\w$]*)\s*\(", element)
    if not names:
        return None
    try:
        blob = git(repo, "show", commit + ":" + path)
        if len(blob) > 2_000_000 or b"\x00" in blob:
            return None
        text = blob.decode("utf-8", "replace")
    except (OSError, RuntimeError):
        return None
    pattern = re.compile(
        r"\b" + re.escape(names[-1]) +
        r"\s*\([^()]*\)\s*(?:throws\s+[\w.,\s]+)?\{")
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        return None
    return _body(text[matches[0].start():])


def assess_extract_event(repo, event):
    """Assess a single step; do not compare intermediary lines to base/head."""
    if event.get("kind") not in ("Extract Method", "Extract And Move Method"):
        return None
    commit = event.get("commit_sha")
    if not commit:
        return {"status": "UNKNOWN", "reason": "No commit SHA to inspect"}
    try:
        parents = git(repo, "rev-list", "--parents", "-n", "1", commit).decode().split()
    except (OSError, RuntimeError):
        parents = []
    if len(parents) != 2:
        return {"status": "UNKNOWN", "reason": "No unique parent commit (merge/root)"}
    old_text = _method_text(repo, parents[1], event.get("old"))
    new_text = _method_text(repo, commit, event.get("new"))
    old_body, extracted_body = _body(old_text), _body(new_text)
    if not old_body or extracted_body is None:
        return {"status": "UNKNOWN",
                "reason": "Java method body unavailable or outside supported bounds",
                "step_base": parents[1], "step_head": commit}
    old_tokens, extracted_tokens = _tokens(old_body), _tokens(extracted_body)
    if not old_tokens or not extracted_tokens:
        return {"status": "UNKNOWN", "reason": "Empty source/extracted method tokens",
                "step_base": parents[1], "step_head": commit}
    body_found = _contains(old_tokens, extracted_tokens)
    introduced = sorted({
        t for t in extracted_tokens if t not in old_tokens
        and (t in OPERATORS or t in CONTROL
             or t[:1].isdigit() or t[:1] in ("'", '"'))
    })
    new_caller_body = _find_caller_body(
        repo, commit, event["old"]["path"], event["old"].get("element"))
    old_conditions = _conditions(old_tokens)
    new_conditions = (_conditions(_tokens(new_caller_body)) +
                      _conditions(extracted_tokens)) if new_caller_body is not None else []
    changed_conditions = (
        sorted(set(old_conditions) ^ set(new_conditions))
        if new_caller_body is not None else [])
    if introduced or changed_conditions:
        status, reason = ("POSSIBLE_MIXED_BEHAVIOR",
                          "Extracted logic or surrounding caller control flow differs from original")
    elif body_found:
        status, reason = ("TEXTUAL_EXTRACTION_CANDIDATE",
                          "Extracted statement tokens occur verbatim in old method")
    else:
        status, reason = ("UNVERIFIED_EXTRACTION",
                          "Could not establish textual equivalence (parameterization may explain it)")
    return {
        "status": status, "reason": reason,
        "step_base": parents[1], "step_head": commit,
        "extracted_body_found_verbatim": body_found,
        "new_risk_tokens": introduced[:20],
        "caller_control_flow_diff": changed_conditions[:20],
        "caller_inspected": new_caller_body is not None,
        "limitation": "Does not prove behavior equivalence, purity or call-site correctness"
    }


def assess_extractions(repo, events, max_checks=150):
    count = 0
    warnings = []
    for event in events:
        if event.get("kind") not in ("Extract Method", "Extract And Move Method"):
            continue
        if count >= max_checks:
            warnings.append({"type": "EXTRACTION_BUDGET_EXCEEDED", "max": max_checks})
            break
        event["behavior_assessment"] = assess_extract_event(repo, event)
        count += 1
    return warnings
