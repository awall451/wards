#!/usr/bin/env bash
# ward-tests.py — Stop gate runs project tests; no test deletion / skipping.
# shellcheck source=tests/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
H=ward-tests.py
command -v pytest >/dev/null || { echo "SKIP: pytest not installed"; exit 0; }
PROJ="$SCRATCH/proj"; mkdir -p "$PROJ/tests"; cd "$PROJ" || exit 1
printf 'def add(a, b):\n    return a + b\n' > "$PROJ/calc.py"
printf 'from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n\ndef test_add_neg():\n    assert add(-1, 1) == 0\n' > "$PROJ/tests/test_calc.py"
touch "$PROJ/conftest.py"
export PYTHONPATH="$PROJ" PYTHONDONTWRITEBYTECODE=1

echo "# no source touched -> stop is free"
expect "stop, nothing touched"            0 $H "$(ev Stop '' '' s0)"

echo "# green path"
expect "pre source"                       0 $H "$(ev PreToolUse Edit "$PROJ/calc.py" s1)"
expect "post is a no-op"                  0 $H "$(ev PostToolUse Edit "$PROJ/calc.py" s1)"
expect "stop -> tests run, pass"          0 $H "$(ev Stop '' '' s1)"
check "audit PASS" grep -q "PASS: tests" "$WARDS_AUDIT_LOG"
echo "# red path"
printf 'def add(a, b):\n    return a - b  # broken on purpose\n' > "$PROJ/calc.py"
expect "stop -> tests fail -> block"      2 $H "$(ev Stop '' '' s1)"
expect_out "names command"                'pytest -q'
expect_out "shows failure tail"           'assert'
printf 'def add(a, b):\n    return a + b\n' > "$PROJ/calc.py"
expect "fixed -> stop ok"                 0 $H "$(ev Stop '' '' s1)"

echo "# test deletion / skip ratchet"
T="$PROJ/tests/test_calc.py"
expect "pre test file (snapshot 2 tests)" 0 $H "$(ev PreToolUse Edit "$T" s1)"
printf 'from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n' > "$T"
expect "one test removed -> block"        2 $H "$(ev Stop '' '' s1)"
expect_out "counts"                       'tests went 2 → 1'
printf 'import pytest\nfrom calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n\n@pytest.mark.skip\ndef test_add_neg():\n    assert add(-1, 1) == 0\n' > "$T"
expect "test restored but skipped -> block" 2 $H "$(ev Stop '' '' s1)"
expect_out "skip counted"                 'skip markers went 0 → 1'
printf 'from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n\ndef test_add_neg():\n    assert add(-1, 1) == 0\n\ndef test_zero():\n    assert add(0, 0) == 0\n' > "$T"
expect "restored + added -> ok"           0 $H "$(ev Stop '' '' s1)"
rm "$T"
expect "test file deleted -> block"       2 $H "$(ev Stop '' '' s1)"
expect_out "deleted message"              'is gone'
printf 'from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n\ndef test_add_neg():\n    assert add(-1, 1) == 0\n' > "$T"

echo "# stop-block limit"
printf 'def add(a, b):\n    return a + b\n' > "$PROJ/calc.py"
expect "s2 pre (green baseline)"          0 $H "$(ev PreToolUse Edit "$PROJ/calc.py" s2)"
printf 'def add(a, b):\n    return a - b  # broken on purpose\n' > "$PROJ/calc.py"
expect "block 1"                          2 $H "$(ev Stop '' '' s2)"
expect "block 2"                          2 $H "$(ev Stop '' '' s2)"
expect "block 3"                          2 $H "$(ev Stop '' '' s2)"
expect "4th: limit -> let it stop"        0 $H "$(ev Stop '' '' s2)"
printf 'def add(a, b):\n    return a + b\n' > "$PROJ/calc.py"

echo "# config: explicit command / disabled / js detection"
mkdir -p "$PROJ/.wards"
printf '[tests]\ncommand = "false"\n' > "$PROJ/.wards/config.toml"
expect "command=false -> block"           2 $H "$(ev Stop '' '' s1)"
printf '[tests]\ncommand = ""\n' > "$PROJ/.wards/config.toml"
expect "command='' -> run disabled"       0 $H "$(ev Stop '' '' s1)"
printf '[tests]\nrun_on_stop = false\n' > "$PROJ/.wards/config.toml"
printf 'def add(a, b):\n    return a - b  # broken on purpose\n' > "$PROJ/calc.py"
expect "run_on_stop=false -> no run"      0 $H "$(ev Stop '' '' s1)"
printf 'def add(a, b):\n    return a + b\n' > "$PROJ/calc.py"
rm "$PROJ/.wards/config.toml"
JS="$SCRATCH/jsproj"; mkdir -p "$JS"; cd "$JS" || exit 1
printf '{"name":"x","scripts":{"test":"node --test"}}\n' > package.json
printf 'const test = require("node:test"); const assert = require("node:assert");\ntest("ok", () => assert.equal(1, 1));\n' > app.test.js
printf 'module.exports = 1;\n' > app.js
expect "js pre"                           0 $H "$(ev PreToolUse Edit "$JS/app.js" s3)"
expect "js: npm test detected + passes"   0 $H "$(ev Stop '' '' s3)"
check "npm test ran" grep -q "npm test" "$WARDS_AUDIT_LOG"
printf 'const test = require("node:test"); const assert = require("node:assert");\ntest("ok", () => assert.equal(1, 2));\n' > app.test.js
expect "js red -> block"                  2 $H "$(ev Stop '' '' s3)"

echo "# pytest with no tests collected (exit 5) is not a failure"
E="$SCRATCH/empty"; mkdir -p "$E/tests"; touch "$E/conftest.py"; printf 'x = 1\n' > "$E/m.py"
expect "pre empty"                        0 $H "$(ev PreToolUse Edit "$E/m.py" s5)"
expect "stop: exit 5 -> pass"             0 $H "$(ev Stop '' '' s5)"
check "audit notes no tests"              grep -q "collected no tests" "$WARDS_AUDIT_LOG"

echo "# multi-project session: each touched project's tests run"
printf 'def add(a, b):\n    return a + b\n' > "$PROJ/calc.py"
printf 'const test = require("node:test"); const assert = require("node:assert");\ntest("ok", () => assert.equal(1, 1));\n' > "$JS/app.test.js"
expect "pre proj (green baseline)"        0 $H "$(ev PreToolUse Edit "$PROJ/calc.py" s6)"
expect "pre jsproj (green baseline)"      0 $H "$(ev PreToolUse Edit "$JS/app.js" s6)"
printf 'def add(a, b):\n    return a - b  # broken on purpose\n' > "$PROJ/calc.py"
expect "stop -> python project red -> block" 2 $H "$(ev Stop '' '' s6)"
printf 'def add(a, b):\n    return a + b\n' > "$PROJ/calc.py"
printf 'const test = require("node:test"); const assert = require("node:assert");\ntest("ok", () => assert.equal(1, 2));\n' > "$JS/app.test.js"
expect "stop -> js project red -> block"  2 $H "$(ev Stop '' '' s6)"
expect_out "js command named"             'npm test'
printf 'const test = require("node:test"); const assert = require("node:assert");\ntest("ok", () => assert.equal(1, 1));\n' > "$JS/app.test.js"
expect "both green -> 0"                  0 $H "$(ev Stop '' '' s6)"

echo "# pre-existing red suite: gate disabled, agent told, no 3x block"
printf 'def add(a, b):\n    return a - b  # broken on purpose\n' > "$PROJ/calc.py"
expect "pre with red baseline -> exit 0"  0 $H "$(ev PreToolUse Edit "$PROJ/calc.py" s7)"
expect_out "agent told suite already red" 'ALREADY RED'
expect "stop -> not blocked"              0 $H "$(ev Stop '' '' s7)"
check "audit: baseline fail"              grep -q "BASELINE-FAIL: tests" "$WARDS_AUDIT_LOG"
printf 'def add(a, b):\n    return a + b\n' > "$PROJ/calc.py"

echo "# deletion via any mechanism (rm / git rm / mv), rename tolerated, parametrize tolerated"
T2="$PROJ/tests/test_more.py"; printf 'def test_x():\n    assert True\n\ndef test_y():\n    assert True\n' > "$T2"
expect "pre (inventory includes test_more)" 0 $H "$(ev PreToolUse Edit "$PROJ/calc.py" s8)"
rm "$T2"
expect "rm test file via shell -> block"  2 $H "$(ev Stop '' '' s8)"
expect_out "names the missing file"       'test_more.py — test file is gone'
mv "$PROJ/tests/test_calc.py" "$PROJ/tests/test_calc_renamed.py"; printf 'def test_x():\n    assert True\n\ndef test_y():\n    assert True\n' > "$T2"
expect "rename tolerated"                 0 $H "$(ev Stop '' '' s8)"
mv "$PROJ/tests/test_calc_renamed.py" "$PROJ/tests/test_calc.py"
printf 'import pytest\n\n@pytest.mark.parametrize("v", [1, 2])\ndef test_x(v):\n    assert v\n' > "$T2"
expect "2 tests -> 1 parametrized -> tolerated" 0 $H "$(ev Stop '' '' s8)"
printf 'import sys, pytest\n\n@pytest.mark.skipif(sys.platform == "win32", reason="x")\ndef test_x():\n    assert True\n\ndef test_y():\n    assert True\n' > "$T2"
expect "skipif is not a skip"             0 $H "$(ev Stop '' '' s8)"
rm "$T2"; printf 'def add(a, b):\n    return a + b\n' > "$PROJ/calc.py"

echo "# missing test binary -> skipped, not blocked"
mkdir -p "$SCRATCH/nobin/.wards"; printf '[tests]\ncommand = "definitely-not-a-binary -q"\n' > "$SCRATCH/nobin/.wards/config.toml"
printf 'x = 1\n' > "$SCRATCH/nobin/m.py"; touch "$SCRATCH/nobin/conftest.py"
expect "pre nobin"                        0 $H "$(ev PreToolUse Edit "$SCRATCH/nobin/m.py" s9)"
expect "stop nobin -> 0"                  0 $H "$(ev Stop '' '' s9)"
check "unavailable audited"               grep -q "not found (PATH" "$WARDS_AUDIT_LOG"

echo "# non-source touch (md) doesn't trigger"
expect "md pre"                           0 $H "$(ev PreToolUse Edit "$SCRATCH/README.md" s4)"
expect "md-only session stop free"        0 $H "$(ev Stop '' '' s4)"
finish
