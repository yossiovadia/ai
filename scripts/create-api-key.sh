#!/usr/bin/env bash
# Create a maas API key for a user on the dogfood cluster.
#
# Usage:
#   ./scripts/create-api-key.sh <username> <key-name> [group]
#
# Examples:
#   ./scripts/create-api-key.sh yovadia@redhat.com dogfood-yossi ai-eng
#   ./scripts/create-api-key.sh noyitz@redhat.com dogfood-noy ai-eng
#   ./scripts/create-api-key.sh chris.wright@redhat.com cto-demo executive

set -euo pipefail

NAMESPACE="ai-gateway-dogfood"
USERNAME="${1:?Usage: $0 <username> [key-name] [group]}"
KEY_NAME="${2:-dogfood-${USERNAME%%@*}}"
GROUP="${3:-ai-eng}"

if ! oc whoami > /dev/null 2>&1; then
    echo "ERROR: not logged in to OpenShift. Run: oc login ..."
    exit 1
fi

# Port-forward to maas-api
oc -n "$NAMESPACE" port-forward svc/maas-api 18080:8080 &
PF_PID=$!
trap "kill $PF_PID 2>/dev/null" EXIT
sleep 2

RESPONSE=$(curl -s -X POST "http://localhost:18080/v1/api-keys" \
    -H "Content-Type: application/json" \
    -H "X-MaaS-Username: $USERNAME" \
    -H "X-MaaS-Group: [\"$GROUP\"]" \
    -d "{\"name\":\"$KEY_NAME\",\"description\":\"Dogfood gateway key for $USERNAME\"}")

KEY=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['key'])" 2>/dev/null)
EXPIRES=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['expiresAt'])" 2>/dev/null)

if [[ -z "$KEY" ]]; then
    echo "ERROR: failed to create key"
    echo "$RESPONSE"
    exit 1
fi

ANTHROPIC_ROUTE=$(oc -n "$NAMESPACE" get route ai-gateway-anthropic -o jsonpath='{.spec.host}' 2>/dev/null || echo "<route-not-found>")

echo ""
echo "=========================================="
echo "  API Key Created"
echo "=========================================="
echo ""
echo "  User:    $USERNAME"
echo "  Name:    $KEY_NAME"
echo "  Group:   $GROUP"
echo "  Expires: $EXPIRES"
echo ""
echo "  Key: $KEY"
echo ""
echo "  Connect Claude Code:"
echo "    ANTHROPIC_BASE_URL=\"https://$ANTHROPIC_ROUTE\" \\"
echo "    ANTHROPIC_API_KEY=\"$KEY\" \\"
echo "    claude"
echo ""
