#!/usr/bin/env bash
# ward-typecheck.py — Stop gate on NEW type errors (ratchet vs first-touch baseline).
# shellcheck source=tests/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
H=ward-typecheck.py
command -v mypy >/dev/null || { echo "SKIP: mypy not installed"; exit 0; }
P="$SCRATCH/pyproj"; mkdir -p "$P"; printf '[mypy]\n' > "$P/mypy.ini"
printf 'def f(x: int) -> str:\n    return x\n' > "$P/legacy.py"          # 1 pre-existing error
printf 'def g(x: int) -> int:\n    return x\n' > "$P/mod.py"

echo "# baseline on first touch; legacy error tolerated"
expect "pre -> baseline"                  0 $H "$(ev PreToolUse Edit "$P/mod.py" s1)"
check "baseline=1" grep -q "baseline 1 error" "$WARDS_AUDIT_LOG"
expect "stop, no new errors -> 0"         0 $H "$(ev Stop '' '' s1)"
printf 'def g(x: int) -> int:\n    return "no"\n' > "$P/mod.py"
expect "new error -> block"               2 $H "$(ev Stop '' '' s1)"
expect_out "names file"                   'mod.py'
expect_out "counts new only"              '1 new type error'
printf 'def g(x: int) -> int:\n    return x\n' > "$P/mod.py"
expect "fixed -> 0"                       0 $H "$(ev Stop '' '' s1)"
printf 'def f(x: int) -> str:\n    return str(x)\n' > "$P/legacy.py"
expect "legacy fixed too -> 0"            0 $H "$(ev Stop '' '' s1)"

echo "# no checker configured -> silent"
Q="$SCRATCH/plain"; mkdir -p "$Q"; printf 'x = 1\n' > "$Q/a.py"
expect "pre plain"                        0 $H "$(ev PreToolUse Edit "$Q/a.py" s2)"
expect "stop plain"                       0 $H "$(ev Stop '' '' s2)"

echo "# baseline missing (checker failed at first touch) -> never block"
R="$SCRATCH/nobase"; mkdir -p "$R/.wards"; printf '[mypy]\n' > "$R/mypy.ini"
printf '[types]\ncommand = "definitely-not-a-binary --flag"\n' > "$R/.wards/config.toml"
printf 'def g(x: int) -> int:\n    return "no"\n' > "$R/m.py"
expect "pre (checker missing)"            0 $H "$(ev PreToolUse Edit "$R/m.py" s5)"
expect "stop -> 0, no baseline"           0 $H "$(ev Stop '' '' s5)"
printf '[types]\ncommand = "mypy ."\n' > "$R/.wards/config.toml"
python3 - "$WARDS_STATE_DIR" <<'PY'
import json,sys,pathlib
for f in pathlib.Path(sys.argv[1]).glob("s5.types.json"):
    d=json.loads(f.read_text()); d["command"]="mypy ."; d["baseline_errors"]=None; f.write_text(json.dumps(d))
PY
expect "stop with errors but baseline None -> 0" 0 $H "$(ev Stop '' '' s5)"
check "audit says no baseline"            grep -q "no baseline" "$WARDS_AUDIT_LOG"

echo "# config command / disabled"
mkdir -p "$P/.wards"; printf '[types]\ncommand = ""\n' > "$P/.wards/config.toml"
printf 'def g(x: int) -> int:\n    return "no"\n' > "$P/mod.py"
expect "pre s3 (command disabled)"        0 $H "$(ev PreToolUse Edit "$P/mod.py" s3)"
expect "stop s3 -> no run"                0 $H "$(ev Stop '' '' s3)"
rm -r "$P/.wards"

if command -v tsc >/dev/null; then
  echo "# typescript"
  T="$SCRATCH/tsproj"; mkdir -p "$T"
  echo '{"compilerOptions":{"strict":true,"noEmit":true},"include":["*.ts"]}' > "$T/tsconfig.json"
  printf 'export const n: number = 1;\n' > "$T/a.ts"
  expect "ts pre"                         0 $H "$(ev PreToolUse Edit "$T/a.ts" s4)"
  printf 'export const n: number = "x";\n' > "$T/a.ts"
  expect "ts new error -> block"          2 $H "$(ev Stop '' '' s4)"
  expect_out "TS code"                    'TS2322'
fi
finish
