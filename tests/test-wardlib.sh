#!/usr/bin/env bash
# wardlib.py — crash visibility, state pruning, config env override, anchor resolution.
# shellcheck source=tests/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

echo "# crash handler: a broken hook is visible (exit 1 + audit CRASH), never a silent pass"
cat > "$SCRATCH/broken-hook.py" <<'PY'
import os, sys
sys.path.insert(0, os.environ["HOOKS"])
from wardlib import dispatch, read_payload
def boom(_p): raise RuntimeError("kaboom")
dispatch(read_payload(), boom, boom, boom)
PY
out="$(ev PostToolUse Edit /tmp/x.py s1 | HOOKS="$HOOKS" python3 "$SCRATCH/broken-hook.py" 2>&1)"; rc=$?
if [ "$rc" = 1 ] && grep -q "WARD CRASH" <<<"$out"; then PASS=$((PASS+1)); echo "  ok   crash -> exit 1 + message"; else FAIL=$((FAIL+1)); echo "  FAIL crash handling rc=$rc: $out"; fi
check "crash audited"                     grep -q "CRASH:" "$WARDS_AUDIT_LOG"

echo "# state pruning"
mkdir -p "$WARDS_STATE_DIR"; echo '{}' > "$WARDS_STATE_DIR/old.complexity.json"; touch -d '30 days ago' "$WARDS_STATE_DIR/old.complexity.json"
echo '{}' > "$WARDS_STATE_DIR/new.complexity.json"
ev PreToolUse Edit "$SCRATCH/nothing.md" s2 | python3 "$HOOKS/ward-complexity.py" >/dev/null 2>&1 || true
python3 - "$HOOKS" "$WARDS_STATE_DIR" <<'PY'
import sys; sys.path.insert(0, sys.argv[1])
from wardlib import Session
Session("s2", "complexity")
PY
check "old state pruned"                  [ ! -e "$WARDS_STATE_DIR/old.complexity.json" ]
check "fresh state kept"                  [ -e "$WARDS_STATE_DIR/new.complexity.json" ]

# pyok <label> <args...>  -> reads a python program on stdin, passes if it exits 0
pyok() { local label="$1"; shift; if python3 - "$@" >/dev/null 2>&1; then PASS=$((PASS+1)); printf '  ok   %s\n' "$label"; else FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$label"; fi; }

echo "# corrupt state file -> reset, not crash"
printf '{"baseline": {"/x.py": {' > "$WARDS_STATE_DIR/corrupt.complexity.json"
pyok "corrupt state resets" "$HOOKS" "$SCRATCH" <<'PY'
import sys; sys.path.insert(0, sys.argv[1])
from wardlib import Session
s = Session("corrupt", "complexity"); assert s.baseline == {}; s.save()
import json, os; json.loads(open(s.path).read()); assert not [f for f in os.listdir(os.path.dirname(s.path)) if f.endswith(".tmp")]
PY
check "corrupt state audited"             grep -q "unreadable" "$WARDS_AUDIT_LOG"

echo "# config: env override + section isolation + bad toml"
mkdir -p "$SCRATCH/.wards"; printf '[complexity]\nccn = [1, 2, 3]\n[git]\nsubject_max = 50\n' > "$SCRATCH/.wards/config.toml"
pyok "config env override + isolation" "$HOOKS" "$SCRATCH" <<'PY'
import os, sys; sys.path.insert(0, sys.argv[1]); os.chdir(sys.argv[2])
os.environ["WARDS_GIT_SUBJECT_MAX"] = "60"; os.environ["WARDS_LINT_FORMAT"] = "false"
from wardlib import config
assert config("complexity")["ccn"] == [1, 2, 3], config("complexity")
assert config("git")["subject_max"] == 60, config("git")
assert config("lint")["format"] is False, config("lint")
assert config("nothing") == {}
print("config-ok")
PY
printf 'this is = not [ toml\n' > "$SCRATCH/.wards/config.toml"
pyok "bad toml -> empty config, no crash" "$HOOKS" "$SCRATCH" <<'PY'
import os, sys; sys.path.insert(0, sys.argv[1]); os.chdir(sys.argv[2])
from wardlib import config
assert config("complexity") == {}
PY
check "bad toml audited"                  grep -q "unreadable" "$WARDS_AUDIT_LOG"
rm -f "$SCRATCH/.wards/config.toml"

echo "# as_list / bands / JSON env coercion / run_hook crash-safe configure"
pyok "as_list + bands + coercion" "$HOOKS" "$SCRATCH" <<'PY'
import os, sys; sys.path.insert(0, sys.argv[1]); os.chdir(sys.argv[2])
from wardlib import as_list, bands, _coerce
assert as_list("a, b,c") == ["a", "b", "c"] and as_list(["x", 1]) == ["x", "1"] and as_list(None) == []
assert bands([1, 2, 3], [9, 9, 9]) == [1, 2, 3]
assert bands(10, [10, 20, 50]) == [10, 20, 50] and bands([3, 2, 1], [10, 20, 50]) == [10, 20, 50]
assert _coerce("[5,10,20]") == [5, 10, 20] and _coerce("true") is True and _coerce("7") == 7 and _coerce("x,y") == "x,y"
PY
cat > "$SCRATCH/badcfg-hook.py" <<'PY'
import os, sys
sys.path.insert(0, os.environ["HOOKS"])
from wardlib import run_hook
def configure(_a): raise ValueError("bad config value")
def noop(_p): pass
run_hook("demo", configure, (noop, noop, noop))
PY
out="$(ev PostToolUse Edit /tmp/x.py s3 | HOOKS="$HOOKS" python3 "$SCRATCH/badcfg-hook.py" 2>&1)"; rc=$?
if [ "$rc" = 1 ] && grep -q "WARD CRASH (demo)" <<<"$out"; then PASS=$((PASS+1)); echo "  ok   configure crash is visible + audited"; else FAIL=$((FAIL+1)); echo "  FAIL configure crash rc=$rc: $out"; fi
check "config fingerprint change detected" bash -c "
mkdir -p '$SCRATCH/fp/.wards' && printf '[x]\na=1\n' > '$SCRATCH/fp/.wards/config.toml' && cd '$SCRATCH/fp' &&
python3 - '$HOOKS' <<'PY'
import sys; sys.path.insert(0, sys.argv[1])
from wardlib import config_fingerprint_changed
assert config_fingerprint_changed('fpsess') == ''
open('.wards/config.toml','a').write('b=2\n')
assert 'config.toml' in config_fingerprint_changed('fpsess')
assert config_fingerprint_changed('fpsess') == ''   # reported once
PY"

echo "# anchor: Pre/Post -> file path; Stop -> first touched; else cwd"
pyok "anchor resolution" "$HOOKS" "$SCRATCH" <<'PY'
import os, sys; sys.path.insert(0, sys.argv[1]); os.chdir(sys.argv[2])
from wardlib import anchor, Session
assert anchor({"tool_input": {"file_path": "/a/b.py"}}, "x") == "/a/b.py"
s = Session("anch", "x"); s.baseline["/first/f.py"] = {}; s.save()
assert anchor({"hook_event_name": "Stop", "session_id": "anch"}, "x") == "/first/f.py"
assert anchor({"hook_event_name": "Stop", "session_id": "nobody"}, "x") == os.getcwd()
PY
finish
