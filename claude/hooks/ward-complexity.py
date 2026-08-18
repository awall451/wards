#!/usr/bin/env python3
"""Layer 5 — code-shape ward for Claude Code (complexity, length, params, nesting).

Runs `lizard` on every source file the agent edits and enforces a band-based,
ratcheting policy on four per-function metrics plus file length. Three hook
events, one script:

  PreToolUse  (Edit|Write|MultiEdit)  snapshot the file's per-function metrics
                                      the first time it is touched this session
                                      — that snapshot is the "pre-existing"
                                      baseline for the ratchet.
  PostToolUse (Edit|Write|MultiEdit)  re-measure; block (exit 2) or warn.
  Stop                                re-measure every file touched this
                                      session; block the stop if anything
                                      still violates.

Metrics and default bands (upper bounds; tune in .wards/config.toml [complexity]):

  metric        ok    moderate   high     very-high
  ccn           <=10  11-20      21-50    51+        cyclomatic complexity
  length        <=50  51-100     101-200  201+       function NLOC
  params        <=4   5-6        7-10     11+        parameter count
  file_lines    <=400 401-800    801-1500 1501+      whole-file line count

(Nesting depth is deliberately absent: lizard's -END extension is unreliable
across languages. CCN catches most arrow code; revisit with a better tool.)

Policy per (function, metric):
  ok         silent
  moderate   allowed, audit-logged
  high/vh    NEW or WORSENED into the band -> block: bring it back down
             pre-existing, unchanged/improved -> warn once: tell the human,
             add no more
             pre-existing, increased -> block

Escape hatch is human-only: `.wards/complexity-allow.txt` next to
.wards/config.toml, one function name per line. The agent must not edit it —
if a function is genuinely irreducible, stop and explain to the human.

Config (.wards/config.toml):
  [complexity]
  ccn = [10, 20, 50]          # upper bound of ok / moderate / high
  length = [50, 100, 200]
  params = [4, 6, 10]
  file_lines = [400, 800, 1500]
  ignore = ["node_modules", "vendor", "dist", "build", ".min."]
"""

import csv
import io
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wardlib import (
    Session,
    as_list,
    audit,
    bands,
    block,
    config,
    find_upward,
    in_project,
    rel,
    run_hook,
    warn_context,
)
from wardtools import lang_of, tool_for

HOOK = "complexity"
DEFAULTS = {
    "ccn": [10, 20, 50],
    "length": [50, 100, 200],
    "params": [4, 6, 10],
    "file_lines": [400, 800, 1500],
}
DEFAULT_IGNORE = [
    "node_modules",
    "vendor",
    "dist",
    "build",
    ".min.",
    "migrations",
    "alembic/versions",
    ".generated.",
]
SET = {"thresholds": dict(DEFAULTS), "ignore": list(DEFAULT_IGNORE), "lint_cfg": {}}


def configure(anchor_path):
    """Load [complexity] from the nearest .wards/config.toml above `anchor_path`."""
    cfg = config(HOOK, anchor_path)
    SET["thresholds"] = {m: bands(cfg.get(m, d), d, f"complexity.{m}") for m, d in DEFAULTS.items()}
    SET["ignore"] = as_list(cfg.get("ignore", DEFAULT_IGNORE)) or list(DEFAULT_IGNORE)
    SET["lint_cfg"] = config("lint", anchor_path)


LABEL = {"ccn": "CCN", "length": "NLOC", "params": "params", "file_lines": "lines"}
HINT = {
    "ccn": "extract helpers, early-return, table-drive branches, split by responsibility",
    "length": "split into named steps; one function, one job",
    "params": "group related params into an object/dataclass, or split the function",
    "file_lines": "split the module by responsibility",
}


# ------------------------------------------------------------------ measure --


def band(metric, value):
    mod, high, vhigh = SET["thresholds"][metric]
    if value <= mod:
        return "ok"
    if value <= high:
        return "moderate"
    if value <= vhigh:
        return "high"
    return "very-high"


def ignored(path):
    """Ignore entries match whole path components (`build` ≠ `builder/`), or — when they
    contain a dot or slash — as substrings of the path (`.min.`, `alembic/versions`)."""
    norm = path.replace(os.sep, "/")
    parts = set(norm.split("/"))
    for raw in SET["ignore"]:
        ent = raw.strip("/")
        if ent and ((("." in ent or "/" in ent) and ent in norm) or ent in parts):
            return True
    return False


def allow_list(path):
    f = find_upward(path, os.path.join(".wards", "complexity-allow.txt"))
    if not f:
        return set()
    return {ln.strip() for ln in f.read_text().splitlines() if ln.strip() and not ln.startswith("#")}


def _stamp(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def measure(path):
    """{long_name: {name, line, ccn, length, params}} plus a synthetic
    "(file)" entry for file_lines. None if lizard unavailable; {} if unsupported.
    Sibling PostToolUse hooks (the formatter in ward-lint) run in parallel and may
    rewrite the file mid-measure; if the file changed while lizard ran, measure again."""
    if not shutil.which("lizard"):
        return None
    for _attempt in range(3):
        before = _stamp(path)
        result = _measure_once(path)
        if _stamp(path) == before:
            return result
    return result


def _formatted_copy(path):
    """If the project has a formatter for this file, measure a FORMATTED temp copy so a
    formatter run by the sibling lint hook can't change NLOC/lines under the ratchet.
    Returns (path_to_measure, cleanup_path_or_None)."""
    lang = lang_of(path)
    tool = tool_for(lang, "format", path, SET["lint_cfg"].get(lang, {})) if lang else None
    if not tool:
        return path, None
    fd, tmp = tempfile.mkstemp(suffix=os.path.splitext(path)[1], dir=os.path.dirname(path) or None)
    os.close(fd)
    try:
        with open(path, "rb") as src, open(tmp, "wb") as dst:
            dst.write(src.read())
        argv = [tmp if a == path else a for a in tool[0]]
        subprocess.run(argv, capture_output=True, timeout=45, cwd=os.path.dirname(path) or None)
        return tmp, tmp
    except (OSError, subprocess.TimeoutExpired):
        os.unlink(tmp)
        return path, None


def _measure_once(path):
    if not os.path.isfile(path):
        return {}
    target, cleanup = _formatted_copy(path)
    try:
        out = subprocess.run(["lizard", "--csv", target], capture_output=True, text=True, timeout=45).stdout
        return _parse_lizard(out, target)
    except subprocess.TimeoutExpired:
        audit(f"WARN: {HOOK}", f"lizard timeout on {path}")
        return {}
    finally:
        if cleanup:
            os.unlink(cleanup)


def _param_count(row):
    """lizard counts `self`/`cls`; Python methods shouldn't pay for them."""
    n = int(row[3])
    sig = row[8]
    inner = sig[sig.find("(") + 1 :] if "(" in sig else ""
    first = inner.strip().split(",")[0].strip().split(" ")[0] if inner.strip() else ""
    return n - 1 if first in ("self", "cls") and n > 0 else n


def _parse_lizard(out, target):
    fns = {}
    # CSV: nloc, ccn, tokens, params, length, location, file, name, long_name, start, end
    for row in csv.reader(io.StringIO(out)):
        if len(row) < 11:
            continue
        cur = {
            "name": row[7],
            "line": int(row[9]),
            "ccn": int(row[1]),
            "length": int(row[0]),
            "params": _param_count(row),
        }
        prev = fns.get(row[8])
        if prev is None or cur["ccn"] > prev["ccn"]:
            fns[row[8]] = cur
    if fns:  # lizard understood the file → file length counts too
        with open(target, "rb") as fh:
            fns["(file)"] = {"name": "(file)", "line": 1, "file_lines": sum(1 for _ in fh)}
    return fns


def baseline_value(base, long_name, name, metric):
    """Ratchet lookup: exact signature first, then same short name in this file (a
    signature change to a legacy function shouldn't make it look new), then the same
    name in ANY file touched this session (moving a legacy function into a new module
    is the refactor we recommend — it must not read as "new")."""
    if long_name in base and metric in base[long_name]:
        return base[long_name][metric]
    same = [v[metric] for v in base.values() if v["name"] == name and metric in v]
    if same:
        return max(same)
    if name != "(file)":
        elsewhere = [
            v[metric]
            for other in SET.get("all_baselines", {}).values()
            for v in other.values()
            if v.get("name") == name and metric in v
        ]
        if elsewhere:
            return max(elsewhere)
    return None


# ------------------------------------------------------------------- policy --


def judge_one(ctx, long_name, cur, metric):
    """(level, key, message) for one function × metric, or None if fine.
    ctx = (path, baseline-for-path, allow-set)."""
    path, base, allowed = ctx
    value = cur[metric]
    b = band(metric, value)
    if b == "ok":
        return None
    name, line = cur["name"], cur["line"]
    loc = f"{rel(path)}:{line} {name}() {LABEL[metric]}={value}"
    key = f"{path}::{name}::{metric}"
    if b == "moderate":
        return "NOTE", key, f"{loc} [moderate]"
    if name in allowed or long_name in allowed:
        return "NOTE", key, f"{loc} [{b}, human allow-listed]"
    prev = baseline_value(base, long_name, name, metric)
    level, msg = _verdict(metric, value, prev, b)
    return level, key, f"{loc} {msg}"


def _verdict(metric, value, prev, b):
    """Policy for a high/very-high value given its baseline `prev` (None = new)."""
    ceiling = SET["thresholds"][metric][1]
    legacy = prev is not None and band(metric, prev) in ("high", "very-high")
    if not legacy:
        was = prev if prev is not None else "new"
        return "BLOCK", (
            f"[{b}] — new or worsened into {b} risk (was {was}). "
            f"Bring {LABEL[metric]} to <= {ceiling}: {HINT[metric]}."
        )
    if metric == "file_lines" and band(metric, value) == band(metric, prev):
        return "WARN", (
            f"[{b}, legacy, was {prev}] — large legacy file. Fine to edit; prefer putting new "
            f"code in a new module, and tell the human a split is recommended."
        )
    if value > prev:
        return "BLOCK", (
            f"[{b}, legacy] — already {b} risk before this session (was {prev}); this edit "
            f"made it worse (+{value - prev}). Do not grow a {b}-risk function: put new logic in a new "
            f"helper, or refactor first. If neither is possible, stop and tell the human a refactor "
            f"is needed before this change."
        )
    fragile = " VERY HIGH: treat as fragile — smallest possible edits." if b == "very-high" else ""
    return "WARN", (
        f"[{b}, legacy, was {prev}] — pre-existing {b}-risk function. Not your job to refactor "
        f"unasked, but: add NO further {LABEL[metric]} to it, and tell the human it exists and a "
        f"refactor is recommended.{fragile}"
    )


def judge(path, base, now, allowed):
    ctx = (path, base, allowed)
    for long_name, cur in sorted(now.items(), key=lambda kv: -kv[1].get("ccn", 0)):
        for metric in SET["thresholds"]:
            if metric in cur:
                verdict = judge_one(ctx, long_name, cur, metric)
                if verdict:
                    yield verdict


def check(session, paths):
    """Judge `paths` against the session baseline. Returns (blocks, warns)."""
    SET["all_baselines"] = session.baseline
    blocks, warns = [], []
    for path in paths:
        if ignored(path):
            continue
        now = measure(path)
        if now is None:
            msg = "complexity ward INACTIVE: `lizard` not on PATH (pipx install lizard). Tell the human."
            audit(f"WARN: {HOOK}", msg)
            return blocks, [msg]
        for level, key, msg in judge(path, session.baseline.get(path, {}), now, allow_list(path)):
            if level == "BLOCK":
                blocks.append(msg)
            elif level == "WARN" and session.warn_once(key):
                warns.append(msg)
            audit(f"{level}: {HOOK}", msg)
    session.save()
    return blocks, warns


# ------------------------------------------------------------------- events --


def on_pre(payload):
    path = payload.get("tool_input", {}).get("file_path")
    if not path or ignored(path) or not in_project(path):
        return
    session = Session(payload["session_id"], HOOK)
    if path in session.baseline:
        return  # baseline = state at FIRST touch this session
    session.baseline[path] = measure(path) or {}
    session.save()


def on_post(payload):
    path = payload.get("tool_input", {}).get("file_path")
    if not path or not in_project(path):
        return
    session = Session(payload["session_id"], HOOK)
    blocks, warns = check(session, [path])
    if blocks:
        block(block_text(blocks, warns))
    warn_context("PostToolUse", "WARD (code shape):", warns)


def on_stop(payload):
    session = Session(payload["session_id"], HOOK)
    if not session.touched():
        return
    blocks, _ = check(session, session.touched())
    if blocks and session.may_block_stop():
        block(
            "WARD BLOCK (code shape) — files touched this session still violate policy; "
            "resolve before finishing:\n" + "\n".join(f"- {b}" for b in blocks)
        )
    if not blocks:
        session.clear_stop_blocks()


def block_text(blocks, warns):
    t = SET["thresholds"]
    lines = [
        "WARD BLOCK (code shape). The edit was applied but violates the code-shape policy; "
        "fix it before doing anything else.",
        f"Policy per function: CCN <= {t['ccn'][0]} target / <= {t['ccn'][1]} acceptable; "
        f"NLOC <= {t['length'][1]}; params <= {t['params'][1]}; file <= {t['file_lines'][1]} lines. "
        "New or worsened code past those is blocked. Legacy high-risk code: no growth, tell the human.",
        "Do not edit .wards/complexity-allow.txt yourself. "
        "If genuinely irreducible, stop and explain to the human.",
        "",
    ] + [f"- {b}" for b in blocks]
    if warns:
        lines += ["", "Also note:"] + [f"- {w}" for w in warns]
    return "\n".join(lines)


if __name__ == "__main__":
    run_hook(HOOK, configure, (on_pre, on_post, on_stop))
