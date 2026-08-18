"""Shared tool discovery for the code-quality wards: which linter / formatter /
checker applies to a file, resolved to an executable. Tools are discovered,
never imposed — see ward-lint.py's docstring for the policy.

Used by ward-lint.py (check + format) and ward-complexity.py (measure the
FORMATTED shape so a formatter run by the sibling hook can't change NLOC
under the ratchet's feet).
"""

import os
import shlex
import shutil
from pathlib import Path

from wardlib import find_upward

LANG_BY_EXT = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "javascript",
    ".tsx": "javascript",
    ".mts": "javascript",
    ".cts": "javascript",
    ".go": "go",
    ".sh": "shell",
    ".bash": "shell",
    ".tf": "terraform",
    ".tfvars": "terraform",
}
ESLINT_CFG = [
    "eslint.config.js",
    "eslint.config.mjs",
    "eslint.config.cjs",
    "eslint.config.ts",
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.json",
    ".eslintrc.yml",
]
PRETTIER_CFG = [
    ".prettierrc",
    ".prettierrc.json",
    ".prettierrc.js",
    ".prettierrc.cjs",
    ".prettierrc.mjs",
    ".prettierrc.yml",
    ".prettierrc.yaml",
    ".prettierrc.toml",
    "prettier.config.js",
    "prettier.config.mjs",
    "prettier.config.cjs",
]

# Each tool: binary, command template ({file}/{dir} substituted), config files
# that must exist for it to run (empty = whenever installed; a callable = custom
# predicate on the file path), and the parser for check output.
TOOLS = {
    "python": {
        "check": {
            "bin": "ruff",
            "cmd": "ruff check --output-format json {file}",
            "needs": [],
            "parse": "ruff",
        },
        "format": {"bin": "ruff", "cmd": "ruff format {file}", "needs": "ruff-config"},
    },
    "javascript": {
        "check": {"bin": "eslint", "cmd": "eslint -f json {file}", "needs": ESLINT_CFG, "parse": "eslint"},
        "format": {"bin": "prettier", "cmd": "prettier --write {file}", "needs": PRETTIER_CFG},
    },
    "go": {
        "check": {"bin": "go", "cmd": "go vet {dir}", "needs": [], "parse": "generic"},
        "format": {"bin": "gofmt", "cmd": "gofmt -w {file}", "needs": []},
    },
    "shell": {
        "check": {
            "bin": "shellcheck",
            "cmd": "shellcheck -x -f json {file}",
            "needs": [],
            "parse": "shellcheck",
        },
        "format": {"bin": "shfmt", "cmd": "shfmt -w {file}", "needs": [".editorconfig"]},
    },
    "terraform": {
        "check": {
            "bin": "tflint",
            "cmd": "tflint --format json --chdir {dir}",
            "needs": [".tflint.hcl"],
            "parse": "tflint",
        },
        "format": {"bin": "terraform", "cmd": "terraform fmt {file}", "needs": []},
    },
}


def lang_of(path):
    return LANG_BY_EXT.get(os.path.splitext(path)[1].lower())


def find_bin(name, start):
    """Executable for `name`: nearest node_modules/.bin or .venv/bin walking up from
    `start` (a file or dir — monorepo packages carry their own), else PATH."""
    p = Path(start).resolve()
    if p.is_file():
        p = p.parent
    for d in [p, *p.parents]:
        for cand in (d / "node_modules" / ".bin" / name, d / ".venv" / "bin" / name):
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand)
    return shutil.which(name)


def _has_ruff_config(path):
    """ruff format is presumptuous on a black/yapf project: require ruff's own config."""
    if find_upward(path, "ruff.toml") or find_upward(path, ".ruff.toml"):
        return True
    pp = find_upward(path, "pyproject.toml")
    return bool(pp) and "[tool.ruff" in pp.read_text(errors="replace")


def applies(spec, path):
    needs = spec["needs"]
    if needs == "ruff-config":
        return _has_ruff_config(path)
    return not needs or any(find_upward(path, n) for n in needs)


def tool_for(lang, kind, path, overrides=None):
    """Resolved (argv, parser) for language/kind, or None. `overrides` is the
    [lint.<language>] config table: {check:…, format:…, parser:…}."""
    overrides = overrides or {}
    if overrides.get(kind):
        cmd, parser = overrides[kind], overrides.get("parser", "generic")
    else:
        spec = TOOLS.get(lang, {}).get(kind)
        if not spec or not applies(spec, path):
            return None
        exe = find_bin(spec["bin"], path)
        if not exe:
            return None
        cmd, parser = spec["cmd"].replace(spec["bin"], shlex.quote(exe), 1), spec.get("parse", "generic")
    argv = [a.format(file=path, dir=os.path.dirname(path) or ".") for a in shlex.split(cmd)]
    return argv, parser
