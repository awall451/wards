#!/usr/bin/env python3
"""Layer 5 — tests ward for Claude Code.

Two guarantees, one script:

  B4  "Done" means the tests ran and passed. On Stop, if the agent touched any
      source file this session, the project's test command runs (auto-detected
      or configured) for every project touched. Non-zero exit blocks the stop
      with the tail of the output.
      Ratchet: the suite is run ONCE at the first source-file touch. If it was
      already red before the agent did anything, the run gate is disabled for
      the session (audited + told to the agent: "suite was already red — tell
      the human") — the agent is not held hostage to a broken suite it didn't
      break, and cannot be blamed for it either.

  C2  Tests are not deleted or skipped to go green. At first touch the ward
      inventories every test file in the project (count of test functions +
      unconditional skip markers). On Stop, any test file with fewer tests or
      more skips — or gone entirely — blocks, no matter HOW it was removed
      (Edit, `rm`, `git rm`, `mv`). Renames are recognised (same test count
      appears in a new file); parametrize/.each consolidation is tolerated.

Auto-detection (nearest project root upward from the touched file):
  pyproject.toml / pytest.ini / conftest.py / setup.cfg / tests dir  ->  pytest -q
  package.json with scripts.test                                    ->  npm test --silent
  go.mod                                                            ->  go test ./...
  Cargo.toml                                                        ->  cargo test --quiet
The command's binary is resolved through .venv/bin / node_modules/.bin / PATH;
a missing binary is audited and skipped, never blocked.

Config (.wards/config.toml):
  [tests]
  enabled = true
  command = "pytest -q tests/"      # overrides auto-detect; "" disables the run
  run_on_stop = true
  baseline = true                   # run once at first touch; red baseline disables the gate
  timeout = 600
  test_paths = ["tests", "test", "spec", "__tests__"]
"""

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wardlib import Session, as_list, audit, block, config, in_project, rel, run_hook, warn_context
from wardtools import find_bin

HOOK = "tests"
DEFAULT_TEST_DIRS = ["tests", "test", "spec", "__tests__"]
SET = {
    "cfg": {},
    "enabled": True,
    "run_on_stop": True,
    "baseline": True,
    "timeout": 600,
    "test_dirs": set(DEFAULT_TEST_DIRS),
}

SOURCE_EXT = {
    ".py",
    ".js",
    ".mjs",
    ".cjs",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".cs",
    ".rb",
    ".php",
    ".swift",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".sh",
    ".bash",
    ".tf",
}
TEST_FILE_RE = re.compile(r"(^|/)(test_[^/]+\.py|[^/]+_test\.(py|go)|[^/]+\.(test|spec)\.[cm]?[jt]sx?)$")
SKIP_DIRS = {
    "node_modules",
    ".venv",
    "venv",
    ".git",
    "dist",
    "build",
    "site-packages",
    "__pycache__",
    "vendor",
}
INVENTORY_CAP = 3000

# Per language: (test-definition regex, unconditional-skip regex, consolidation regex)
PATTERNS = {
    "python": (
        re.compile(r"^\s*(async\s+)?def\s+test_\w+", re.M),
        re.compile(
            r"(pytest\.mark\.skip\b(?!if)|pytest\.mark\.xfail\b|unittest\.skip\b(?!If|Unless)|@skip\b|pytest\.skip\()"
        ),
        re.compile(r"pytest\.mark\.parametrize\("),
    ),
    "javascript": (
        re.compile(r"(^|[^\w.])(it|test)\s*(\.each\s*\([^)]*\)\s*)?\(", re.M),
        re.compile(r"\b(x(it|test|describe)\s*\(|(it|test|describe)\.(skip|todo)\s*\(|\.skip\s*\()"),
        re.compile(r"\b(it|test|describe)\.each\s*\("),
    ),
    "go": (
        re.compile(r"^func\s+Test\w+\s*\(", re.M),
        re.compile(r"\bt\.Skip(Now|f)?\s*\("),
        re.compile(r"$^"),
    ),
    "rust": (re.compile(r"#\[test\]"), re.compile(r"#\[ignore\b"), re.compile(r"$^")),
}
LANG_BY_EXT = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "javascript",
    ".tsx": "javascript",
    ".go": "go",
    ".rs": "rust",
}
PROJECT_MARKERS = (
    "pyproject.toml",
    "pytest.ini",
    "conftest.py",
    "setup.cfg",
    "tox.ini",
    "package.json",
    "go.mod",
    "Cargo.toml",
    ".git",
)


def configure(anchor_path):
    cfg = config(HOOK, anchor_path)
    SET["cfg"] = cfg
    SET["enabled"] = cfg.get("enabled", True)
    SET["run_on_stop"] = cfg.get("run_on_stop", True)
    SET["baseline"] = cfg.get("baseline", True)
    SET["timeout"] = int(cfg.get("timeout", 600))
    SET["test_dirs"] = set(as_list(cfg.get("test_paths", DEFAULT_TEST_DIRS)) or DEFAULT_TEST_DIRS)


# ------------------------------------------------------------------ classify --


def is_test_file(path):
    parts = set(Path(path).parts)
    return bool(parts & SET["test_dirs"]) or bool(TEST_FILE_RE.search(path.replace(os.sep, "/")))


def is_source(path):
    return os.path.splitext(path)[1].lower() in SOURCE_EXT


def count_tests(path):
    """{"tests", "skips", "groups"} for a test file (zeros if unreadable/unknown language)."""
    lang = LANG_BY_EXT.get(os.path.splitext(path)[1].lower())
    if not lang or not os.path.isfile(path):
        return {"tests": 0, "skips": 0, "groups": 0}
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    test_re, skip_re, group_re = PATTERNS[lang]
    return {
        "tests": len(test_re.findall(text)),
        "skips": len(skip_re.findall(text)),
        "groups": len(group_re.findall(text)),
    }


def inventory(root):
    """{path: counts} for every test file under the project root (capped)."""
    found, n = {}, 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            if is_test_file(p) and os.path.splitext(fn)[1].lower() in LANG_BY_EXT:
                found[p] = count_tests(p)
                n += 1
                if n >= INVENTORY_CAP:
                    audit(f"WARN: {HOOK}", f"test inventory capped at {INVENTORY_CAP} files under {root}")
                    return found
    return found


# ------------------------------------------------------------------- command --


def project_root_for(path):
    cur = Path(path).resolve().parent
    for candidate in [cur, *cur.parents]:
        if any((candidate / m).exists() for m in PROJECT_MARKERS):
            return candidate
    return Path(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))


def project_roots(paths):
    roots = []
    for p in paths:
        r = project_root_for(p)
        if r not in roots:
            roots.append(r)
    return roots


def detect_command(root):
    if "command" in SET["cfg"]:
        return SET["cfg"]["command"] or None
    if any((root / m).exists() for m in ("pytest.ini", "conftest.py")) or _pyproject_has_pytest(root):
        return "pytest -q"
    if (root / "package.json").exists() and _npm_has_test(root / "package.json"):
        return "npm test --silent"
    if (root / "go.mod").exists():
        return "go test ./..."
    if (root / "Cargo.toml").exists():
        return "cargo test --quiet"
    return None


def _pyproject_has_pytest(root):
    pp = root / "pyproject.toml"
    if pp.exists() and "pytest" in pp.read_text(errors="replace"):
        return True
    return any((root / d).is_dir() and any((root / d).glob("test_*.py")) for d in SET["test_dirs"])


def _npm_has_test(pkg):
    try:
        script = json.loads(pkg.read_text()).get("scripts", {}).get("test", "")
    except (json.JSONDecodeError, OSError):
        return False
    return bool(script) and "no test specified" not in script


def run_tests(command, root):
    """(status, tail): status in {"pass", "fail", "unavailable"}."""
    argv = shlex.split(command)
    exe = find_bin(argv[0], root)
    if not exe:
        return "unavailable", f"{argv[0]!r} not found (PATH, .venv/bin, node_modules/.bin)"
    argv[0] = exe
    try:
        p = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=SET["timeout"])
    except subprocess.TimeoutExpired:
        return "fail", f"test command timed out after {SET['timeout']}s: {command}"
    except OSError as exc:
        return "unavailable", f"could not run {command!r}: {exc}"
    out = (p.stdout + "\n" + p.stderr).strip().splitlines()
    if p.returncode == 5 and "pytest" in command:
        audit(f"NOTE: {HOOK}", "pytest collected no tests (exit 5) — treated as pass; write tests")
        return "pass", "\n".join(out[-30:])
    return ("pass" if p.returncode == 0 else "fail"), "\n".join(out[-30:])


# ------------------------------------------------------------------- policy --


def deletion_problems(session):
    """C2: any inventoried test file with fewer tests / more skips than at baseline, or gone —
    unless it was renamed (same count reappears in a new test file) or consolidated
    (parametrize/.each groups grew)."""
    base_inv = session.data.get("inventory", {})
    now_files = {}
    for root in session.data.get("roots", []):
        now_files.update(inventory(root))
    new_files = {p: c for p, c in now_files.items() if p not in base_inv}
    for path, base in base_inv.items():
        now = now_files.get(path)
        if now is None:
            if any(c["tests"] >= base["tests"] for c in new_files.values()):
                continue  # renamed/moved
            yield f"{rel(path)} — test file is gone (had {base['tests']} tests)"
        elif now["tests"] < base["tests"] and now["groups"] <= base["groups"]:
            yield f"{rel(path)} — tests went {base['tests']} → {now['tests']}"
        elif now["skips"] > base["skips"]:
            yield f"{rel(path)} — unconditional skip markers went {base['skips']} → {now['skips']}"


def touched_source(session):
    return [p for p, b in session.baseline.items() if b.get("is_source")]


# ------------------------------------------------------------------- events --


def on_pre(payload):
    path = payload.get("tool_input", {}).get("file_path")
    if not path or not SET["enabled"] or not is_source(path) or not in_project(path):
        return
    session = Session(payload["session_id"], HOOK)
    if path in session.baseline:
        return
    session.baseline[path] = {"is_source": True, "is_test": is_test_file(path)}
    root = str(project_root_for(path))
    notes = []
    if root not in session.data.setdefault("roots", []):
        session.data["roots"].append(root)
        session.data.setdefault("inventory", {}).update(inventory(root))
        notes += _baseline_run(session, root)
    session.save()
    warn_context("PreToolUse", "WARD (tests):", notes)


def _baseline_run(session, root):
    """Run the suite once per project at first touch; a red baseline disables the gate."""
    if not SET["baseline"] or not SET["run_on_stop"]:
        return []
    command = detect_command(Path(root))
    if not command:
        return []
    status, tail = run_tests(command, root)
    session.data.setdefault("baseline_status", {})[root] = status
    audit(f"BASELINE-{status.upper()}: {HOOK}", f"{command} (cwd {root})")
    if status == "fail":
        return [
            f"the test suite in {rel(root)} was ALREADY RED before your work (`{command}`). "
            f"The run gate is disabled for this session; tell the human. Last lines:\n{tail[-600:]}"
        ]
    return []


def on_post(_payload):
    return


def on_stop(payload):
    session = Session(payload["session_id"], HOOK)
    if not SET["enabled"] or not touched_source(session):
        return
    problems = list(deletion_problems(session))
    if problems:
        audit(f"BLOCK: {HOOK}", "; ".join(problems))
        if session.may_block_stop():
            block(
                "WARD BLOCK (tests) — tests were removed or skipped this session:\n"
                + "\n".join(f"- {p}" for p in problems)
                + "\nRestore them. If a test genuinely should go, stop and tell the human why instead."
            )
    if SET["run_on_stop"]:
        for root in session.data.get("roots", []):
            _run_gate_for(session, Path(root))
    session.clear_stop_blocks()


def _run_gate_for(session, root):
    if session.data.get("baseline_status", {}).get(str(root)) == "fail":
        audit(f"NOTE: {HOOK}", f"run gate skipped for {root}: suite was red at baseline")
        return
    command = detect_command(root)
    if not command:
        audit(f"NOTE: {HOOK}", f"no test command detected/configured for {root}; run gate skipped")
        return
    status, tail = run_tests(command, root)
    audit(f"{status.upper()}: {HOOK}", f"{command} (cwd {root})")
    if status == "unavailable":
        audit(f"WARN: {HOOK}", f"{tail}; run gate skipped")
        return
    if status == "fail" and session.may_block_stop():
        block(
            f"WARD BLOCK (tests) — `{command}` failed in {rel(root)}. Fix the code (not the tests) "
            f"before finishing. If the failure is pre-existing and unrelated, say so to the human.\n\n{tail}"
        )


if __name__ == "__main__":
    run_hook(HOOK, configure, (on_pre, on_post, on_stop))
