#!/usr/bin/env python3
"""Layer 5 — git guard for Claude Code (PreToolUse on the Bash tool).

Sibling of ward-az-guard.sh, aimed at the repo instead of the cloud account.
Agents under pressure to "go green" reach for the same handful of git
commands a careful human never runs unsupervised. This blocks those (exit 2,
reason to the agent) and enforces commit-message hygiene. Every mutating git
command — allowed or blocked — is audit-logged.

Blocked (human runs these):
  hook bypass          --no-verify (and git's unambiguous abbreviations), commit -n,
                       -c core.hooksPath=…, config core.hooksPath / alias.*
  history rewrite      push --force/-f/--force-with-lease/--force-if-includes/+refspec/
                       --mirror/--prune, filter-branch, filter-repo (also the standalone
                       git-filter-repo tool), reflog expire/delete, gc --prune, prune,
                       update-ref (all), rebase -i/--root
  work destruction     reset --hard, clean -f, checkout/restore of the whole tree or a
                       directory, checkout -f/--force, switch -C/-f/--discard-changes,
                       stash drop/clear, branch -D/-df/-f/-M, rm -r, worktree remove -f /
                       prune, push --delete / push :branch
  config / identity    config --global|--system WRITES (reads pass), remote remove/rm/
                       set-url/rename

Commit messages: Conventional Commits on the first line — `type(scope)?: subject`,
subject <= 72. Handles -m, -am"…", --message=, `-m "$(cat <<'EOF' … EOF)"`,
`-F - <<'EOF'`, and `echo … | git commit -F -`. Messages that cannot be known
statically (-F file, editor, $VAR, $(cmd)) pass. Merge/Revert/fixup!/squash!/
Initial commit are exempt (commitlint's own ignore list).

Two nets: a structured parse of every `git …` invocation (segments split on
; & | && || and NEWLINES, quotes respected, redirections stripped, `sh -c "…"`
/ `bash -c` / `eval "…"` bodies re-scanned), plus a coarse regex net over the
quote-normalised command that catches the deadliest patterns even when git is
reached indirectly ($IFS tricks, subprocess strings). Heredoc bodies are data
and are excluded from both.

Honest limit: a shell string can always hide a command (`$G push …`,
`$(which git)`). This guard is a mistake-catcher with an audit trail; the layer
that holds against a determined agent is server-side branch protection + CI.

Config (.wards/config.toml):
  [git]
  enabled = true
  conventional_commits = true
  types = ["feat","fix","docs","style","refactor","perf","test","build","ci","chore","revert"]
  subject_max = 72
"""

import os
import re
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wardlib import as_list, audit, block, config, read_payload

HOOK = "git"
DEFAULT_TYPES = ["feat", "fix", "docs", "style", "refactor", "perf", "test", "build", "ci", "chore", "revert"]
SET = {"enabled": True, "conventional": True, "types": DEFAULT_TYPES, "subject_max": 72, "cc_re": None}

MUTATING = {
    "commit",
    "push",
    "reset",
    "clean",
    "checkout",
    "restore",
    "stash",
    "branch",
    "rebase",
    "merge",
    "cherry-pick",
    "revert",
    "tag",
    "filter-branch",
    "filter-repo",
    "reflog",
    "gc",
    "prune",
    "update-ref",
    "config",
    "remote",
    "rm",
    "mv",
    "am",
    "apply",
    "switch",
    "worktree",
    "submodule",
    "notes",
    "symbolic-ref",
}
GIT_GLOBAL_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
SEPARATORS = {";", "&", "&&", "|", "||", ";;", "|&"}
SHELL_WRAPPERS = {"sh", "bash", "zsh", "dash", "ksh"}
CC_EXEMPT = re.compile(
    r"^(Merge (branch|pull request|remote-tracking branch|tag)\b|Revert \"|fixup! |squash! |amend! "
    r"|Initial commit\b|Automatic merge\b)"
)
HEREDOC_RE = re.compile(
    r"(?P<open>(?<!<)<<(?!<)-?\s*['\"]?(?P<tag>\w+)['\"]?[^\n]*\n)(?P<body>.*?)\n\s*(?P=tag)\s*$", re.S | re.M
)
REDIRECT_TOKEN = re.compile(r"^\d*(>>?\|?|<<?<?|>&|<&|&>>?)\S*$")
# Coarse net over the quote-normalised command: catches indirect git (sh -c, python subprocess strings, $IFS).
COARSE = [
    # (regex, reason, always) — always=True entries are checked even without indirection
    (re.compile(r"--no-veri(f(y)?)?\b"), "hook bypass (--no-verify)", False),
    (
        re.compile(
            r"\bpush\b[^\n;&|]{0,120}?(--force\b|--force-w\S*|--force-i\S*|[\s,'\"]-[a-zA-Z]*f[a-zA-Z]*\b|[\s,'\"]\+\w)"
        ),
        "force push",
        False,
    ),
    (re.compile(r"\breset\b[^\n;&|]{0,40}?--har(d)?\b"), "reset --hard", False),
    (re.compile(r"\bclean\b[^\n;&|]{0,40}?-[a-zA-Z]*f"), "git clean -f", False),
    (
        re.compile(r"\bfilter-branch\b|\bfilter-repo\b|\bgit[_-]filter[_-]repo\b"),
        "history rewrite (filter-branch/filter-repo)",
        True,
    ),
    (re.compile(r"\bcore\.hooksPath\b", re.I), "hook bypass (core.hooksPath)", True),
]


def configure(anchor_path):
    cfg = config(HOOK, anchor_path)
    SET["enabled"] = cfg.get("enabled", True)
    SET["conventional"] = cfg.get("conventional_commits", True)
    SET["types"] = as_list(cfg.get("types", DEFAULT_TYPES)) or DEFAULT_TYPES
    SET["subject_max"] = int(cfg.get("subject_max", 72))
    SET["cc_re"] = re.compile(
        rf"^({'|'.join(map(re.escape, SET['types']))})(\([^)]+\))?!?: (?P<subject>\S.*)$"
    )


# ------------------------------------------------------------------ parsing --


def strip_heredocs(command):
    """Heredoc bodies are data, not commands (docs mentioning `git push --force`)."""
    return HEREDOC_RE.sub(lambda m: m.group("open") + m.group("tag"), command)


def normalise(command):
    """Quote-splitting tricks (`g""it`, `\\git`, `gi\\t`) collapse for both nets."""
    return re.sub(r"\"\"|''|\\(?=[\w./-])", "", strip_heredocs(command))


def newlines_to_separators(command):
    """Replace newlines that are OUTSIDE quotes with ';' so each line is its own
    segment; newlines inside a quoted commit message stay part of the token."""
    out, quote, escaped = [], None, False
    for ch in command:
        if escaped:
            out.append(ch)
            escaped = False
        elif ch == "\\" and quote != "'":
            out.append(ch)
            escaped = True
        elif quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            out.append(ch)
            quote = ch
        elif ch == "\n":
            out.append(" ; ")
        else:
            out.append(ch)
    return "".join(out)


def tokenize(command):
    """Shell-ish tokens: ; & | && || (and top-level newlines) are separator tokens; quotes respected."""
    command = newlines_to_separators(command)
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = ""
        return list(lex)
    except ValueError:  # unbalanced quotes: degrade to whitespace + separator split
        return [t for t in re.split(r"(\s+|;|&&|\|\||\||&)", command) if t and not t.isspace()]


def segments(tokens):
    seg = []
    for t in [*tokens, ";"]:
        if t in SEPARATORS:
            if seg:
                yield seg
            seg = []
        else:
            seg.append(t)


def strip_redirects(seg):
    """Drop `2>&1`, `>file`, `>> log`, `2 > /dev/null`, `<in`, `<<EOF` tokens (fd digits,
    operators and a bare operator's target)."""
    out, skip = [], False
    for i, t in enumerate(seg):
        if skip:
            skip = False
            continue
        nxt = seg[i + 1] if i + 1 < len(seg) else ""
        if t.isdigit() and REDIRECT_TOKEN.match(nxt or "x"):
            continue  # fd number split off by the tokenizer: `2 > /dev/null`
        if REDIRECT_TOKEN.match(t):
            skip = t in (">", ">>", "<", ">|", "&>", "&>>") or re.fullmatch(r"\d>>?", t) is not None
            continue
        out.append(t)
    return out


def git_invocations(command):
    """Yield (subcommand, args, segment_text) for every git invocation, including
    inside `sh -c "…"` / `bash -c` / `eval "…"` bodies and $(…) / backtick starts."""
    for raw_seg in segments(tokenize(command)):
        seg = strip_redirects(raw_seg)
        for i, t in enumerate(seg):
            head = t.lstrip("$(`")
            if head == "git" or head.endswith("/git"):
                sub, args = _split_git(seg[i + 1 :])
                if sub:
                    yield sub, args, " ".join(seg)
                break
            if head.startswith("git-") and len(head) > 4:  # standalone git-filter-repo & co.
                yield head[4:], seg[i + 1 :], " ".join(seg)
                break
        if seg and os.path.basename(seg[0]) in SHELL_WRAPPERS | {"eval"}:
            for body in [a for a in seg[1:] if not a.startswith("-")]:
                yield from git_invocations(body)


def _split_git(toks):
    """Skip git's global options (blocking hook-path / alias injection); return (subcommand, args)."""
    i = 0
    while i < len(toks) and toks[i].startswith("-"):
        if toks[i] == "-c" and i + 1 < len(toks) and _bad_config_key(toks[i + 1]):
            return "config", [toks[i + 1]]
        if toks[i].startswith("-c") and len(toks[i]) > 2 and _bad_config_key(toks[i][2:]):
            return "config", [toks[i][2:]]
        i += 2 if toks[i] in GIT_GLOBAL_WITH_ARG else 1
    return (toks[i], toks[i + 1 :]) if i < len(toks) else (None, [])


def _bad_config_key(kv):
    key = kv.split("=", 1)[0].lower()
    return key in ("core.hookspath",) or key.startswith("alias.")


# ------------------------------------------------------------------- flags --

GUARDED_LONG = [
    "--force",
    "--force-with-lease",
    "--force-if-includes",
    "--hard",
    "--no-verify",
    "--delete",
    "--mirror",
    "--prune",
    "--interactive",
    "--root",
    "--discard-changes",
    "--global",
    "--system",
]


def has_flag(args, *flags):
    """Exact, `=value`, or git-style unambiguous prefix (`--har` for --hard, len >= 4)."""
    for a in args:
        if a in flags or any(a.startswith(f + "=") for f in flags):
            return True
        if a.startswith("--") and len(a) >= 5:
            stem = a.split("=", 1)[0]
            matches = [g for g in GUARDED_LONG if g.startswith(stem)]
            if matches and any(m in flags for m in matches):
                return True
    return False


def has_short(args, letter):
    """True if a short-flag cluster contains `letter` (e.g. -fd, -sn, -df)."""
    return any(re.fullmatch(r"-[a-zA-Z]+", a) and letter in a[1:] for a in args)


def positionals(args):
    return [a for a in args if not a.startswith("-")]


def without_message_values(args):
    """Drop -m/--message values so a message body like '-n' isn't read as a flag."""
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a in ("-m", "--message", "-F", "--file"):
            skip = True
            continue
        if a.startswith("--message=") or a.startswith("--file=") or re.fullmatch(r"-[a-zA-Z]*m.+", a):
            continue
        out.append(a)
    return out


def is_dir_target(p):
    return p in (".", "./", ":/", "*") or p.endswith("/") or os.path.isdir(p)


# ------------------------------------------------------------------- policy --


def rule_for(sub, args):
    """Return a block reason for (subcommand, args), or None if allowed."""
    flags = without_message_values(args) if sub == "commit" else args
    if has_flag(flags, "--no-verify") or (sub == "commit" and has_short(flags, "n")):
        return (
            "hook bypass (--no-verify): pre-commit/commit-msg hooks are a ward — fix what they flag instead"
        )
    return (CHECKS.get(sub) or (lambda _a: None))(flags)


def _push(a):
    if has_flag(a, "--force", "--force-with-lease", "--force-if-includes") or has_short(a, "f"):
        return "force push rewrites shared history"
    if any(p.startswith("+") for p in positionals(a)):
        return "force push via +refspec rewrites shared history"
    if has_flag(a, "--mirror", "--prune"):
        return "push --mirror/--prune deletes remote refs"
    if has_flag(a, "--delete", "-d") or any(p.startswith(":") for p in positionals(a)):
        return "remote branch deletion"
    return None


def _reset(a):
    return "reset --hard discards uncommitted work" if has_flag(a, "--hard") else None


def _clean(a):
    return "git clean -f deletes untracked files" if has_flag(a, "--force") or has_short(a, "f") else None


def _checkout(a):
    if has_flag(a, "--force") or has_short(a, "f"):
        return "checkout -f discards uncommitted changes"
    if "--" in a:
        targets = a[a.index("--") + 1 :]
    else:
        pos = positionals(a)
        targets = pos[1:] if len(pos) > 1 else pos  # `checkout .`, `checkout HEAD .`, `checkout main src/`
    if any(is_dir_target(p) for p in targets):
        return "checkout of a directory / whole tree discards uncommitted changes under it"
    return None


def _restore(a):
    if has_flag(a, "--staged", "-S") and not has_flag(a, "--worktree", "-W"):
        return None  # unstaging is harmless
    return (
        "restore <dir> discards uncommitted changes under it"
        if any(is_dir_target(p) for p in positionals(a))
        else None
    )


def _switch(a):
    if (
        has_flag(a, "--discard-changes")
        or has_short(a, "f")
        or has_short(a, "C")
        or has_flag(a, "--force-create")
    ):
        return "switch -C/-f/--discard-changes discards work or clobbers a branch"
    return None


def _stash(a):
    return "stash drop/clear destroys stashed work" if a and a[0] in ("drop", "clear") else None


def _branch(a):
    if has_short(a, "D") or has_short(a, "M") or has_flag(a, "--force") or has_short(a, "f"):
        return "branch -D/-M/-f clobbers or force-deletes a branch"
    if (has_flag(a, "--delete") or has_short(a, "d")) and has_short(a, "f"):
        return "branch -df force-deletes an unmerged branch"
    return None


def _config(a):
    if any(_bad_config_key(k) for k in positionals(a)):
        return "config alias.* / core.hooksPath re-routes git commands or disables hooks"
    if has_flag(a, "--global", "--system") and not (
        has_flag(a, "--list", "-l", "--get", "--get-all", "--get-regexp")
        or any(x.startswith("--get") for x in a)
    ):
        return "global/system git config is the human's identity"
    return None


def _remote(a):
    return "remote removal/URL change/rename" if a and a[0] in ("remove", "rm", "set-url", "rename") else None


def _reflog(a):
    return "reflog expire/delete destroys recovery points" if a and a[0] in ("expire", "delete") else None


def _gc(a):
    return "gc --prune destroys recovery points" if has_flag(a, "--prune") else None


def _rm(a):
    return "git rm -r removes trees (tests, dirs) — human decision" if has_short(a, "r") else None


def _worktree(a):
    if a and a[0] == "prune":
        return "worktree prune"
    if a and a[0] == "remove" and (has_flag(a, "--force") or has_short(a, "f")):
        return "worktree remove --force"
    return None


def _rebase(a):
    return (
        "interactive/root rebase rewrites history"
        if has_flag(a, "--interactive", "--root") or has_short(a, "i")
        else None
    )


def _always(reason):
    return lambda _a: reason


CHECKS = {
    "push": _push,
    "reset": _reset,
    "clean": _clean,
    "checkout": _checkout,
    "restore": _restore,
    "switch": _switch,
    "stash": _stash,
    "branch": _branch,
    "config": _config,
    "remote": _remote,
    "reflog": _reflog,
    "gc": _gc,
    "rm": _rm,
    "worktree": _worktree,
    "rebase": _rebase,
    "prune": _always("git prune destroys recovery points"),
    "update-ref": _always("update-ref rewrites/deletes refs directly"),
    "filter-branch": _always("history rewrite (filter-branch)"),
    "filter-repo": _always("history rewrite (filter-repo)"),
}


# ---------------------------------------------------------- commit messages --


def _message_arg(args):
    """Raw -m/--message value, "$(stdin)" for -F -, or None (editor / -F file / reuse)."""
    for i, a in enumerate(args):
        nxt = args[i + 1] if i + 1 < len(args) else None
        if a in ("-m", "--message") and nxt is not None:
            return nxt
        if a.startswith("--message="):
            return a.split("=", 1)[1]
        m = re.fullmatch(r"-[a-zA-Z]*m(.*)", a)  # -am"msg", -m"msg", -qm msg
        if m and not a.startswith("--"):
            return m.group(1) or nxt
        if a in ("-F-", "--file=-") or (a in ("-F", "--file") and nxt == "-"):
            return "$(stdin)"
    return None


def first_message(args, command, seg_text):
    """Commit message text, or None if it cannot be known statically."""
    msg = _message_arg(args)
    if msg is None:
        return None
    if msg == "$(stdin)":
        return _piped_message(command, seg_text) or _heredoc_near(command, seg_text)
    if msg.lstrip().startswith("$(") or msg.lstrip().startswith("`"):
        return _heredoc_near(command, seg_text) if "<<" in msg else None
    if re.fullmatch(r"\$\{?\w+\}?", msg.strip()):
        return None  # $MSG — unknowable
    return msg


def _heredoc_near(command, seg_text):
    """The heredoc that belongs to this commit: the first one at/after the commit's position."""
    pos = command.find("git commit") if "git commit" in command else command.find("commit")
    for m in HEREDOC_RE.finditer(command):
        if m.start() >= max(pos, 0) or m.end() >= pos:
            return m.group("body")
    m = HEREDOC_RE.search(command)
    return m.group("body") if m else None


def _piped_message(command, seg_text):
    """`echo "msg" | git commit -F -` / `printf … |` — the previous segment's literal."""
    for line in command.split("\n"):
        if seg_text.split(" ", 2)[0] in line and "|" in line:
            before = line.split("|")[0]
            try:
                toks = shlex.split(before)
            except ValueError:
                return None
            if toks and toks[0] in ("echo", "printf"):
                return " ".join(t for t in toks[1:] if not t.startswith("-")).replace("\\n", "\n")
    return None


def commit_message_problem(args, command, seg_text):
    msg = first_message(args, command, seg_text)
    if msg is None:
        return None
    lines = [ln for ln in msg.strip().splitlines() if ln.strip()]
    first = lines[0].strip() if lines else ""
    if CC_EXEMPT.match(first):
        return None
    if not SET["cc_re"].match(first):
        return (
            f"commit message must follow Conventional Commits: `type(scope)?: subject` with type in "
            f"{{{', '.join(SET['types'])}}}. Got: {first[:80]!r}"
        )
    if len(first) > SET["subject_max"]:
        return (
            f"commit subject line is {len(first)} chars; keep it <= {SET['subject_max']}. "
            "Move detail to the body."
        )
    return None


# --------------------------------------------------------------------- main --


def deny(reason, command):
    audit(f"BLOCKED: {HOOK}", f"{reason}\t{command}")
    block(
        f"WARD BLOCK (git): {reason}. This command class is reserved for a human operator; "
        f"report the block and hand the command to the human — do not work around it."
    )


INDIRECT = re.compile(
    r"(^|[\s;&|(])(sh|bash|zsh|dash|ksh)\s+-[a-zA-Z]*c\b|\beval\b|\bsubprocess\b|os\.system|execSync|"
    r"\bspawn\b|\$IFS|\bxargs\b|which\s+git|command\s+-v\s+git|\$\(\s*git|`\s*git"
)


def coarse_net(norm, command):
    """Second net, only when the command reaches a shell/interpreter indirectly:
    the deadliest patterns anywhere in the quote-normalised text."""
    indirect = bool(INDIRECT.search(norm))
    for rx, reason, always in COARSE:
        if (always or indirect) and rx.search(norm):
            deny(
                f"{reason} — matched by pattern (indirect shell/interpreter call or history-rewrite tool)",
                command,
            )


def main():
    payload = read_payload()
    configure(payload.get("cwd") or os.getcwd())
    if not SET["enabled"] or payload.get("tool_name") != "Bash":
        return
    command = payload.get("tool_input", {}).get("command", "")
    norm = normalise(command)
    if "git" not in norm:
        return
    for sub, args, seg_text in git_invocations(norm):
        reason = rule_for(sub, args)
        if not reason and sub == "commit" and SET["conventional"]:
            reason = commit_message_problem(args, command, seg_text)
        if reason:
            deny(reason, command)
        if sub in MUTATING:
            audit(f"ALLOWED: {HOOK}", command)
    coarse_net(norm, command)


if __name__ == "__main__":
    main()
