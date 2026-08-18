# The six layers, explained

The design principle: **enforcement lives below the agent.** Each layer down is harder to bypass and cheaper to trust. The top layers exist for ergonomics and audit; the bottom layers exist so the top layers don't have to be perfect.

## Layer 1 — Scoped identity (Azure RBAC)

The agent logs in as a service principal assigned the **Wards Operator** custom role on **one resource group**. Wards Operator is Contributor minus the tampering surface: no `Microsoft.Authorization` writes (can't grant itself roles), no elevate-access, no key vault purge.

- Files: `azure/rbac/wards-operator-role.json`, `azure/rbac/create-agent-identity.sh`
- Failure mode covered: agent (or leaked credential) touching anything outside its sandbox. The API returns 403 — there is no agent-side bypass.

## Layer 2 — Azure Policy

Built-in policies assigned at the resource-group scope: allowed locations, allowed VM SKUs, denied resource types. Even inside its own RG with a valid role, the agent cannot create a GPU cluster or an AKS instance — the ARM API rejects the request at write time.

- File: `azure/policy/assign-policies.sh`
- Failure mode covered: expensive-resource accidents. A wrong SKU in a Terraform variable becomes a policy denial, not a bill.

## Layer 3 — Budgets + alerts

A monthly budget on the RG with email alerts at 50/90/100%. Budgets don't stop spend (on PAYG nothing hard-stops spend) — they bound *detection time*. Requires Pay-As-You-Go; on a free account, the spending limit is the (stronger) guard instead.

- File: `azure/budgets/create-budget.sh`
- Failure mode covered: slow cost leaks that pass layers 1-2 (e.g. legitimate resources left running, egress, storage growth).

## Layer 4 — Harness permissions (Claude Code)

`settings.json` deny-list rejects identity/RBAC/policy/destructive command *prefixes* before the shell ever runs; the allow-list lets read-only commands through without prompting. Enforced by the Claude Code harness, not by the model.

- File: `claude/settings.json`
- Failure mode covered: the agent attempting a forbidden command class at all — fails fast, no API round trip, teaches the agent the boundary.

## Layer 5 — Command-inspection hook

A PreToolUse hook inspects every Bash command containing `az`/`terraform`/`tofu`. Regex classes: guardrail tampering (identity, RBAC, policy, budgets, subscription switching) and destructive verbs (`delete`, `purge`, `destroy`, `state rm`). Blocked commands return an explanation to the agent; **every inspected command — allowed or blocked — is appended to an audit log** (`~/.wards/audit.log`, tab-separated: timestamp, verdict, command).

The audit log is a deliverable: after an engagement, the client can read exactly what the agent ran and what the wards refused.

- File: `claude/hooks/ward-az-guard.sh`
- Failure mode covered: forbidden commands smuggled past prefix matching (pipes, `&&` chains, flags reordered) — the hook sees the full command string. Also: the audit trail itself.

The same layer carries six more hooks aimed at the repo rather than the cloud account — the agent's *output* is warded too. `ward-git-guard.py` (PreToolUse Bash) blocks hook bypass, history rewrites, whole-tree discards and enforces Conventional Commits. `ward-complexity.py` and `ward-lint.py` (Pre/PostToolUse on file edits + Stop) ratchet code shape (`lizard`) and the project's own linter/formatter, and block new suppression comments. `ward-tests.py` and `ward-typecheck.py` (Stop) refuse "done" while tests are red, deleted or skipped, or new type errors exist. `ward-tamper-guard.py` keeps the agent out of `.claude/settings*.json`, `.claude/hooks/` and `.wards/` — enforcement the agent can edit is not enforcement. All ratchet against the state at the agent's first touch this session — legacy problems warn once, new or worsened ones block. See [`code-quality.md`](code-quality.md).

- Files: `claude/hooks/ward-git-guard.py`, `ward-complexity.py`, `ward-lint.py`, `ward-tests.py`, `ward-typecheck.py`, `ward-tamper-guard.py`, `wardlib.py`, `wardtools.py`
- Failure mode covered: correct-but-badly-shaped code accumulating silently, and the specific ways agents cheat to go green — silence the linter, delete the test, `--no-verify`.

## Layer 6 — Agent conventions

CLAUDE.md / Cursor rules: scope statement, cost discipline (free SKUs default, confirm-before-billable), tagging and naming standards, "when warded, don't work around — report." No enforcement, but it shapes nearly all routine behavior, and it's what makes the agent pleasant to supervise.

- Files: `claude/CLAUDE.md.template`, `cursor/wards-rules.md`

## What each tool gets

| Tool | Layers available |
|---|---|
| Claude Code | 1-6 (full stack) |
| Cursor | 1-3 + 6 (no harness permissions / hooks — cloud-side layers carry enforcement) |
| Anything else | 1-3 always hold. That is the point of the design. |
