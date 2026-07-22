#!/usr/bin/env bash
# Layer 1 — Scoped identity.
# Creates: resource group, "Wards Operator" custom role (first run only),
# and a service principal scoped to ONLY that resource group.
#
# Run as a HUMAN with Owner rights. The agent never runs this.
#
# Usage: ./create-agent-identity.sh <resource-group> <location>
#   e.g. ./create-agent-identity.sh rg-sigilworks-site eastus2
set -euo pipefail
cd "$(dirname "$0")"

RG="${1:?usage: $0 <resource-group> <location>}"
LOCATION="${2:?usage: $0 <resource-group> <location>}"

SUB_ID="$(az account show --query id -o tsv)"
echo "Subscription: $SUB_ID"
echo "Resource group: $RG ($LOCATION)"

# 1. Resource group, tagged as agent-operated
az group create --name "$RG" --location "$LOCATION" \
  --tags managed-by=wards-agent wards=true --output none
echo "✔ resource group"

# 2. Custom role (create once per subscription; update if it already exists)
ROLE_DEF="$(sed "s/SUBSCRIPTION_ID/$SUB_ID/" wards-operator-role.json)"
if az role definition list --name "Wards Operator" --query '[0]' -o tsv | grep -q .; then
  az role definition update --role-definition "$ROLE_DEF" --output none
  echo "✔ Wards Operator role (updated)"
else
  az role definition create --role-definition "$ROLE_DEF" --output none
  echo "✔ Wards Operator role (created)"
fi

# 3. Service principal scoped to the RG only — this is the agent's identity.
#    Output contains the credential. Store it in a secret manager; never commit it.
echo "— Creating scoped service principal (SAVE THIS OUTPUT SECURELY): —"
az ad sp create-for-rbac \
  --name "wards-agent-$RG" \
  --role "Wards Operator" \
  --scopes "/subscriptions/$SUB_ID/resourceGroups/$RG"

cat <<EOF

Agent login (in the agent's shell):
  az login --service-principal -u <appId> -p <password> --tenant <tenant>
  az configure --defaults group=$RG location=$LOCATION

Next: assign policies (../policy/assign-policies.sh $RG) and a budget
(../budgets/create-budget.sh $RG <amount>).
EOF
