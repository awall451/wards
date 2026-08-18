#!/usr/bin/env bash
# ward-az-guard.sh — cloud-command inspection: tampering, destructive verbs,
# profile assertion, segment inspection, heredoc bodies ignored, audit flattening.
# shellcheck source=tests/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
H=ward-az-guard.sh
export AZURE_CONFIG_DIR="$SCRATCH/az-profile"
ok() { local l="${1//$'\n'/ }"; expect "allow: ${l:0:60}" 0 $H "$(bash_ev "$1" s1)"; }
no() { local l="${1//$'\n'/ }"; expect "block: ${l:0:60}" 2 $H "$(bash_ev "$1" s1)"; }

echo "# reads and ordinary work"
ok "az group list -o table"
ok "az account show"
ok "az role assignment list --all"
ok "az policy assignment list"
ok "az ad sp show --id x"
ok "az staticwebapp create --name swa-x --resource-group rg-x --sku Free"
ok "terraform plan -out tf.plan"
ok "terraform apply tf.plan"
ok "terraform fmt -recursive"
ok "ls -la; echo done"

echo "# guardrail tampering"
no "az role assignment create --assignee x --role Owner"
no "az ad sp create-for-rbac"
no "az policy assignment create --name x"
no "az policy definition create --name x"
no "az account set --subscription other"
no "az account clear"
no "az logout"
no "az consumption budget create --amount 5"
no "az role assignment list && az role assignment create --assignee x"

echo "# destructive verbs"
no "az group delete -n rg-x --yes"
no "az keyvault purge --name kv"
no "az storage account delete -n x"
no "terraform destroy -auto-approve"
no "terraform state rm module.x"
no "terraform workspace delete old"
no "terraform apply -destroy"
no "tofu destroy"
no "az group show -n rg-x | az group delete -n rg-x"

echo "# profile assertion"
payload="$(bash_ev 'az group list' s1)"
out="$(printf '%s' "$payload" | env -u AZURE_CONFIG_DIR "$HOOKS/$H" 2>&1)"; rc=$?
if [ "$rc" = 2 ] && grep -q "AZURE_CONFIG_DIR" <<<"$out"; then PASS=$((PASS+1)); echo "  ok   unset profile blocks az"; else FAIL=$((FAIL+1)); echo "  FAIL unset profile (rc=$rc)"; fi
bash_ev 'terraform plan' s1 | env -u AZURE_CONFIG_DIR "$HOOKS/$H" >/dev/null 2>&1; rc=$?
if [ "$rc" = 0 ]; then PASS=$((PASS+1)); echo "  ok   unset profile does not block terraform"; else FAIL=$((FAIL+1)); echo "  FAIL terraform blocked by profile check"; fi

echo "# heredoc bodies are data"
ok $'cat > docs.md <<\'EOF\'\nNever run az group delete or terraform destroy.\nEOF'
ok $'python3 - <<\'PY\'\nprint("az role assignment create")\nPY'
no $'cat > x.md <<EOF\nnothing\nEOF\naz group delete -n rg-x'
ok $'cat <<EOF\naz keyvault purge --name kv\nEOF\naz group list'

echo "# reviewer findings"
no $'grep x <<<"foo"\naz group delete -n foo'                       # here-string is not a heredoc
no $'cat <<< EOF\naz group delete -n foo'
no 'AZURE_CONFIG_DIR=$HOME/.azure az account show'                    # inline profile override
no 'env AZURE_CONFIG_DIR=/tmp/x az group list'
no "az login"
no "az login --service-principal -u x -p y --tenant z"
no 'az group "delete" -n x'
no "az group 'delete' -n x"
no 'az account "set" --subscription x'
ok "terraform show tfplan | grep -c delete"                          # delete outside the az/tf segment
ok "az resource list -o table | grep -i delete"
ok "echo az group delete"                                             # not an az invocation… wait: 'echo az group delete' IS 'az group delete' after echo? treat as data
ok "cd terraform && grep -rn purge ."
ok "terraform plan -destroy -out p.plan"                              # planning a destroy is a read
ok "terraform state list"

echo "# audit: one line per entry, flattened"
lines_before=$(wc -l <"$WARDS_AUDIT_LOG")
no $'az group delete -n rg-x <<EOF\nmulti\nline\nEOF'
lines_after=$(wc -l <"$WARDS_AUDIT_LOG")
check "multi-line command = one audit line" [ $((lines_after - lines_before)) = 1 ]
check "audit has BLOCKED" grep -q "BLOCKED: destructive verb" "$WARDS_AUDIT_LOG"
check "audit has ALLOWED" grep -q "ALLOWED" "$WARDS_AUDIT_LOG"
finish
