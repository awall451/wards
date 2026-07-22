#!/usr/bin/env bash
# Layer 2 — Azure Policy.
# Assigns built-in policies to the agent's resource group:
#   - Allowed locations           (deny resources outside approved regions)
#   - Allowed VM size SKUs        (deny expensive compute; free/burstable only by default)
#   - Not allowed resource types  (deny categorically dangerous/expensive types)
#
# Run as a HUMAN with Owner rights. Policy denials happen at the Azure API —
# they hold even if every agent-side guardrail fails.
#
# Usage: ./assign-policies.sh <resource-group> [allowed-locations] [allowed-vm-skus]
#   e.g. ./assign-policies.sh rg-sigilworks-site '["eastus2"]' '["Standard_B1s","Standard_B1ls"]'
set -euo pipefail

RG="${1:?usage: $0 <resource-group> [locations-json] [skus-json]}"
LOCATIONS="${2:-[\"eastus2\"]}"
VM_SKUS="${3:-[\"Standard_B1s\",\"Standard_B1ls\",\"Standard_B2s\"]}"

SCOPE="$(az group show --name "$RG" --query id -o tsv)"

# Built-in policy definition GUIDs (stable, documented by Microsoft):
ALLOWED_LOCATIONS_DEF="e56962a6-4747-49cd-b67b-bf8b01975c4c"
ALLOWED_VM_SKUS_DEF="cccc23c7-8427-4f53-ad12-b6a63eb452b3"
NOT_ALLOWED_TYPES_DEF="6c112d4e-5bc7-47ae-a041-ea2d9dccd749"

# Resource types the agent has no business creating (expensive or blast-radius-heavy).
# Tune per engagement.
DENIED_TYPES='["Microsoft.Compute/virtualMachineScaleSets","Microsoft.ContainerService/managedClusters","Microsoft.Sql/managedInstances","Microsoft.CognitiveServices/accounts","Microsoft.Synapse/workspaces","Microsoft.Databricks/workspaces","Microsoft.HDInsight/clusters","Microsoft.NetApp/netAppAccounts","Microsoft.VMwareCloudSimple/dedicatedCloudNodes"]'

az policy assignment create --name "wards-locations" --scope "$SCOPE" \
  --policy "$ALLOWED_LOCATIONS_DEF" \
  --params "{\"listOfAllowedLocations\":{\"value\":$LOCATIONS}}" --output none
echo "✔ allowed locations: $LOCATIONS"

az policy assignment create --name "wards-vm-skus" --scope "$SCOPE" \
  --policy "$ALLOWED_VM_SKUS_DEF" \
  --params "{\"listOfAllowedSKUs\":{\"value\":$VM_SKUS}}" --output none
echo "✔ allowed VM SKUs: $VM_SKUS"

az policy assignment create --name "wards-denied-types" --scope "$SCOPE" \
  --policy "$NOT_ALLOWED_TYPES_DEF" \
  --params "{\"listOfResourceTypesNotAllowed\":{\"value\":$DENIED_TYPES}}" --output none
echo "✔ denied resource types"

echo "Policies assigned to $RG. Test: try creating a resource in a disallowed region — expect a policy denial."
