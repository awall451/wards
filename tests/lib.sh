#!/usr/bin/env bash
# Shared harness for hook tests: feed a Claude Code hook payload to a hook
# script, assert on exit code. Each test runs in a scratch dir with an
# isolated audit log and state dir so runs never touch ~/.wards.
set -uo pipefail
HOOKS="$(cd "$(dirname "${BASH_SOURCE[0]}")/../claude/hooks" && pwd)"
SCRATCH="$(mktemp -d)"
export WARDS_AUDIT_LOG="$SCRATCH/audit.log" WARDS_STATE_DIR="$SCRATCH/state"
export CLAUDE_PROJECT_DIR="$SCRATCH"
cd "$SCRATCH" || exit 1
PASS=0; FAIL=0

# ev <event> <tool> <file> <session> [extra-json]  -> payload on stdout
ev() {
  jq -cn --arg e "$1" --arg t "$2" --arg f "$3" --arg s "$4" --argjson x "${5:-{\}}" \
    '{hook_event_name:$e,tool_name:$t,session_id:$s,tool_input:{file_path:$f}} + $x'
}
# bash_ev <command> <session> [event]
bash_ev() {
  jq -cn --arg c "$1" --arg s "$2" --arg e "${3:-PreToolUse}" \
    '{hook_event_name:$e,tool_name:"Bash",session_id:$s,tool_input:{command:$c}}'
}
# expect <label> <want-exit> <hook> <payload-json>
expect() {
  local label="$1" want="$2" hook="$3" payload="$4" got out
  out="$(printf '%s' "$payload" | python3 "$HOOKS/$hook" 2>&1)"; got=$?
  [ "$hook" = "${hook%.py}" ] && { out="$(printf '%s' "$payload" | "$HOOKS/$hook" 2>&1)"; got=$?; }
  if [ "$got" = "$want" ]; then PASS=$((PASS+1)); printf '  ok   %s\n' "$label"
  else FAIL=$((FAIL+1)); printf '  FAIL %s (want %s got %s)\n%s\n' "$label" "$want" "$got" "$out"; fi
  LAST_OUT="$out"
}
# check <label> <command...>  -> pass if command succeeds
check() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then PASS=$((PASS+1)); printf '  ok   %s\n' "$label"
  else FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$label"; fi
}
# expect_out <label> <regex>  -> assert LAST_OUT matches
expect_out() {
  if grep -qE "$2" <<<"$LAST_OUT"; then PASS=$((PASS+1)); printf '  ok   %s\n' "$1"
  else FAIL=$((FAIL+1)); printf '  FAIL %s (no match /%s/)\n%s\n' "$1" "$2" "$LAST_OUT"; fi
}
finish() { printf '%s: %d passed, %d failed\n' "$(basename "$0")" "$PASS" "$FAIL"; rm -rf "$SCRATCH"; [ "$FAIL" = 0 ]; }
