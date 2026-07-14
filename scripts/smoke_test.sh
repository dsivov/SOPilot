#!/usr/bin/env bash
# Two-tenant isolation smoke test against a running SOPilot API on :8100
set -euo pipefail
BASE=http://127.0.0.1:8100
ADMIN="X-Admin-Token: dev-admin-token-p0"
JSON='Content-Type: application/json'
pass=0; fail=0
check() { # name expected actual
  if [ "$2" = "$3" ]; then pass=$((pass+1)); echo "PASS: $1";
  else fail=$((fail+1)); echo "FAIL: $1 (expected=$2 actual=$3)"; fi
}

# --- bootstrap two tenants ---
KEY_A=$(curl -s -X POST $BASE/admin/tenants -H "$ADMIN" -H "$JSON" -d '{"slug":"acme"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["api_key"])')
KEY_B=$(curl -s -X POST $BASE/admin/tenants -H "$ADMIN" -H "$JSON" -d '{"slug":"globex"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["api_key"])')
check "tenant keys issued" "yes" "$([ -n "$KEY_A" ] && [ -n "$KEY_B" ] && echo yes)"

# bad admin token refused
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/admin/tenants -H "X-Admin-Token: wrong" -H "$JSON" -d '{"slug":"evil"}')
check "bad admin token → 403" "403" "$code"

# --- projects ---
curl -s -X POST $BASE/admin/projects -H "Authorization: Bearer $KEY_A" -H "$JSON" -d '{"slug":"collections"}' > /dev/null
curl -s -X POST $BASE/admin/projects -H "Authorization: Bearer $KEY_B" -H "$JSON" -d '{"slug":"collections"}' > /dev/null

# --- SOP in tenant A ---
SOP_DEF='{"definition":{"name":"renewal","agent_actions":[{"name":"Greeting"},{"name":"VerifyIdentity"},{"name":"PitchRenewal","data_dependencies":["policy"]}],"user_states":[{"name":"AgreedToRenew"},{"name":"HardDecline"}],"conversation_profile":{"success_markers":["AgreedToRenew"],"failure_markers":["HardDecline"]},"data_dependencies":[{"name":"policy","kind":"mock","config":{"text":"policy #1"}}],"sop":{"edges":[{"src":"Greeting","dst":"VerifyIdentity"},{"src":"VerifyIdentity","dst":"PitchRenewal"}]}}}'
SOP_ID=$(curl -s -X POST $BASE/sops -H "Authorization: Bearer $KEY_A" -H "X-Project: collections" -H "$JSON" -d "$SOP_DEF" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
check "SOP created in tenant A" "yes" "$([ -n "$SOP_ID" ] && echo yes)"

# --- isolation: tenant B must not see or fetch tenant A's SOP ---
COUNT_B=$(curl -s $BASE/sops -H "Authorization: Bearer $KEY_B" -H "X-Project: collections" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))')
check "tenant B sees zero SOPs" "0" "$COUNT_B"
code=$(curl -s -o /dev/null -w '%{http_code}' $BASE/sops/$SOP_ID -H "Authorization: Bearer $KEY_B" -H "X-Project: collections")
check "tenant B fetching A's SOP → 404" "404" "$code"
code=$(curl -s -o /dev/null -w '%{http_code}' $BASE/sops -H "Authorization: Bearer invalid-key" -H "X-Project: collections")
check "invalid API key → 401" "401" "$code"
code=$(curl -s -o /dev/null -w '%{http_code}' $BASE/sops -H "Authorization: Bearer $KEY_A" -H "X-Project: nonexistent")
check "unknown project → 404" "404" "$code"

# --- lint + publish + session ---
PUBLISHABLE=$(curl -s -X POST $BASE/sops/$SOP_ID/lint -H "Authorization: Bearer $KEY_A" -H "X-Project: collections" | python3 -c 'import sys,json;print(json.load(sys.stdin)["publishable"])')
check "lint passes" "True" "$PUBLISHABLE"
STATUS=$(curl -s -X POST $BASE/sops/$SOP_ID/publish -H "Authorization: Bearer $KEY_A" -H "X-Project: collections" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')
check "publish succeeds" "published" "$STATUS"

# lint blocker: broken SOP (cycle) must refuse publish
BAD_DEF='{"definition":{"name":"broken","agent_actions":[{"name":"A"},{"name":"B"}],"user_states":[],"sop":{"edges":[{"src":"A","dst":"B"},{"src":"B","dst":"A"}]}}}'
BAD_ID=$(curl -s -X POST $BASE/sops -H "Authorization: Bearer $KEY_A" -H "X-Project: collections" -H "$JSON" -d "$BAD_DEF" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/sops/$BAD_ID/publish -H "Authorization: Bearer $KEY_A" -H "X-Project: collections")
check "cyclic SOP publish → 422" "422" "$code"

SESS=$(curl -s -X POST $BASE/sessions -H "Authorization: Bearer $KEY_A" -H "X-Project: collections" -H "$JSON" -d "{\"sop_id\":\"$SOP_ID\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')
check "session started" "yes" "$([ -n "$SESS" ] && echo yes)"

# pool snapshot: empty, and invisible cross-tenant
SIZE=$(curl -s $BASE/sessions/$SESS/pool -H "Authorization: Bearer $KEY_A" -H "X-Project: collections" | python3 -c 'import sys,json;print(json.load(sys.stdin)["size"])')
check "pool starts empty" "0" "$SIZE"
code=$(curl -s -o /dev/null -w '%{http_code}' $BASE/sessions/$SESS/pool -H "Authorization: Bearer $KEY_B" -H "X-Project: collections")
check "tenant B reading A's session pool → 404" "404" "$code"

STATUS=$(curl -s -X POST $BASE/sessions/$SESS/end -H "Authorization: Bearer $KEY_A" -H "X-Project: collections" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')
check "session ends" "ended" "$STATUS"

echo "---"
echo "RESULT: $pass passed, $fail failed"
[ $fail -eq 0 ]
