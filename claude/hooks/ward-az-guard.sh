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

# Heredoc bodies are data (docs, scripts), not commands: drop them before scanning
# so `cat > notes.md <<EOF … az group delete … EOF` is not mistaken for a delete.
# The full command (flattened) is still what gets audit-logged. `scan` is what
# every inspection below looks at; `cmd` is what gets logged.
# A here-string (<<<"x") is NOT a heredoc opener; `<<` must not be preceded or followed by `<`.
scan="$(awk '
  !intag && match($0, /(^|[^<])<<-?[[:space:]]*["'"'"']?[A-Za-z_][A-Za-z0-9_]*/) && substr($0, RSTART + RLENGTH, 1) != "<" {
    tag = substr($0, RSTART, RLENGTH)
    sub(/^.?<<-?[[:space:]]*["'"'"']?/, "", tag)
    print; intag = 1; next
  }
  intag { if ($0 ~ ("^[[:space:]]*" tag "[[:space:]]*$")) { intag = 0; print }; next }
  { print }' <<<"$cmd")"
# Quoting a verb (`az group "delete"`) must not hide it from the word regexes.
scan="$(tr -d "'\"" <<<"$scan")"

# Only inspect az / terraform / tofu invocations (incl. after pipes/&&/;)
if ! grep -qE '(^|[|;&[:space:]])(az|terraform|tofu)[[:space:]]' <<<"$scan"; then
  exit 0
fi

AUDIT_LOG="${WARDS_AUDIT_LOG:-$HOME/.wards/audit.log}"
mkdir -p "$(dirname "$AUDIT_LOG")"

# --- Profile assertion: az must run against the agent's isolated profile, ---
# --- never the default ~/.azure (which may hold a human's login).         ---
# The harness sets AZURE_CONFIG_DIR via settings.json "env"; if it's missing,
# this session is misconfigured — block az entirely rather than risk running
# as the wrong identity.
if grep -qE '(^|[|;&[:space:]])az[[:space:]]' <<<"$scan" && [ -z "${AZURE_CONFIG_DIR:-}" ]; then
  printf '%s\t%s\t%s\n' "$(date -Is)" "BLOCKED: AZURE_CONFIG_DIR unset" "${cmd//$'\n'/\\n}" >> "$AUDIT_LOG"
  echo "WARD BLOCK: AZURE_CONFIG_DIR is not set — az would use the default profile (possibly a human identity). Fix settings.json env block; do not work around." >&2
  exit 2
fi
# …and the command itself must not re-point the profile or change identity inside it.
if grep -qE '(^|[[:space:];&|])(env[[:space:]]+)?AZURE_CONFIG_DIR=' <<<"$scan"; then
  printf '%s\t%s\t%s\n' "$(date -Is)" "BLOCKED: AZURE_CONFIG_DIR override in command" "${cmd//$'\n'/\\n}" >> "$AUDIT_LOG"
  echo "WARD BLOCK: overriding AZURE_CONFIG_DIR in a command would run az as a different identity. Reserved for a human operator." >&2
  exit 2
fi

# One audit entry = one line: multi-line commands (heredocs) are flattened.
log() { printf '%s\t%s\t%s\n' "$(date -Is)" "$1" "${cmd//$'\n'/\\n}" >> "$AUDIT_LOG"; }

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
done < <(tr '|;&' '\n' <<<"$scan")
grep -qE 'az[[:space:]]+account[[:space:]]+(set|clear)' <<<"$scan" \
  && deny "subscription context change"
grep -qE 'az[[:space:]]+(logout|login)([[:space:]]|$)' <<<"$scan" \
  && deny "login/logout (identity change)"
grep -qE 'az[[:space:]]+consumption[[:space:]]+budget' <<<"$scan" \
  && deny "budget modification"

# --- Destructive verbs: a human runs these. Judged per az/terraform SEGMENT, so
# --- `terraform show plan | grep -c delete` or `echo az group delete` don't trip it.
while IFS= read -r seg; do
  # the segment's COMMAND must be az/terraform (after optional env assignments / sudo / time)
  grep -qE '^[[:space:]]*((env|sudo|time|nice)[[:space:]]+|[A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*(az|terraform|tofu)[[:space:]]' <<<"$seg" || continue
  grep -qE '(^|[[:space:]])(delete|purge)([[:space:]]|$)' <<<"$seg" \
    && deny "destructive verb (delete/purge)"
  grep -qE '(terraform|tofu)[[:space:]]+(destroy|state[[:space:]]+rm|workspace[[:space:]]+delete)' <<<"$seg" \
    && deny "terraform destroy/state-mutation"
  grep -qE '(terraform|tofu)[[:space:]]+apply.*-destroy' <<<"$seg" \
    && deny "terraform apply -destroy"
done < <(tr '|;&' '\n' <<<"$scan")

log "ALLOWED"
exit 0
