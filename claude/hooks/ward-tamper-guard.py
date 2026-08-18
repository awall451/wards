#!/usr/bin/env python3
"""Layer 5 — tamper guard for Claude Code (PreToolUse on Edit, Write, NotebookEdit and Bash).

"Enforcement lives below the agent" is void if the agent can edit the
enforcement. This blocks the agent from modifying its own wards and the
repo's local hook plumbing:

  .claude/settings.json / settings.local.json   (permissions + hook wiring)
  .claude/hooks/*                               (the hooks themselves)
  .wards/*                                      (config, allow-lists — the human's)
  ~/.claude/settings.json                       (user-level wiring)
  .git/hooks/, .git/config, .pre-commit-config.yaml, and `rm -rf .git`

File tools -> block on path. Bash -> any command segment that names a
protected path is allowed ONLY if it is a known read-only command with no
file redirection (cat, ls, grep, diff, jq, git diff/log/show/status, …);
everything else (editors, interpreters, rm/mv/cp/sed -i, `>`, xargs, find
-delete …) is blocked. Allow-listing reads is safer than deny-listing writes:
the red team found ed/ex/vim/awk -i/python script.py in minutes.

Honest limits: a shell string can hide a path (`d=".wa""rds"; … $d/…`,
`$(printf …)`). Quote-splitting is normalised; variable indirection is not
solvable by inspection. So the guard also FINGERPRINTS .wards/config.toml when
a session first reads it and audit-logs (and tells the agent) if it changes
mid-session — tampering that slips past becomes visible instead of silent.
Layer 4 (`permissions.deny` `Edit(./.claude/**)`, `Edit(./.wards/**)`) and
git review of `.wards/` diffs are the belt-and-braces; a filesystem sandbox is
the true below-agent layer.

Config (.wards/config.toml) — a human sets it, e.g. in the wards repo where
hooks are developed:
  [tamper]
  enabled = true
  extra_paths = ["infra/policies/"]     # more protected path fragments
"""

import os
import re
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wardlib import audit, block, config, config_fingerprint_changed, read_payload

HOOK = "tamper"
PROTECTED = [
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".claude/hooks/",
    ".wards/",
    ".git/hooks/",
    ".git/config",
    ".pre-commit-config.yaml",
]
HOME_SETTINGS_RE = re.compile(r"(~|\$HOME|\$\{HOME\}|/home/[^/\s]+|/Users/[^/\s]+)/\.claude/settings")
DOTGIT_RE = re.compile(r"(^|[\s/'\"])\.git([\s'\"]|$)")  # `rm -rf .git`, `mv .git …`
READ_ONLY = {
    "cat",
    "less",
    "more",
    "head",
    "tail",
    "grep",
    "egrep",
    "fgrep",
    "rg",
    "ag",
    "ls",
    "tree",
    "diff",
    "cmp",
    "jq",
    "yq",
    "wc",
    "file",
    "stat",
    "md5sum",
    "sha256sum",
    "sha1sum",
    "sort",
    "uniq",
    "cut",
    "tr",
    "column",
    "echo",
    "printf",
    "test",
    "[",
    "cd",
    "pwd",
    "realpath",
    "readlink",
    "basename",
    "dirname",
    "which",
    "type",
    "true",
    "false",
    "toml",
    "bat",
    "tomlq",
    "pre-commit",
}
# git subcommands that don't rewrite protected files in the working tree (staging/committing a
# human's hook change is fine and shows in the diff; checkout/restore/rm/mv/clean/stash pop do rewrite)
GIT_READ = {
    "diff",
    "log",
    "show",
    "status",
    "blame",
    "ls-files",
    "cat-file",
    "rev-parse",
    "grep",
    "add",
    "commit",
    "push",
    "fetch",
    "stash",
    "tag",
    "branch",
    "remote",
    "describe",
}
DANGEROUS_ANYWHERE = re.compile(
    r"\bpre-commit\s+uninstall\b|core\.hooksPath\s*=|config\b(?:\s+--?\w+)*\s+core\.hooksPath\s+\S", re.I
)
SEPARATORS = {";", "&", "&&", "|", "||", ";;", "|&"}
HEREDOC_RE = re.compile(
    r"(?P<open><<-?\s*['\"]?(?P<tag>\w+)['\"]?[^\n]*\n)(?P<body>.*?)\n\s*(?P=tag)\s*$", re.S | re.M
)


def configure(anchor_path):
    cfg = config(HOOK, anchor_path)
    return cfg.get("enabled", True), PROTECTED + [p for p in cfg.get("extra_paths", []) if p]


# ------------------------------------------------------------- file tools --


def norm(path):
    p = os.path.abspath(os.path.expanduser(path)).replace(os.sep, "/")
    home = os.path.expanduser("~").replace(os.sep, "/")
    return p.replace(home + "/.claude/settings", "/~/.claude/settings")


def protected_hit(path, protected):
    p = norm(path)
    return next((frag for frag in protected if f"/{frag}" in p or p.endswith(f"/{frag.rstrip('/')}")), None)


def check_file_tool(payload, protected):
    path = payload.get("tool_input", {}).get("file_path", "")
    hit = path and protected_hit(path, protected)
    return f"editing {hit} — the wards' own config/wiring is human-owned" if hit else None


# -------------------------------------------------------------------- bash --


def strip_heredocs(command):
    """Heredoc bodies are data (docs that *mention* .claude/hooks), not commands."""
    return HEREDOC_RE.sub(lambda m: m.group("open") + m.group("tag"), command)


def unquote_split(command):
    """Normalise quote-splitting tricks: `.wa""rds`, `.cla''ude`, `\\.wards` -> `.wards`."""
    return re.sub(r"\"\"|''|\\(?=[.\w/])", "", command)


def segments(command):
    """Token lists per shell segment (split on ; & | && ||), quotes respected."""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        toks = command.split()
    seg, out = [], []
    for t in [*toks, ";"]:
        if t in SEPARATORS:
            if seg:
                out.append(seg)
            seg = []
        else:
            seg.append(t)
    return out


def head_command(seg):
    """First real command word: skip env assignments, sudo/env/nice/time wrappers."""
    i = 0
    while i < len(seg) and (
        re.fullmatch(r"[A-Za-z_]\w*=.*", seg[i])
        or seg[i] in ("sudo", "env", "nice", "time", "command", "builtin")
    ):
        i += 1
    return seg[i:] or [""]


def redirects_to_file(seg):
    """True if the segment writes to a file: any > / >> whose target is not /dev/null or a fd."""
    for i, t in enumerate(seg):
        if t in (">", ">>", ">|", "&>", "&>>") or re.fullmatch(r"\d?>>?", t):
            target = seg[i + 1] if i + 1 < len(seg) else ""
            if not (target.startswith("&") or target == "/dev/null"):
                return True
    return False


def read_only(seg):
    """A segment is a read if its command is on the allow-list and it doesn't redirect to a file
    (find without -delete/-exec, awk/sed without in-place, git with a read subcommand)."""
    cmd = head_command(seg)
    name = os.path.basename(cmd[0])
    if redirects_to_file(seg):
        return False
    special = {
        "git": lambda: any(t in GIT_READ for t in cmd[1:3]) and not (cmd[1:2] == ["stash"] and "pop" in cmd),
        "find": lambda: not any(t in ("-delete", "-exec", "-execdir", "-ok", "-okdir") for t in cmd),
        "awk": lambda: not any("inplace" in t for t in cmd),
        "gawk": lambda: not any("inplace" in t for t in cmd),
        "sed": lambda: not any(t == "--in-place" or re.fullmatch(r"-[a-zA-Z]*i[a-zA-Z]*", t) for t in cmd),
        "pre-commit": lambda: "uninstall" not in cmd,
    }
    if name in special:
        return special[name]()
    return name in READ_ONLY


def check_bash(payload, protected):
    raw = payload.get("tool_input", {}).get("command", "")
    cmd = unquote_split(strip_heredocs(raw))
    if DANGEROUS_ANYWHERE.search(cmd):
        return "disabling git hooks (core.hooksPath / pre-commit uninstall) — hooks are a ward"
    frags = [f.rstrip("/") for f in protected]
    for seg in segments(cmd):
        text = " ".join(seg)
        hits = sorted({f for f in frags if f in text})
        if HOME_SETTINGS_RE.search(text):
            hits.append("~/.claude/settings")
        if DOTGIT_RE.search(text) and not read_only(seg):
            hits.append(".git")
        if hits and not read_only(seg):
            return f"shell write touching {', '.join(hits)} — the wards' own config/wiring is human-owned"
    return None


# -------------------------------------------------------------------- main --


def main():
    payload = read_payload()
    if payload.get("hook_event_name") != "PreToolUse":
        return
    tool = payload.get("tool_name", "")
    anchor_path = payload.get("tool_input", {}).get("file_path") or payload.get("cwd") or os.getcwd()
    enabled, protected = configure(anchor_path)
    changed = config_fingerprint_changed(payload.get("session_id", ""), anchor_path)
    if changed:
        audit(f"WARN: {HOOK}", f".wards/config.toml changed mid-session ({changed})")
        sys.stderr.write(
            f"WARD NOTICE: .wards/config.toml changed during this session ({changed}). "
            "If you did not do this at the human's explicit request, say so now.\n"
        )
    if not enabled:
        return
    reason = None
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        reason = check_file_tool(payload, protected)
    elif tool == "Bash":
        reason = check_bash(payload, protected)
    if reason:
        audit(f"BLOCKED: {HOOK}", f"{reason}\t{payload.get('tool_input')}")
        block(
            f"WARD BLOCK (tamper): {reason}. Do not modify hooks, settings, or .wards/ yourself; "
            f"if a ward is wrong, stop and tell the human what to change."
        )


if __name__ == "__main__":
    main()
