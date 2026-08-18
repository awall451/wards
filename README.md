# Wards 🛡️

**Layered guardrails for AI agents operating cloud infrastructure.**

> *"We don't ask you to trust the AI. We scope its keys so trust isn't required."*

AI coding agents (Claude Code, Cursor, and whatever comes next) are extraordinarily productive when handed real cloud credentials — and extraordinarily dangerous if handed the *wrong* credentials. Wards is the answer: a set of guardrail layers where the important ones are **enforced by the cloud itself**, not by the agent's good behavior.

## The threat model

An agent with your cloud keys could, in principle:

- delete or corrupt production resources,
- create expensive resources (a GPU VM is a car payment per week),
- escalate its own privileges or alter its own guardrails,
- touch resources far outside its assigned task.

Config files that ask the agent to behave do not solve this — a different tool won't read them, and an agent can err. Enforcement has to live *below* the agent.

## The six layers

| # | Layer | Enforced by | What it does |
|---|-------|-------------|--------------|
| 1 | **Scoped identity** | Azure RBAC | Agent credentials are scoped to **one resource group** with a custom role (Contributor minus guardrail-tampering rights). It cannot touch what it cannot see. |
| 2 | **Azure Policy** | Azure | Allowed regions, allowed SKUs, denied resource types. Even a syntactically valid command gets a policy denial at the API. |
| 3 | **Budgets + alerts** | Azure | Cost alert thresholds on the agent's resource group. Blast radius measured in dollars, capped and monitored. |
| 4 | **Harness permissions** | Agent harness (Claude Code `settings.json`) | Deny-list for identity/RBAC/policy commands, allow-list for read-only operations. First line of defense, enforced by the harness before the shell runs. |
| 5 | **Command-inspection hook** | Agent harness (PreToolUse hook) | Every `az`/`terraform` command is inspected by a script before execution: destructive verbs blocked, guardrail-tampering blocked, **everything audit-logged**. |
|   | **Code-quality hooks** | Agent harness (Pre/PostToolUse + Stop hooks) | The agent's *output* is warded, not just its cloud access: code shape (complexity, length, params — `lizard`), the project's own linter/formatter, no new suppression comments, git hygiene (no `--no-verify`, force-push, `reset --hard`; Conventional Commits), tests must run green and may not be deleted or skipped, no new type errors; and the agent may not edit its own wards. All ratcheting: new/worsened blocks, legacy warns once. |
| 6 | **Agent conventions** | The agent (CLAUDE.md / Cursor rules) | Naming standards, cost-consciousness, confirm-before-create-billable. Softest layer — etiquette, not enforcement — but it shapes 99% of routine behavior. |

Layers 1–3 hold even if the agent goes completely rogue or a different, rule-ignoring tool is used. Layers 4–5 catch mistakes cheaply and produce the audit trail. Layer 6 makes the day-to-day pleasant.

## Repo layout

```
wards/
├── azure/
│   ├── rbac/        # Wards Operator custom role + scoped identity setup script (layer 1)
│   ├── policy/      # built-in policy assignments: locations, SKUs, denied types (layer 2)
│   └── budgets/     # budget + alert creation script (layer 3)
├── claude/
│   ├── settings.json              # harness permissions + hook wiring (layer 4)
│   ├── hooks/ward-az-guard.sh     # cloud-command inspector + audit logger (layer 5)
│   ├── hooks/ward-git-guard.py    # git hygiene: no bypass / rewrite / destroy; Conventional Commits
│   ├── hooks/ward-complexity.py   # code shape ratchet: CCN, fn length, params, file length (lizard)
│   ├── hooks/ward-lint.py         # project's own linter+formatter on every edit; blocks new suppressions
│   ├── hooks/ward-tests.py        # Stop gate: tests ran green; no test deleted or skipped
│   ├── hooks/ward-typecheck.py    # Stop gate: no new type errors (tsc / pyright / mypy / go vet)
│   ├── hooks/ward-tamper-guard.py # the agent may not edit its own wards (settings, hooks, .wards/)
│   ├── hooks/wardlib.py           # shared: .wards/config.toml, session state, audit log
│   ├── hooks/wardtools.py         # shared: linter/formatter discovery (never imposed)
│   └── CLAUDE.md.template         # agent conventions (layer 6)
├── cursor/
│   └── wards-rules.md       # layer 6, Cursor dialect
├── docs/
│   ├── layers.md        # the full explainer, layer by layer
│   ├── code-quality.md  # the five code-quality hooks: policy, ratchet, config
│   └── ci.md            # CI backstop recipes (lizard, jscpd, diff-cover, ADO sketch)
├── tests/               # bash simulation harness for every hook (tests/run.sh)
└── .wards/config.toml   # this repo's own ward config (dogfood)
```

## Quickstart (new engagement)

1. **Human:** run `azure/rbac/create-agent-identity.sh <rg-name> <location>` — creates the resource group, the Wards Operator role, and a service principal scoped to that RG only.
2. **Human:** run `azure/policy/assign-policies.sh <rg-name>` and `azure/budgets/create-budget.sh <rg-name> <amount> <alert-email>`. Budgets apply to pay-as-you-go subscriptions; on a free-credit account the spending limit is the stronger guard until upgrade.
3. Copy `claude/` contents into the project's `.claude/` directory (or `cursor/wards-rules.md` into `.cursor/rules/`).
   The code-quality hooks need `lizard` on the agent's PATH (`pipx install lizard`) and wrap whatever linter/formatter/test/type tools the project already uses. Optional per-project tuning in `.wards/config.toml` — see `docs/code-quality.md`.
4. Hand the agent the scoped credentials. It now operates inside the wards; the audit log lands in `~/.wards/audit.log`.

Everything above uses free Azure features — RBAC, Policy, and budgets cost $0.

## Working on this repo

```bash
pre-commit install
```

`.pre-commit-config.yaml` runs gitleaks, a private-key detector, ShellCheck, ruff, lizard (the same CCN limit the hooks enforce), the hook test suites (`tests/run.sh`), and basic hygiene checks on every commit.

This is the one failure the six layers cannot undo. Layers 1–3 are enforced by Azure and hold even against a rogue agent, but nothing in them prevents a credential being committed to a public repository — and published history has to be assumed cloned before it can be scrubbed. A pre-commit scan is the only place that failure is cheap to stop.

Scan everything, including before any first public push:

```bash
pre-commit run --all-files
```
