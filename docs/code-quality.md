# Code-quality wards

The cloud wards answer "what can the agent *do* to my account?" These answer a quieter question: "what does the agent *leave behind* in my repo?" AI agents write code that passes the tests and is often shaped badly — a 40-branch function, a silenced linter, a deleted test that was inconvenient. Nobody reviews shape and hygiene as carefully as behavior, so the wards do.

Same principle as the rest of wards: **the harness enforces cheaply and produces feedback; CI enforces hard.** Etiquette (CLAUDE.md) shapes most behavior; the hooks catch the rest; a CI job (see [`ci.md`](ci.md)) is the layer that holds when neither is present.

## The six hooks

| Hook | Events | Enforces | Blocks on |
|---|---|---|---|
| `ward-complexity.py` | Pre/Post `Edit\|Write`, Stop | code shape: cyclomatic complexity, function length, parameter count, file length (via `lizard`, ~20 languages) | new or worsened function past the "high" band; growth of any legacy high-risk function |
| `ward-lint.py` | Pre/Post `Edit\|Write`, Stop | the project's **own** linter + formatter (ruff, eslint/prettier, shellcheck/shfmt, gofmt/go vet, tflint/terraform fmt); suppression comments | new lint findings (error severity); any increase in `noqa` / `type: ignore` / `eslint-disable` / `nolint` … |
| `ward-git-guard.py` | Pre `Bash` | git hygiene | `--no-verify`, force-push, `reset --hard`, `clean -f`, whole-tree discards, `branch -D`, `stash drop`, history rewrites, global config, remote removal; non-Conventional-Commits messages |
| `ward-tests.py` | Pre `Edit\|Write`, Stop | tests ran and passed before "done"; tests not deleted or skipped | test command exit ≠ 0; fewer test functions or more skip markers than at first touch |
| `ward-typecheck.py` | Pre `Edit\|Write`, Stop | type checker (tsc / pyright / mypy / go vet) | any type error not present at the session's first source-file touch |
| `ward-tamper-guard.py` | Pre `Edit\|Write\|MultiEdit\|NotebookEdit`, Pre `Bash` | the wards themselves stay human-owned | Edit/Write of `.claude/settings*.json`, `.claude/hooks/*`, `.wards/*`, `~/.claude/settings.json`, `.git/hooks/`, `.git/config`, `.pre-commit-config.yaml`; any Bash segment naming those paths (or `rm -rf .git`) unless it is a known **read-only** command with no file redirection (allow-list: cat/ls/grep/diff/jq/git diff|log|show/…; `find` without `-delete`/`-exec`, `sed`/`awk` without in-place); `pre-commit uninstall`, `core.hooksPath`. Quote-splitting (`.wa""rds`) normalised; heredoc bodies ignored. `.wards/config.toml` is fingerprinted at first sight and any mid-session change is audited and announced to the agent. Layer 4 belt-and-braces: `permissions.deny` `Edit(./.claude/**)`, `Edit(./.wards/**)` (Claude Code's `Edit(...)` path rules cover Write/NotebookEdit too). |

All six share `wardlib.py` (config, session state, audit log) and the same **ratchet** semantics.

## The ratchet (why nothing here screams about legacy code)

Legacy code exists. A ward that blocks on every pre-existing violation trains the agent to ignore it — or worse, to refactor things nobody asked it to touch. So every rule is about *direction*, keyed to the state of the file when the agent **first touched it this session** (`PreToolUse` snapshot):

| Finding is… | Result |
|---|---|
| new, or worse than the baseline | **block** — code shape immediately (exit 2, the edit is on disk but the agent must fix it before proceeding); lint findings are *reported* at PostToolUse ("will block at Stop") and **enforced at Stop**, because mid-refactor states (an import added before its use) are normal between edits. Suppressions and syntax errors block immediately — never transient. |
| pre-existing and unchanged / improved | **warn once** per function/rule per session (`additionalContext`): "not yours to clean unasked; add no more; tell the human" |
| moderate-band (complexity) / warning-severity (lint, incl. shellcheck warnings) | audit-logged or surfaced as context, never blocking |

The `Stop` hook re-checks every touched file, so an agent cannot declare "done" with a violation outstanding. Consecutive Stop blocks are capped (3 per hook, `WARDS_STOP_BLOCK_LIMIT`) so a hook can never trap the agent in a loop — after the cap it lets the stop through and audit-logs that it did. Files outside `$CLAUDE_PROJECT_DIR` (scratch scripts in `/tmp`) are not judged.

Everything — block, warn, note, pass — is appended to `~/.wards/audit.log` alongside the cloud-command audit. Tab-separated: timestamp, verdict, detail; multi-line commands are flattened so one entry is one line.

**A broken ward is loud, not silent.** A crash inside any hook — including a bad config value — is audit-logged as `CRASH:` and surfaced to the user (exit 1, non-blocking) with the hook name; a missing `lizard` injects "ward INACTIVE" into the agent's context; malformed thresholds fall back to defaults with an audit warning; corrupt session state resets itself with an audit note; state writes are atomic. Session state lives in `~/.wards/state/` and is pruned after 14 days (`WARDS_STATE_TTL_DAYS`).

## Code shape (`ward-complexity.py`)

Bands follow SonarSource / McCabe conventions. Upper bounds, tunable:

| metric | ok | moderate | high | very high |
|---|---|---|---|---|
| `ccn` cyclomatic complexity | ≤ 10 | 11–20 | 21–50 | 51+ |
| `length` function NLOC | ≤ 50 | 51–100 | 101–200 | 201+ |
| `params` parameter count | ≤ 4 | 5–6 | 7–10 | 11+ |
| `file_lines` whole file | ≤ 400 | 401–800 | 801–1500 | 1501+ |

Policy per (function, metric): moderate is acceptable when the logic warrants it (the agent is told to *aim* for ok); high/very-high is blocked for new or worsened code; legacy high/very-high is warned once and may not grow. Details that keep this fair:

- Python `self`/`cls` don't count as parameters.
- `file_lines` is softer: a large legacy file may still grow within its band (warn once, "prefer a new module"); it blocks only when a *new* file lands in the high band or a legacy file crosses into a higher band. Function length stays strict.
- Moving or renaming a legacy high-risk function (into a new module — the refactor we recommend) is recognised: the baseline lookup falls back to the same function name in *any* file touched this session, so it reads as legacy, not new. Growing it there still blocks.
- When the project has a formatter, shape is measured on a **formatted temp copy** at both Pre and Post, so the sibling lint hook reformatting the file can't turn a comment into "+105 lines".
- `ignore` entries match whole path components (`build` ≠ `builder/`) or, when they contain `.`/`/`, path substrings. Defaults add `migrations`, `alembic/versions`, `.generated.`.
- Nesting depth is deliberately absent — `lizard`'s nesting extension is unreliable across languages; CCN catches most arrow code.

**Escape hatch — human-only:** `.wards/complexity-allow.txt` next to `.wards/config.toml`, one function name per line. The agent is told never to edit it: if a function is genuinely irreducible, the agent's job is to stop and *say so*; the human's job is to decide. The allow-list diff in the PR is the paper trail.

## Lint, format, suppressions (`ward-lint.py`)

Tools are **discovered, never imposed.** A linter or formatter runs only if it is installed (PATH, `node_modules/.bin`, `.venv/bin`) and — for tools whose defaults would be presumptuous — the project already carries its config:

| language | check | format | gate |
|---|---|---|---|
| python | `ruff check` | `ruff format` | format needs `ruff.toml` / `.ruff.toml` / `pyproject.toml` |
| js / ts | `eslint -f json` | `prettier --write` | eslint needs `eslint.config.*` / `.eslintrc*`; prettier needs `.prettierrc*` |
| go | `go vet` | `gofmt -w` | — |
| shell | `shellcheck -x -f json` | `shfmt -w` | shfmt needs `.editorconfig` |
| terraform | `tflint --format json` | `terraform fmt` | tflint needs `.tflint.hcl` |

No tool for a language → nothing happens (audit-logged once). Override any command per language in `[lint.<language>]`.

- **Findings ratchet by fingerprint** (`rule|message`, line numbers dropped): a finding whose fingerprint count exceeds the first-touch baseline is new → block if error-severity, warn if warning-severity. Fixing one `F401` and adding another is still caught (different message); an unchanged finding shifted by an edit above it is not. Pre-existing findings are summarized once.
- **Format-on-save:** the formatter runs after every edit; if it changed the file the agent is told to re-read before editing again (otherwise its next `Edit` would miss).
- **Findings the ecosystem calls idiomatic are not findings:** `F401` in `__init__.py` (re-exports). shellcheck *warnings* (SC2034 unused var in a sourced lib, …) surface but never block; only shellcheck errors do. Tool binaries are resolved from the nearest `node_modules/.bin` / `.venv/bin` above the edited file (monorepo packages), then PATH.
- **Suppressions:** any increase in `# noqa`, `# type: ignore`, `# pragma: no cover`, `# nosec`, `# pylint: disable`, `# shellcheck disable`, `// eslint-disable*`, `// @ts-ignore`, `// @ts-expect-error`, `// nolint`, `@SuppressWarnings`, `#[allow(` … is blocked. Markers inside strings, docstrings and markdown-ish lines (docs *about* suppressions) are not counted. Fix the finding; if a suppression is genuinely right, tell the human.

## Git (`ward-git-guard.py`)

Sibling of `ward-az-guard.sh`. Two nets. **Structured:** every `git …` invocation is parsed — segments split on `; & | && ||` *and newlines* (quote-aware, so a commit body containing `&` doesn't split), redirections stripped, `sh -c "…"` / `bash -c` / `eval "…"` bodies re-scanned, `$(git …)`/backtick starts, quoted or escaped `git`, `git-filter-repo` binaries, git's `-c key=val` global options inspected. **Coarse:** when the command reaches a shell/interpreter indirectly (`subprocess`, `execSync`, `$IFS`, `xargs`, `which git`), the deadliest patterns are matched anywhere in the quote-normalised text. Heredoc bodies are data and excluded from both. Blocked:

- **hook bypass** — `--no-verify` (and git's unambiguous abbreviations `--no-veri`), `commit -n`, `-c core.hooksPath=…`, `config core.hooksPath` / `alias.*`
- **history rewrite** — `push --force`/`-f`/`--force-with-lease`/`--force-if-includes`/`+refspec`/`--mirror`/`--prune`, `filter-branch`, `filter-repo`, `reflog expire|delete`, `gc --prune`, `prune`, `update-ref` (all), `rebase -i`/`--root`
- **work destruction** — `reset --hard`, `clean -f`, `checkout`/`restore` of the whole tree or any *directory* (`.`, `HEAD .`, `-- src/`), `checkout -f`, `switch -C|-f|--discard-changes`, `stash drop|clear`, `branch -D|-df|-f|-M`, `rm -r`, `worktree remove -f`/`prune`, `push --delete` / `push :branch`
- **identity / config** — `config --global|--system` *writes* (`--list`/`--get` pass), `remote remove|rm|set-url|rename`
- **commit messages** — first line must be Conventional Commits `type(scope)?: subject`, subject ≤ 72 chars (`[git] conventional_commits = false` to disable; `types = [...]` to customize). Parsed from `-m "…"`, `-am"…"`, `--message=`, `-m "$(cat <<'EOF' … EOF)"` (Claude Code's own style; the heredoc nearest the commit), `-F - <<'EOF'`, and `echo "…" | git commit -F -`. Messages that cannot be known statically (`-F file`, editor, `$MSG`, `$(some-command)`) and git's own shapes (`Merge …`, `Revert "…"`, `fixup!`, `squash!`, `Initial commit`) pass.

Reads and ordinary work (`status`, `log`, `add`, `commit -m 'feat: …'`, `push`, `rebase main`, `stash`/`pop`, `branch -d`, `restore --staged`, `checkout -- file.py`, `commit --amend`, `remote add`, `worktree add/remove`) pass. Mutating commands — allowed or blocked — are audit-logged; reads are not.

## Tests (`ward-tests.py`)

- **Baseline at first touch:** the suite runs once when the agent first touches a source file in a project. If it was **already red**, the run gate is disabled for that project this session — the agent is told (`additionalContext`: "suite was already red before your work — tell the human"), and it is audit-logged as `BASELINE-FAIL`. The agent is neither held hostage to a broken suite nor able to blame one it broke.
- **Run gate (B4):** on Stop, for each project touched this session (a monorepo session touching two projects runs both), the test command runs from that project's root. Non-zero exit blocks with the last 30 lines; `pytest` exit 5 (no tests collected) passes with an audit note. Auto-detected: `pytest -q` (pyproject/pytest.ini/conftest.py/tests dir), `npm test --silent` (package.json `scripts.test`), `go test ./...`, `cargo test --quiet`; binaries resolved via `.venv/bin` / `node_modules/.bin` / PATH — a missing binary is audited and skipped, never blocked. `[tests] command = ""` disables; `run_on_stop = false` keeps only the ratchet below; `baseline = false` skips the first-touch run.
- **Deletion/skip ratchet (C2):** at first touch the ward inventories every test file in the project (`tests/`, `test/`, `spec/`, `__tests__/`, `test_*.py`, `*_test.go`, `*.test.js`, `*.spec.ts` …) — number of test functions and *unconditional* skip markers (`pytest.mark.skip`/`xfail`, `it.skip`/`xit`, `t.Skip`, `#[ignore]`; `skipif` is not a skip). On Stop, any inventoried file with fewer tests or more skips — or gone, however it went (Edit, `rm`, `git rm`, `mv`) — blocks: "restore them, or stop and tell the human why a test should go." Renames (the same test count reappears in a new file) and parametrize/`.each` consolidation are tolerated.

The Stop hook's timeout is raised in `settings.json` (660 s); tune `[tests] timeout` to match.

## Types (`ward-typecheck.py`)

Baseline is taken lazily on the first source-file touch of the session (running the checker once; PreToolUse hook timeout 360 s); if that run fails or times out there is no baseline and the Stop check only audit-logs — it never blocks on a guess. Otherwise Stop runs it again and blocks on errors **not in the baseline** (fingerprint = file *basename* + message: line numbers dropped so unrelated edits don't make old errors look new, directory dropped so moving a file with a legacy error doesn't either). Go's `vet:`-prefixed lines are understood. Auto-detected: `tsc --noEmit` (tsconfig.json), `pyright` (pyrightconfig.json / `[tool.pyright]`), `mypy` (mypy.ini / `[tool.mypy]`), `go vet ./...` (go.mod). Only if the binary is installed.

## Config — `.wards/config.toml`

Human-owned; every key optional; env vars `WARDS_<SECTION>_<KEY>` override individual keys. Resolution is **nearest `.wards/config.toml` upward from the edited file** (for Stop: from the first file touched this session), so a subproject's config wins over the workspace root's — one Claude Code session at a monorepo root can drive several projects with different rules.

```toml
[complexity]
ccn = [10, 20, 50]           # upper bound of ok / moderate / high
length = [50, 100, 200]
params = [4, 6, 10]
file_lines = [400, 800, 1500]
ignore = ["node_modules", "vendor", "dist", "build", ".min."]

[lint]
enabled = true
format = true                # false = never auto-format
[lint.python]                # per-language overrides; {file} / {dir} substituted
check = "ruff check --output-format json {file}"
format = "ruff format {file}"
parser = "ruff"              # ruff | eslint | shellcheck | tflint | generic

[git]
enabled = true
conventional_commits = true
types = ["feat","fix","docs","style","refactor","perf","test","build","ci","chore","revert"]
subject_max = 72

[tests]
command = "pytest -q"        # overrides auto-detect; "" disables the run
run_on_stop = true
timeout = 600
test_paths = ["tests", "test", "spec", "__tests__"]

[types]
command = "pyright src/"     # overrides auto-detect; "" disables
timeout = 300

[tamper]
enabled = true               # false only where a human wants the agent maintaining the hooks (the wards repo itself)
extra_paths = []             # more path fragments the agent may not write
```

Global knobs (env only): `WARDS_AUDIT_LOG` (default `~/.wards/audit.log`), `WARDS_STATE_DIR` (`~/.wards/state`), `WARDS_STOP_BLOCK_LIMIT` (3).

## Install

```bash
pipx install lizard                       # ward-complexity
# then whatever the project already uses: ruff, eslint+prettier, shellcheck, shfmt, tflint,
# pytest / npm test / go test, tsc / pyright / mypy — the wards wrap them, they don't ship them.
```

Copy `claude/hooks/*` (including `wardlib.py` and `wardtools.py`) into the project's `.claude/hooks/` (`chmod +x`), and merge `claude/settings.json`'s `hooks` and `permissions.deny` blocks. Per-hook `timeout`s are set explicitly (Stop tests 660 s, Stop lint 600 s, typecheck 360 s …); Claude Code's default is 600 s. Settings changes are picked up live. If `lizard` is missing the complexity ward does not fail silently — it injects "complexity ward INACTIVE" into the agent's context and audit-logs it.

## Testing the wards themselves

`tests/run.sh` — bash simulation harness that feeds hook payloads to each script and asserts exit codes / output (~470 assertions across nine suites, including `ward-az-guard.sh`, the tamper guard and `wardlib`; the git-guard suite alone covers 170+ commands, most of them contributed by an adversarial red-team pass). The hooks were also exercised under a real headless `claude -p` session: complexity block → Stop blocks → stop-limit release; suppression block; git-guard commit-message block and retry — all behaved as designed. Wired into pre-commit for changes under `claude/hooks/` or `tests/`. Each suite skips itself if its tool (ruff, eslint, pytest, mypy …) is missing.

## Honest limits — and what holds below them

The git and tamper guards inspect shell *strings*. Against an agent that is actively trying, string inspection cannot win: `G=git; $G push --force`, `$(printf …)`-built paths, or a script written to disk and executed later all evade any regex (a red-team pass reproduced exactly these before the guards were hardened; the cheap ones are closed, the structural ones are not closable). Treat these two guards as what they are — **mistake-catchers with an audit trail** — and put the enforcement that must hold below the agent:

- **Repo:** server-side branch protection (no force-push, required PR checks, required reviews) and CI running the same checks (see [`ci.md`](ci.md)). A `.wards/` or `.claude/` change shows up in the PR diff.
- **Files:** Claude Code's `permissions.deny` `Edit(...)` rules (harness-enforced) and, where it matters, a filesystem sandbox / read-only mounts for `.claude/`, `.wards/`, `.git/hooks/`.
- **Visibility:** everything the wards decide is in `~/.wards/audit.log`; a config change mid-session is announced. Tampering that slips past becomes visible, not silent.

The code-shape, lint, tests and types wards are different in kind: they judge the *result on disk* at Stop, so evading the per-edit check only defers the block.

## Why PostToolUse and not PreToolUse?

Because the code doesn't exist yet. A pre-hook sees `new_string`, not the resulting function, and cannot measure it. Pre-hooks are for "this verb is forbidden" (git guard, az guard); quality is judged after the write. `PreToolUse` on file tools is used here only to snapshot the baseline.

## Not hookable — etiquette + review

SOLID, naming quality, meaningful abstractions, tests-first, small PRs, design before code. `CLAUDE.md` states them; the human reviews for them. Hooks catch shape; humans catch meaning.

## Roadmap

Duplication (`jscpd`) and diff-coverage as Stop hooks once a project needs them (CI recipes in [`ci.md`](ci.md) today); import/layer boundaries (`import-linter`, `dependency-cruiser`); dependency-add gate (`npm i` / `pip install` → confirm); nesting depth with a better tool than `lizard -END`.
