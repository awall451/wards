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
│   ├── settings.json        # harness permissions + hook wiring (layer 4)
│   ├── hooks/ward-az-guard.sh  # command inspector + audit logger (layer 5)
│   └── CLAUDE.md.template   # agent conventions (layer 6)
├── cursor/
│   └── wards-rules.md       # layer 6, Cursor dialect
└── docs/
    └── layers.md    # the full explainer, layer by layer
```

## Quickstart (new engagement)

1. **Human:** run `azure/rbac/create-agent-identity.sh <rg-name> <location>` — creates the resource group, the Wards Operator role, and a service principal scoped to that RG only.
2. **Human:** run `azure/policy/assign-policies.sh <rg-name>` and `azure/budgets/create-budget.sh <rg-name> <amount>`.
3. Copy `claude/` contents into the project's `.claude/` directory (or `cursor/wards-rules.md` into `.cursor/rules/`).
4. Hand the agent the scoped credentials. It now operates inside the wards; the audit log lands in `~/.wards/audit.log`.

Everything above uses free Azure features — RBAC, Policy, and budgets cost $0.
