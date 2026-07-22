#!/usr/bin/env bash
# Layer 3 — Budgets + alerts.
# Creates a monthly cost budget scoped to the agent's resource group, with
# alert emails at 50% / 90% / 100% of the amount.
#
# Run as a HUMAN. Note: budgets require a Pay-As-You-Go (or CSP/EA)
# subscription — they are not available while a free-account spending limit
# is the active guard.
#
# Usage: ./create-budget.sh <resource-group> <monthly-amount-usd> <alert-email>
#   e.g. ./create-budget.sh rg-sigilworks-site 5 dillon@sigilworks.dev
set -euo pipefail

RG="${1:?usage: $0 <resource-group> <amount> <email>}"
AMOUNT="${2:?usage: $0 <resource-group> <amount> <email>}"
EMAIL="${3:?usage: $0 <resource-group> <amount> <email>}"

SUB_ID="$(az account show --query id -o tsv)"
SCOPE="/subscriptions/$SUB_ID/resourceGroups/$RG"
START="$(date +%Y-%m-01)"
END="$(date -d "+5 years" +%Y-%m-01 2>/dev/null || date -v+5y +%Y-%m-01)"

az consumption budget create-with-rg \
  --budget-name "wards-budget-$RG" \
  --resource-group "$RG" \
  --amount "$AMOUNT" \
  --category cost \
  --time-grain monthly \
  --start-date "$START" \
  --end-date "$END" \
  --output none 2>/dev/null || \
az rest --method put \
  --url "https://management.azure.com$SCOPE/providers/Microsoft.Consumption/budgets/wards-budget-$RG?api-version=2023-05-01" \
  --body "{
    \"properties\": {
      \"category\": \"Cost\",
      \"amount\": $AMOUNT,
      \"timeGrain\": \"Monthly\",
      \"timePeriod\": {\"startDate\": \"${START}T00:00:00Z\", \"endDate\": \"${END}T00:00:00Z\"},
      \"notifications\": {
        \"warn50\":  {\"enabled\": true, \"operator\": \"GreaterThan\", \"threshold\": 50,  \"contactEmails\": [\"$EMAIL\"]},
        \"warn90\":  {\"enabled\": true, \"operator\": \"GreaterThan\", \"threshold\": 90,  \"contactEmails\": [\"$EMAIL\"]},
        \"warn100\": {\"enabled\": true, \"operator\": \"GreaterThan\", \"threshold\": 100, \"contactEmails\": [\"$EMAIL\"]}
      }
    }
  }" --output none

echo "✔ budget wards-budget-$RG: \$$AMOUNT/mo on $RG, alerts to $EMAIL at 50/90/100%"
