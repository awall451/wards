#!/usr/bin/env python3
"""Layer 5 — type-check ward for Claude Code (Stop gate, ratcheting).

Types are the cheapest tests. On Stop, if the agent touched a source file this
session, the project's type checker runs; if it reports MORE errors than it
did when the agent first touched a source file this session, the stop is
blocked with the new errors. Pre-existing type errors never block — the agent
is asked not to add to them, not to fix a legacy codebase unasked.

Auto-detection (nearest project root upward from the touched file):
  tsconfig.json                                  ->  tsc --noEmit -p <root>
  pyrightconfig.json / pyproject [tool.pyright]  ->  pyright <root>
  mypy.ini / pyproject [tool.mypy]               ->  mypy <root>
  go.mod                                         ->  go vet ./...
Only runs if the tool is installed (PATH, node_modules/.bin, .venv/bin).

Config (.wards/config.toml):
  [types]
  enabled = true
  command = "pyright src/"       # overrides auto-detect; "" disables
  timeout = 300
"""

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wardlib import Session, audit, block, config, in_project, rel, run_hook
from wardtools import find_bin

HOOK = "types"
SET = {"cfg": {}, "enabled": True, "timeout": 300}


def configure(anchor_path):
    """Load [types] from the nearest .wards/config.toml above `anchor_path`."""
    SET["cfg"] = config(HOOK, anchor_path)
    SET["enabled"] = SET["cfg"].get("enabled", True)
    SET["timeout"] = int(SET["cfg"].get("timeout", 300))


SOURCE_EXT = {".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts", ".go"}
MARKERS = ("tsconfig.json", "pyrightconfig.json", "mypy.ini", "pyproject.toml", "go.mod", ".git")
# "file(1,2): error TS1234: msg" | "file:1: error: msg" | "./file.go:1:2: msg"  — notes/warnings skipped
ERR_RE = re.compile(r"^(?:vet:\s*)?(?P<file>[^\s:(]+\.\w+)[:(](?P<line>\d+)[^\s]*\s*[-:]?\s*(?P<msg>.+)$")
SKIP_RE = re.compile(r"^(note|warning|info|hint)\b", re.I)


# ------------------------------------------------------------------ project --


def project_root_for(path):
    cur = Path(path).resolve().parent
    for cand in [cur, *cur.parents]:
        if any((cand / m).exists() for m in MARKERS):
            return cand
    return Path(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))


def detect_command(root):
    if "command" in SET["cfg"]:
        return SET["cfg"]["command"] or None
    pyproject = (
        (root / "pyproject.toml").read_text(errors="replace") if (root / "pyproject.toml").exists() else ""
    )
    candidates = [
        ((root / "tsconfig.json").exists(), "tsc", f"tsc --noEmit -p {shlex.quote(str(root))}"),
        (
            (root / "pyrightconfig.json").exists() or "[tool.pyright]" in pyproject,
            "pyright",
            f"pyright {shlex.quote(str(root))}",
        ),
        (
            (root / "mypy.ini").exists() or "[tool.mypy]" in pyproject,
            "mypy",
            f"mypy {shlex.quote(str(root))}",
        ),
        ((root / "go.mod").exists(), "go", "go vet ./..."),
    ]
    for applies, binary, cmd in candidates:
        exe = applies and find_bin(binary, root)
        if exe:
            return cmd.replace(binary, shlex.quote(exe), 1)
    return None


def run_check(command, root):
    """Set of error fingerprints (file, message) — line numbers dropped so edits
    elsewhere in a file don't make an old error look new."""
    try:
        p = subprocess.run(
            shlex.split(command), cwd=root, capture_output=True, text=True, timeout=SET["timeout"]
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        audit(f"WARN: {HOOK}", f"{command} failed to run: {exc}")
        return None
    # Fingerprint = (basename, message): line numbers dropped so edits elsewhere don't make an
    # old error look new; directory dropped so moving a file with a legacy error doesn't either.
    errors = set()
    for ln in (p.stdout + "\n" + p.stderr).splitlines():
        m = ERR_RE.match(ln.strip())
        if m and not SKIP_RE.match(m.group("msg")):
            errors.add((os.path.basename(m.group("file")), m.group("msg").strip()))
    return errors


# ------------------------------------------------------------------- events --


def on_pre(payload):
    """Baseline once per session, lazily, on the first source-file touch."""
    path = payload.get("tool_input", {}).get("file_path")
    if (
        not path
        or not SET["enabled"]
        or os.path.splitext(path)[1].lower() not in SOURCE_EXT
        or not in_project(path)
    ):
        return
    session = Session(payload["session_id"], HOOK)
    if "root" in session.data:
        return
    root = project_root_for(path)
    command = detect_command(root)
    session.data["root"], session.data["command"] = str(root), command
    if command:
        errors = run_check(command, root)
        session.data["baseline_errors"] = sorted(list(e) for e in errors) if errors is not None else None
        audit(f"NOTE: {HOOK}", f"baseline {len(errors or [])} error(s) via `{command}` in {root}")
    session.save()


def on_post(_payload):
    return


def on_stop(payload):
    session = Session(payload["session_id"], HOOK)
    command = session.data.get("command")
    if not SET["enabled"] or not command:
        return
    root = Path(session.data["root"])
    now = run_check(command, root)
    if now is None:
        return
    if session.data.get("baseline_errors") is None:
        audit(
            f"WARN: {HOOK}",
            f"no baseline (checker failed/timed out at first touch); {len(now)} error(s) now, not blocking",
        )
        return
    base = {tuple(e) for e in session.data["baseline_errors"]}
    new = sorted(now - base)
    audit(("BLOCK" if new else "PASS") + f": {HOOK}", f"{command}: {len(now)} error(s), {len(new)} new")
    if new and session.may_block_stop():
        block(
            f"WARD BLOCK (types) — `{command}` reports {len(new)} new type error(s) since you started "
            f"(pre-existing ones are not counted). Fix them before finishing; do not add ignore comments:\n"
            + "\n".join(f"- {rel(f)}: {m}" for f, m in new[:25])
        )
    if not new:
        session.clear_stop_blocks()


if __name__ == "__main__":
    run_hook(HOOK, configure, (on_pre, on_post, on_stop))
