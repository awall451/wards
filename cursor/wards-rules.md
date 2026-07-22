# Wards rules — Cursor dialect (layer 6)

<!-- Copy into the client project as .cursor/rules/wards.md (or .cursorrules).
     Cursor has no enforced permission layer or PreToolUse hooks — layers 4-5
     are Claude Code features. Under Cursor, layers 1-3 (scoped identity,
     Azure Policy, budgets) carry the enforcement load alone. Scope the
     credentials accordingly. Replace <RG>, <LOCATION>, <CLIENT>. -->

You are operating cloud infrastructure inside **wards** — your Azure identity is scoped to a single resource group and constrained by Azure Policy and budgets. Rules:

- All work happens in resource group `<RG>` in `<LOCATION>`. Nothing outside it.
- Never run identity, RBAC, policy, budget, or subscription-context commands (`az ad`, `az role`, `az policy`, `az account set`, `az consumption budget`). These will fail at the API; ask the human instead.
- Never run destructive commands (`delete`, `purge`, `terraform destroy`). Output the command for the human to review and run.
- Free or cheapest SKU by default; state estimated monthly cost and get confirmation before creating anything billable.
- Tag every resource `managed-by=wards-agent`, `client=<CLIENT>`. Naming: `<type>-<client>-<workload>`.
- If Azure denies a command (RBAC or policy), do not work around it. Report it.
