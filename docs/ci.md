# CI backstop for the code-quality wards

The hooks only exist inside Claude Code. The layer that holds regardless of tool — a human, Cursor, a rogue script — is CI. Run the same checks there; the hooks then become the fast feedback loop and CI the wall.

Everything below is copy-paste and free.

## Complexity / shape (lizard)

```bash
pipx install lizard
lizard -C 20 -L 100 -a 6 --warnings_only src/     # CCN > 20, NLOC > 100, params > 6 → nonzero exit
```

Ratcheting in CI (fail only on functions that got worse vs. the base branch): `lizard` has no baseline mode. Simple approach — run `lizard --csv` on both `HEAD` and the merge-base and diff the per-function CCNs; a 20-line script, same idea as `ward-complexity.py`.

## Lint / format / types / tests

Use the project's own tools in check mode; nothing wards-specific:

```bash
ruff check . && ruff format --check .
eslint . && prettier --check .
shellcheck **/*.sh
go vet ./... && test -z "$(gofmt -l .)"
tsc --noEmit  |  pyright  |  mypy .
pytest -q  |  npm test  |  go test ./...
```

## Duplication (jscpd, multi-language)

```bash
npx jscpd --min-tokens 50 --threshold 3 --reporters console --exitCode 1 src/
```

`--threshold` = max % duplicated lines before failure. Agents copy-paste instead of extracting; this is the check that notices.

## Diff coverage (new/changed lines only)

Global coverage % punishes legacy code and rewards nothing. Cover the diff instead:

```bash
# python
pytest --cov=src --cov-report=xml
pipx install diff-cover
diff-cover coverage.xml --compare-branch=origin/main --fail-under=80
```

JS: `c8`/`nyc` → lcov → `diff-cover lcov.info --compare-branch=origin/main --fail-under=80` (diff-cover reads lcov too). Go: `go test -coverprofile` + a diff filter.

## Suppression / cheat guards

```bash
# fail if the PR adds suppression comments (same regex family as ward-lint.py)
git diff origin/main...HEAD | grep -E '^\+.*(# ?noqa|# ?type: ?ignore|eslint-disable|@ts-ignore|@ts-expect-error|# ?nosec|//\s*nolint)' && exit 1
# fail if the PR removes tests
git diff origin/main...HEAD --stat -- 'tests/' '**/*_test.go' '**/*.test.*' | grep -E ' \| +[0-9]+ [-]+' && exit 1   # crude; prefer counting test fns
```

## Azure DevOps pipeline sketch

```yaml
steps:
  - script: pipx install lizard ruff && npm ci
    displayName: tools
  - script: lizard -C 20 -L 100 -a 6 --warnings_only src/
    displayName: shape
  - script: ruff check . && ruff format --check .
    displayName: lint
  - script: pytest -q --cov=src --cov-report=xml && pipx run diff-cover coverage.xml --compare-branch=origin/main --fail-under=80
    displayName: tests + diff coverage
  - script: npx jscpd --threshold 3 --exitCode 1 src/
    displayName: duplication
```

GitHub Actions is the same commands under `run:`.
