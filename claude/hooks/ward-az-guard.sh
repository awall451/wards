#!/usr/bin/env bash
# Layer 5 — command-inspection ward for Claude Code.
# PreToolUse hook on the Bash tool: inspects every az / terraform / tofu
# command before it runs. Blocks guardrail-tampering and destructive verbs
# (exit 2 = block, stderr goes back to the agent). Audit-logs every
# inspected command, allowed or blocked.
#
# Wire-up: claude/settings.json → hooks.PreToolUse (matcher "Bash").
# Audit log: $WARDS_AUDIT_LOG, default ~/.wards/audit.log
set -uo pipefail

input="$(cat)"
cmd="$(jq -r '.tool_input.command // empty' <<<"$input")"
[ -n "$cmd" ] || exit 0

# Only inspect az / terraform / tofu invocations (incl. after pipes/&&/;)
if ! grep -qE '(^|[|;&[:space:]])(az|terraform|tofu)[[:space:]]' <<<"$cmd"; then
  exit 0
fi

AUDIT_LOG="${WARDS_AUDIT_LOG:-$HOME/.wards/audit.log}"
mkdir -p "$(dirname "$AUDIT_LOG")"

log() { printf '%s\t%s\t%s\n' "$(date -Is)" "$1" "$cmd" >> "$AUDIT_LOG"; }

deny() {
  log "BLOCKED: $1"
  echo "WARD BLOCK: $1. This command class is reserved for a human operator." >&2
  exit 2
}

# --- Guardrail tampering: identity, RBAC, policy, budgets, subscription hops ---
# Reading the wards is fine (the agent may verify its own constraints), so
# az ad/role/policy list/show pass. Split on shell separators and inspect
# EACH segment so a read can't smuggle a write past the ward (e.g.
# "az role assignment list && az role assignment create").
while IFS= read -r seg; do
  [ -n "$seg" ] || continue
  if grep -qE '(^|[[:space:]])az[[:space:]]+(ad|role|policy)([[:space:]]|$)' <<<"$seg"; then
    grep -qE '(^|[[:space:]])az[[:space:]]+(ad|role|policy)[[:space:]]+([a-z-]+[[:space:]]+)*(list|show)([[:space:]]|$)' <<<"$seg" \
      || deny "identity/RBAC/policy write command"
  fi
done < <(tr '|;&' '\n' <<<"$cmd")
grep -qE 'az[[:space:]]+account[[:space:]]+(set|clear)' <<<"$cmd" \
  && deny "subscription context change"
grep -qE 'az[[:space:]]+logout' <<<"$cmd" \
  && deny "logout"
grep -qE 'az[[:space:]]+consumption[[:space:]]+budget' <<<"$cmd" \
  && deny "budget modification"

# --- Destructive verbs: a human runs these ---
grep -qE '(^|[[:space:]])(delete|purge)([[:space:]]|$)' <<<"$cmd" \
  && deny "destructive verb (delete/purge)"
grep -qE '(terraform|tofu)[[:space:]]+(destroy|state[[:space:]]+rm|workspace[[:space:]]+delete)' <<<"$cmd" \
  && deny "terraform destroy/state-mutation"
grep -qE '(terraform|tofu)[[:space:]]+apply.*-destroy' <<<"$cmd" \
  && deny "terraform apply -destroy"

log "ALLOWED"
exit 0
