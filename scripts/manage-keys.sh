#!/usr/bin/env bash
# Manage API keys on the dogfood cluster.
#
# Usage:
#   ./scripts/manage-keys.sh create <username> [key-name] [group]
#   ./scripts/manage-keys.sh list [username]
#   ./scripts/manage-keys.sh revoke <username> [key-name]
#   ./scripts/manage-keys.sh revoke-all <username>
#
# Examples:
#   ./scripts/manage-keys.sh create noyitz@redhat.com
#   ./scripts/manage-keys.sh create chris.wright@redhat.com cto-demo executive
#   ./scripts/manage-keys.sh list
#   ./scripts/manage-keys.sh list noyitz@redhat.com
#   ./scripts/manage-keys.sh revoke noyitz@redhat.com dogfood-noyitz
#   ./scripts/manage-keys.sh revoke-all test-user@redhat.com

set -euo pipefail

NAMESPACE="ai-gateway-dogfood"
ACTION="${1:?Usage: $0 <create|list|revoke|revoke-all> ...}"
shift

if ! oc whoami > /dev/null 2>&1; then
    echo "ERROR: not logged in to OpenShift. Run: oc login ..."
    exit 1
fi

# ── Port-forward ─────────────────────────────────────────────

oc -n "$NAMESPACE" port-forward svc/maas-api 18080:8080 > /dev/null 2>&1 &
PF_PID=$!
trap "kill $PF_PID 2>/dev/null" EXIT
sleep 2

ADMIN_USER="yovadia@redhat.com"
ADMIN_GROUP='["ai-eng"]'

# ── Helpers ──────────────────────────────────────────────────

api() {
    curl -s "$@" \
        -H "Content-Type: application/json" \
        -H "X-MaaS-Username: $ADMIN_USER" \
        -H "X-MaaS-Group: $ADMIN_GROUP"
}

# ── Actions ──────────────────────────────────────────────────

case "$ACTION" in

create)
    USERNAME="${1:?Usage: $0 create <username> [key-name] [group]}"
    KEY_NAME="${2:-dogfood-${USERNAME%%@*}}"
    GROUP="${3:-ai-eng}"

    RESPONSE=$(api -X POST "http://localhost:18080/v1/api-keys" \
        -H "X-MaaS-Username: $USERNAME" \
        -H "X-MaaS-Group: [\"$GROUP\"]" \
        -d "{\"name\":\"$KEY_NAME\",\"description\":\"Dogfood gateway key for $USERNAME\"}")

    KEY=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('key',''))" 2>/dev/null)
    EXPIRES=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('expiresAt','?'))" 2>/dev/null)

    if [[ -z "$KEY" ]]; then
        echo "ERROR: failed to create key"
        echo "$RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$RESPONSE"
        exit 1
    fi

    ANTHROPIC_ROUTE=$(oc -n "$NAMESPACE" get route ai-gateway-anthropic -o jsonpath='{.spec.host}' 2>/dev/null)
    OPENAI_ROUTE=$(oc -n "$NAMESPACE" get route ai-gateway-openai -o jsonpath='{.spec.host}' 2>/dev/null)

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
    echo "  Claude Code:"
    echo "    ./scripts/connect-dogfood.sh $KEY"
    echo ""
    echo "  Codex:"
    echo "    OPENAI_API_KEY=\"$KEY\" codex"
    echo ""
    ;;

list)
    USERNAME="${1:-}"
    if [[ -n "$USERNAME" ]]; then
        RESPONSE=$(curl -s -X POST "http://localhost:18080/v1/api-keys/search" \
            -H "Content-Type: application/json" \
            -H "X-MaaS-Username: $USERNAME" \
            -H "X-MaaS-Group: $ADMIN_GROUP" \
            -d "{}")
    else
        RESPONSE=$(api -X POST "http://localhost:18080/v1/api-keys/search" -d "{}")
    fi

    echo "$RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
keys = [k for k in data.get('data', []) if k['status'] == 'active']
if not keys:
    print('No keys found.')
    sys.exit(0)
hdr = '{:<38} {:<25} {:<30} {:<10} {}'.format('ID', 'Name', 'User', 'Status', 'Last Used')
print(hdr)
print('-' * len(hdr))
for k in keys:
    last = k.get('lastUsedAt', 'never') or 'never'
    if len(last) > 19:
        last = last[:19]
    print('{:<38} {:<25} {:<30} {:<10} {}'.format(k['id'], k['name'], k['username'], k['status'], last))
print('\nTotal: {} key(s)'.format(len(keys)))
"
    ;;

revoke)
    USERNAME="${1:?Usage: $0 revoke <username> [key-name]}"
    KEY_NAME="${2:-}"

    # Search as the target user to see their keys
    RESPONSE=$(curl -s -X POST "http://localhost:18080/v1/api-keys/search" \
        -H "Content-Type: application/json" \
        -H "X-MaaS-Username: $USERNAME" \
        -H "X-MaaS-Group: $ADMIN_GROUP" \
        -d "{}")

    KEYS=$(echo "$RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
name_filter = '$KEY_NAME'
for k in data.get('data', []):
    if k['status'] != 'active':
        continue
    if name_filter and k['name'] != name_filter:
        continue
    print(k['id'] + '|' + k['name'])
" 2>/dev/null)

    if [[ -z "$KEYS" ]]; then
        echo "No active keys found for $USERNAME${KEY_NAME:+ with name '$KEY_NAME'}"
        exit 1
    fi

    COUNT=0
    while IFS='|' read -r ID NAME; do
        curl -s -X DELETE "http://localhost:18080/v1/api-keys/$ID" \
            -H "Content-Type: application/json" \
            -H "X-MaaS-Username: $USERNAME" \
            -H "X-MaaS-Group: $ADMIN_GROUP" > /dev/null
        echo "Revoked: $NAME ($ID)"
        ((COUNT++))
    done <<< "$KEYS"
    echo "Revoked $COUNT key(s) for $USERNAME"
    ;;

revoke-all)
    USERNAME="${1:?Usage: $0 revoke-all <username>}"

    RESPONSE=$(curl -s -X POST "http://localhost:18080/v1/api-keys/search" \
        -H "Content-Type: application/json" \
        -H "X-MaaS-Username: $USERNAME" \
        -H "X-MaaS-Group: $ADMIN_GROUP" \
        -d "{}")

    IDS=$(echo "$RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
ids = [k['id'] for k in data.get('data', []) if k['status'] == 'active']
print('\n'.join(ids))
" 2>/dev/null)

    if [[ -z "$IDS" ]]; then
        echo "No active keys found for $USERNAME"
        exit 1
    fi

    COUNT=0
    while read -r ID; do
        curl -s -X DELETE "http://localhost:18080/v1/api-keys/$ID" \
            -H "Content-Type: application/json" \
            -H "X-MaaS-Username: $USERNAME" \
            -H "X-MaaS-Group: $ADMIN_GROUP" > /dev/null
        ((COUNT++))
    done <<< "$IDS"
    echo "Revoked all $COUNT key(s) for $USERNAME"
    ;;

*)
    echo "Unknown action: $ACTION"
    echo "Usage: $0 <create|list|revoke|revoke-all> ..."
    exit 1
    ;;
esac
