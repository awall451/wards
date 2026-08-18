#!/usr/bin/env python3
"""Layer 5 — lint / format / suppression ward for Claude Code.

Wraps the project's OWN linter and formatter around every file the agent
edits, and enforces three things with the same session ratchet as
ward-complexity.py:

  1. Lint findings: a rule whose count in this file went UP since the agent
     first touched it this session is a NEW violation -> block (error-severity)
     or warn (warning-severity). Pre-existing findings are reported once and
     never block — the agent is not asked to clean up code it did not write.
  2. Formatting: the project's formatter is run on the file after every edit
     (format-on-save). If it changed anything the agent is told to re-read.
  3. Suppressions: `# noqa`, `# type: ignore`, `eslint-disable`, `@ts-ignore`,
     `#nosec`, `//nolint`, ... — an INCREASE in suppression markers is blocked.
     Fix the finding, don't silence it; if a suppression is genuinely right,
     stop and tell the human.

Tools are discovered, never imposed: a linter/formatter runs only if it is
installed (PATH, node_modules/.bin, .venv/bin) AND — for tools whose defaults
would be presumptuous (eslint, prettier, ruff format, shfmt, tflint) — the
project already carries its config. No tool for a language = nothing happens.

Defaults by language (override in .wards/config.toml, see below):

  python      ruff check              ruff format   (format needs ruff.toml/pyproject)
  js/ts       eslint (needs config)   prettier      (needs .prettierrc*)
  go          go vet (package)        gofmt
  shell       shellcheck              shfmt         (needs .editorconfig)
  terraform   tflint (needs config)   terraform fmt

Config (.wards/config.toml):
  [lint]
  enabled = true
  format = true                       # set false to never auto-format
  [lint.python]                       # per-language overrides; {file}/{dir} substituted
  check = "ruff check --output-format json {file}"
  format = "ruff format {file}"
  parser = "ruff"                     # ruff | eslint | shellcheck | tflint | generic
  [lint.javascript]
  check = "npx eslint -f json {file}"
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wardlib import Session, audit, block, config, in_project, rel, run_hook, warn_context
from wardtools import lang_of, tool_for

HOOK = "lint"
SET = {"cfg": {}, "enabled": True, "format": True}


def configure(anchor_path):
    """Load [lint] from the nearest .wards/config.toml above `anchor_path`."""
    SET["cfg"] = config(HOOK, anchor_path)
    SET["enabled"] = SET["cfg"].get("enabled", True)
    SET["format"] = SET["cfg"].get("format", True)


SUPPRESSION_RE = re.compile(
    r"#\s*(noqa|type:\s*ignore|pyright:\s*(ignore|basic|standard|strict)|pragma:\s*no\s*cover|nosec|"
    r"pylint:\s*(disable|skip-file)|shellcheck\s+disable|ruff:\s*noqa|fmt:\s*(off|skip)|tflint-ignore|"
    r"mypy:\s*ignore|nolint|flake8:\s*noqa|isort:\s*skip|bandit:\s*skip)"
    r"|//\s*(eslint-disable|@ts-ignore|@ts-expect-error|@ts-nocheck|prettier-ignore|nolint|biome-ignore|"
    r"NOSONAR|noinspection|deepcode\s+ignore|sonar-ignore|istanbul\s+ignore|c8\s+ignore|v8\s+ignore)"
    r"|/\*\s*(eslint-disable|istanbul\s+ignore|c8\s+ignore)"
    r"|@SuppressWarnings\("
    r"|#\[allow\("
    r"|#\s*noqa\b",
    re.IGNORECASE,
)


# ------------------------------------------------------------------ helpers --


def run(argv, cwd):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=90, cwd=cwd)
        return p.stdout, p.stderr
    except (subprocess.TimeoutExpired, OSError) as exc:
        audit(f"WARN: {HOOK}", f"{argv[0]} failed: {exc}")
        return "", str(exc)


# ------------------------------------------------------------------ parsers --
# Every parser returns [(rule, line, severity, message)], severity in {error, warning}.


def _json(text, default):
    try:
        return json.loads(text) if text.strip() else default
    except json.JSONDecodeError:
        return default


def parse_ruff(out, _path):
    return [(f.get("code") or "syntax", f["location"]["row"], "error", f["message"]) for f in _json(out, [])]


def parse_eslint(out, _path):
    found = []
    for f in _json(out, []):
        for m in f.get("messages", []):
            if m.get("ruleId") is None and not m.get("fatal"):
                continue  # informational ("File ignored because …"), not a finding
            sev = "error" if m.get("severity") == 2 or m.get("fatal") else "warning"
            found.append((m.get("ruleId") or "parse", m.get("line") or 0, sev, m.get("message", "")))
    return found


def parse_shellcheck(out, _path):
    # shellcheck "warning" is advisory (SC2034 unused var in a sourced lib, …): surface, don't block
    sev = {"error": "error", "warning": "warning", "info": "warning", "style": "warning"}
    return [
        (f"SC{f['code']}", f["line"], sev.get(f["level"], "warning"), f["message"]) for f in _json(out, [])
    ]


def parse_tflint(out, path):
    base = os.path.basename(path)
    return [
        (
            i["rule"]["name"],
            i["range"]["start"]["line"],
            "error" if i["rule"]["severity"] == "error" else "warning",
            i["message"],
        )
        for i in _json(out, {}).get("issues", [])
        if i["range"]["filename"].endswith(base)
    ]


GENERIC_RE = re.compile(
    r"^(?:vet:\s*|[a-z]+:\s+(?=\S+\.\w+[:(]\d))?"  # go vet prefixes "vet: ./x.go:1:2: msg"
    r"(?P<file>[^:\s]+):(?P<line>\d+)(?::\d+)?:?\s*(?:-\s*)?"
    r"(?:(?P<sev>error|warning|note|info)\s*:?\s*)?"
    r"(?:(?P<code>[A-Z]{1,6}\d{2,5}|[a-z][\w-]*/[\w-]+)\s*:?\s+)?"
    r"(?P<msg>.+)$"
)


def parse_generic(out, path):
    base = os.path.basename(path)
    found = []
    for ln in out.splitlines():
        m = GENERIC_RE.match(ln.strip())
        if not m or not m.group("file").endswith(base) or (m.group("sev") or "").lower() in ("note", "info"):
            continue
        sev = "warning" if (m.group("sev") or "").lower() == "warning" else "error"
        found.append((m.group("code") or "lint", int(m.group("line")), sev, m.group("msg")))
    return found


PARSERS = {
    "ruff": parse_ruff,
    "eslint": parse_eslint,
    "shellcheck": parse_shellcheck,
    "tflint": parse_tflint,
    "generic": parse_generic,
}


# ------------------------------------------------------------------ measure --


def _in_text(line, pos):
    """Heuristic: marker sits inside a string/docstring/markdown rather than as a
    live directive — an odd number of quotes or a backtick precedes it, or the line
    is a docstring/comment-prose line. Keeps docs *about* suppressions from counting."""
    before = line[:pos]
    if "`" in before or before.count('"') % 2 or before.count("'") % 2:
        return True
    return line.lstrip().startswith(('"""', "'''", "* ", "- ", "| "))


def suppressions(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return sum(1 for ln in fh if (m := SUPPRESSION_RE.search(ln)) and not _in_text(ln, m.start()))
    except OSError:
        return 0


def measure(path):
    """{"findings": [(rule, line, severity, msg)], "suppressions": n}, or None if not lintable."""
    lang = lang_of(path)
    if not lang or not os.path.isfile(path):
        return None
    findings = []
    tool = tool_for(lang, "check", path, SET["cfg"].get(lang, {}))
    if tool:
        argv, parser = tool
        out, err = run(argv, os.path.dirname(path))
        findings = [f for f in PARSERS[parser](out or err, path) if not _idiomatic(path, f)]
        if not findings and err.strip() and not out.strip():
            audit(f"WARN: {HOOK}", f"{argv[0]} produced no findings but wrote stderr: {err.strip()[:200]}")
    return {"findings": findings, "suppressions": suppressions(path)}


def fingerprint(f):
    """rule|message — line numbers dropped so edits elsewhere don't make an old
    finding look new, but a *different* instance of the same rule still does."""
    return f"{f[0]}|{f[3]}"


def _idiomatic(path, finding):
    """Findings the ecosystem itself considers idiomatic: F401 re-exports in __init__.py."""
    return finding[0] == "F401" and os.path.basename(path) == "__init__.py"


def snapshot(now):
    return {
        "prints": dict(Counter(fingerprint(f) for f in now["findings"])),
        "suppressions": now["suppressions"],
    }


def _digest(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def do_format(path):
    """Run the project formatter; True if the file changed."""
    lang = lang_of(path)
    tool = SET["format"] and tool_for(lang, "format", path, SET["cfg"].get(lang, {}))
    if not tool:
        return False
    before = _digest(path)
    run(tool[0], os.path.dirname(path))
    return _digest(path) != before


# ------------------------------------------------------------------- policy --


def judge_rules(path, base_prints, now):
    """Per rule: any finding whose (rule, message) fingerprint count exceeds the
    baseline is new -> block if error-severity, warn if warning-severity."""
    seen = Counter()
    fresh = {}
    for f in now["findings"]:
        fp = fingerprint(f)
        seen[fp] += 1
        if seen[fp] > base_prints.get(fp, 0):
            fresh.setdefault(f[0], []).append(f)
    for rule in sorted(Counter(f[0] for f in now["findings"])):
        key = f"{path}::{rule}"
        hits = fresh.get(rule)
        if not hits:
            yield "NOTE", key, f"{rel(path)} {rule} pre-existing"
            continue
        sev = "error" if any(f[2] == "error" for f in hits) else "warning"
        where = "; ".join(f"L{f[1]}: {f[3]}" for f in hits)[:400]
        yield (
            ("BLOCK" if sev == "error" else "WARN"),
            key,
            f"{rel(path)} {rule} +{len(hits)} new — {where}. Fix the finding; do not add a suppression.",
        )


def judge(path, base, now):
    """Yield (level, key, msg) for lint ratchet, suppressions, and legacy notice."""
    base_prints = Counter(base.get("prints", {}))
    yield from judge_rules(path, base_prints, now)
    base_sup = base.get("suppressions", 0)
    if now["suppressions"] > base_sup:
        yield (
            "BLOCK",
            f"{path}::suppressions",
            (
                f"{rel(path)} — suppression markers went {base_sup} → {now['suppressions']} "
                f"(noqa / type: ignore / eslint-disable / nolint …). Fix the underlying finding instead. "
                f"If a suppression is genuinely correct, stop and tell the human why; do not add it yourself."
            ),
        )
    legacy = sum(base_prints.values())
    if legacy:
        rules = sorted({fp.split("|", 1)[0] for fp in base_prints})
        yield (
            "WARN",
            f"{path}::legacy",
            (
                f"{rel(path)} has {legacy} pre-existing lint finding(s) ({', '.join(rules)}). "
                f"Not yours to clean unasked; don't add more. Mention it to the human."
            ),
        )


def check(session, paths, fmt=False):
    blocks, warns = [], []
    for path in paths:
        if fmt and do_format(path):
            warns.append(
                f"{rel(path)} was auto-formatted by the project formatter — re-read before editing again."
            )
            audit(f"NOTE: {HOOK}", f"formatted {path}")
        now = measure(path)
        if now is None:
            continue
        for level, key, msg in judge(path, session.baseline.get(path, {}), now):
            if level == "BLOCK":
                blocks.append(msg)
            elif level == "WARN" and session.warn_once(key):
                warns.append(msg)
            audit(f"{level}: {HOOK}", msg)
    session.save()
    return blocks, warns


# ------------------------------------------------------------------- events --


def _target(payload):
    path = payload.get("tool_input", {}).get("file_path")
    return path if path and SET["enabled"] and lang_of(path) and in_project(path) else None


def on_pre(payload):
    path = _target(payload)
    if not path:
        return
    session = Session(payload["session_id"], HOOK)
    if path in session.baseline:
        return
    now = measure(path)
    session.baseline[path] = snapshot(now) if now else {"prints": {}, "suppressions": 0}
    session.save()


SYNTAX_RULES = {"syntax", "parse", "invalid-syntax", "E999", "SC1000", "SC1009", "SC1073", "SC1072"}


def _structural(msg):
    """Blocks that are never a legitimate transient state: added suppressions and syntax errors."""
    return "suppression markers went" in msg or any(f" {r} " in f" {msg} " for r in SYNTAX_RULES)


def on_post(payload):
    path = _target(payload)
    if not path:
        return
    session = Session(payload["session_id"], HOOK)
    blocks, warns = check(session, [path], fmt=True)
    # Mid-refactor states (import added before its use, call before its definition) are
    # normal between edits: report them now, enforce them at Stop. Suppressions and syntax
    # errors are never transient — those block immediately.
    soft = [b for b in blocks if not _structural(b)]
    blocks = [b for b in blocks if _structural(b)]
    if soft:
        warns = [f"(will block at Stop) {b}" for b in soft] + warns
    if blocks:
        block(
            "WARD BLOCK (lint). The edit was applied but introduced lint violations; fix them before "
            "doing anything else. Pre-existing findings are not your job unless asked.\n"
            + "\n".join(f"- {b}" for b in blocks)
            + ("\n\nAlso note:\n" + "\n".join(f"- {w}" for w in warns) if warns else "")
        )
    warn_context("PostToolUse", "WARD (lint):", warns)


def on_stop(payload):
    session = Session(payload["session_id"], HOOK)
    if not SET["enabled"] or not session.touched():
        return
    blocks, _ = check(session, session.touched())
    if blocks and session.may_block_stop():
        block(
            "WARD BLOCK (lint) — files touched this session still carry new lint violations; "
            "resolve before finishing:\n" + "\n".join(f"- {b}" for b in blocks)
        )
    if not blocks:
        session.clear_stop_blocks()


if __name__ == "__main__":
    run_hook(HOOK, configure, (on_pre, on_post, on_stop))
