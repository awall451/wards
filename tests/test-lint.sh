#!/usr/bin/env bash
# ward-lint.py — linter ratchet, formatter, suppression guard, tool gating.
# shellcheck source=tests/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
H=ward-lint.py
for t in ruff eslint prettier shellcheck; do command -v $t >/dev/null || { echo "SKIP: $t not installed"; exit 0; }; done

echo "# python / ruff — ratchet"
P="$SCRATCH/mod.py"
printf 'import os\nimport sys\n\ndef f():\n    return 1\n' > "$P"          # 2× F401 pre-existing
expect "pre snapshot"                     0 $H "$(ev PreToolUse Edit "$P" s1)"
expect "unchanged -> legacy warn, exit 0" 0 $H "$(ev PostToolUse Edit "$P" s1)"
expect_out "legacy notice"                'pre-existing lint finding'
printf 'import os\nimport sys\nimport json\n\ndef f():\n    return 1\n' > "$P"   # 3× F401 = +1
expect "new F401 -> Post warns (soft)"     0 $H "$(ev PostToolUse Edit "$P" s1)"
expect_out "names rule + only the new one" 'will block at Stop.*F401 \+1 new — L3: `json`'
expect "…and Stop blocks it"              2 $H "$(ev Stop '' '' s1)"
printf 'import os\n\ndef f():\n    return 1\n' > "$P"                        # 1× F401 = improved
expect "improved -> 0"                    0 $H "$(ev PostToolUse Edit "$P" s1)"
printf 'import os\n\ndef f():\n    x = 1\n    return 1\n' > "$P"             # F841 new rule
expect "different new rule -> soft warn"  0 $H "$(ev PostToolUse Edit "$P" s1)"
expect_out "F841 named"                   'F841'
expect "Stop blocks F841"                 2 $H "$(ev Stop '' '' s1)"

echo "# fingerprint ratchet: swapping one violation for another of the same rule is still new"
printf 'import os\n\ndef f():\n    return 1\n' > "$P"          # baseline for s6: 1x F401(os)
expect "pre s6"                           0 $H "$(ev PreToolUse Edit "$P" s6)"
printf 'import sys\n\ndef f():\n    return 1\n' > "$P"         # still 1x F401 but a different one
expect "same count, different instance -> soft warn" 0 $H "$(ev PostToolUse Edit "$P" s6)"
expect_out "names sys"                    'sys'
expect "Stop blocks the swapped instance" 2 $H "$(ev Stop '' '' s6)"
printf 'import os\n\n\ndef f():\n    return 1\n' > "$P"       # original instance, extra blank line (line shift)
expect "same instance, lines shifted -> ok" 0 $H "$(ev PostToolUse Edit "$P" s6)"

echo "# suppressions"
printf 'import os  # noqa: F401\n\ndef f():\n    return 1\n' > "$P"
expect "added noqa -> block"              2 $H "$(ev PostToolUse Edit "$P" s1)"
expect_out "suppression message"          'suppression markers went 0 → 1'
printf 'def f():\n    return 1\n' > "$P"
expect "clean -> 0"                       0 $H "$(ev PostToolUse Edit "$P" s1)"
printf 'def f():\n    """Docs mention `# noqa` and "# type: ignore" as examples."""\n    return 1\n' > "$P"
expect "markers inside strings/docs -> not counted" 0 $H "$(ev PostToolUse Edit "$P" s1)"
printf 'MARKERS = ["# noqa", "# type: ignore"]\n' > "$P"
expect "markers in string literals -> not counted" 0 $H "$(ev PostToolUse Edit "$P" s1)"
printf 'import os  # type: ignore\n' > "$P"
expect "real trailing marker -> block"    2 $H "$(ev PostToolUse Edit "$P" s1)"
printf 'def f():\n    return 1\n' > "$P"
expect "clean again -> 0"                 0 $H "$(ev PostToolUse Edit "$P" s1)"

echo "# formatter gating (ruff format only with config)"
printf 'def f( a ):\n    return   a\n' > "$P"
expect "no ruff.toml -> not formatted"    0 $H "$(ev PostToolUse Edit "$P" s1)"
check "untouched" grep -q 'f( a )' "$P"
printf 'line-length = 100\n' > "$SCRATCH/ruff.toml"
expect "with ruff.toml -> formatted"      0 $H "$(ev PostToolUse Edit "$P" s1)"
expect_out "re-read notice"               'auto-formatted'
check "file reformatted" grep -q 'def f(a):' "$P"
mkdir -p "$SCRATCH/.wards"; printf '[lint]\nformat = false\n' > "$SCRATCH/.wards/config.toml"
printf 'def g( a ):\n    return   a\n' > "$P"
expect "config format=false -> untouched" 0 $H "$(ev PostToolUse Edit "$P" s1)"
check "format disabled" grep -q 'g( a )' "$P"
rm -f "$SCRATCH/.wards/config.toml" "$SCRATCH/ruff.toml"

echo "# javascript / eslint + prettier"
J="$SCRATCH/app.js"
printf 'const a = 1;\n' > "$J"
expect "no eslint config -> silent"       0 $H "$(ev PostToolUse Write "$J" s2)"
echo '{"type":"module"}' > "$SCRATCH/package.json"
cat > "$SCRATCH/eslint.config.js" <<'EOC'
export default [{ files: ["**/*.js"], rules: { "no-unused-vars": "error", "eqeqeq": "warn" } }];
EOC
printf 'export function f(x){ return x; }\n' > "$J"
expect "pre (clean)"                      0 $H "$(ev PreToolUse Edit "$J" s3)"
printf 'const a = 1;\nexport function f(x){ return x; }\n' > "$J"
expect "new no-unused-vars -> soft warn"  0 $H "$(ev PostToolUse Edit "$J" s3)"
expect_out "rule id"                      'no-unused-vars'
expect "Stop blocks eslint error"         2 $H "$(ev Stop '' '' s3)"
printf 'export function f(x){ return x == 1; }\n' > "$J"
expect "eslint warn-severity -> exit 0"   0 $H "$(ev PostToolUse Edit "$J" s3)"
expect_out "eqeqeq surfaced as context"   'eqeqeq'
printf 'export function f(x){ return x; }\n// eslint-disable-next-line\nconst z=1;\n' > "$J"
expect "eslint-disable added -> block"    2 $H "$(ev PostToolUse Edit "$J" s3)"
echo '{}' > "$SCRATCH/.prettierrc"
printf 'export function f( x ){return x}\n' > "$J"
expect "prettier formats"                 0 $H "$(ev PostToolUse Edit "$J" s3)"
check "prettier ran" grep -q 'function f(x)' "$J"
echo "# shell / shellcheck"
S="$SCRATCH/run.sh"
printf '#!/bin/sh\necho ok\n' > "$S"
expect "pre"                              0 $H "$(ev PreToolUse Edit "$S" s4)"
printf '#!/bin/sh\necho %s\n' "\$1" > "$S"                # SC2086 = info -> warning-severity
expect "SC2086 (info) -> warn only"       0 $H "$(ev PostToolUse Edit "$S" s4)"
expect_out "SC2086 surfaced"              'SC2086'
printf '#!/bin/sh\nif [ %s = ]; then echo; fi\n' "\$x" > "$S"  # error/warning-level findings
expect "shellcheck error -> block"        2 $H "$(ev PostToolUse Edit "$S" s4)"

echo "# reviewer findings"
mkdir -p "$SCRATCH/pkg"; printf 'from .core import main\n' > "$SCRATCH/pkg/__init__.py"
expect "F401 in __init__.py is idiomatic -> not a finding" 0 $H "$(ev PostToolUse Write "$SCRATCH/pkg/__init__.py" s7)"
expect "Stop clean for __init__ re-export" 0 $H "$(ev Stop '' '' s7)"
printf 'def f(:\n' > "$P"
expect "syntax error blocks at Post (never transient)" 2 $H "$(ev PostToolUse Edit "$P" s1)"
printf 'def f():\n    return 1\n' > "$P"
S2="$SCRATCH/lib.sh"; printf 'CONF_DIR=/etc/x\n' > "$S2"
expect "pre lib.sh"                       0 $H "$(ev PreToolUse Edit "$S2" s8)"
printf 'CONF_DIR=/etc/x\nOTHER=1\n' > "$S2"
expect "shellcheck warning (SC2034) -> warn not block" 0 $H "$(ev PostToolUse Edit "$S2" s8)"
expect "Stop: warnings never block"       0 $H "$(ev Stop '' '' s8)"
printf 'import os  # NOQA\n' > "$P"
expect "uppercase NOQA counted as suppression -> block" 2 $H "$(ev PostToolUse Edit "$P" s1)"
printf 'x = 1  # pyright: ignore[reportX]\n' > "$P"
expect "pyright: ignore counted -> block"  2 $H "$(ev PostToolUse Edit "$P" s1)"
printf 'def f():\n    return 1\n' > "$P"
mkdir -p "$SCRATCH/blackproj"; printf '[tool.black]\nline-length = 120\n' > "$SCRATCH/blackproj/pyproject.toml"
printf "def f( a ):\n    return   'x'\n" > "$SCRATCH/blackproj/m.py"
expect "pyproject without [tool.ruff] -> ruff format does NOT run" 0 $H "$(ev PostToolUse Write "$SCRATCH/blackproj/m.py" s9)"
check "black project untouched" grep -q "f( a )" "$SCRATCH/blackproj/m.py"
cat > "$SCRATCH/eslint.config.js" <<'EOC'
export default [{ ignores: ["gen/**"] }, { files: ["**/*.js"], rules: { "no-unused-vars": "error" } }];
EOC
mkdir -p "$SCRATCH/gen"; printf 'const a = 1;\n' > "$SCRATCH/gen/g.js"
expect "eslint-ignored file: no crash, no findings" 0 $H "$(ev PostToolUse Write "$SCRATCH/gen/g.js" s10)"
check "no CRASH audited"                  bash -c "! grep -q 'CRASH: ward-lint' '$WARDS_AUDIT_LOG'"

echo "# stop sweep + misc"
printf 'def f():\n    return 1\n' > "$P"; printf 'export function f(x){ return x; }\n' > "$J"; printf '#!/bin/sh\necho ok\n' > "$S"
expect "stop clean s1"                    0 $H "$(ev Stop '' '' s1)"
printf 'import os\nimport sys\nimport json\n' > "$P"
expect "stop with new violations s1"      2 $H "$(ev Stop '' '' s1)"
expect "unknown ext -> silent"            0 $H "$(ev PostToolUse Write "$SCRATCH/notes.txt" s1)"
finish
