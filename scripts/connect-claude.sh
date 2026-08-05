#!/usr/bin/env bash
# Authenticate against Keycloak and start Claude Code through the Praxis gateway.
#
# Usage:
#   ./scripts/connect-claude.sh yossi           # uses password 'test123'
#   ./scripts/connect-claude.sh yossi mypass     # custom password
#   ./scripts/connect-claude.sh yossi --dry-run  # print env vars, don't launch
#
# This script:
#   1. Gets a JWT from Keycloak for the given user
#   2. Starts Claude Code with ANTHROPIC_BASE_URL pointing at the local Praxis gateway
#   3. Uses the JWT as the API key (Praxis validates it, then injects the real Anthropic key)
#
# The user's normal Vertex/GCP Claude Code config is NOT modified.
# Only this shell session uses the Praxis gateway.

set -euo pipefail

GATEWAY_URL="${GATEWAY_URL:-http://127.0.0.1:8180}"
KEYCLOAK_URL="${KEYCLOAK_URL:-http://127.0.0.1:8280}"
KC_REALM="${KC_REALM:-ai-gateway}"
KC_CLIENT="${KC_CLIENT:-praxis-gateway}"

# ── Parse arguments ───────────────────────────────────────────────

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <keycloak-username> [password] [--dry-run]"
    echo ""
    echo "Examples:"
    echo "  $0 yossi              # authenticate as yossi (password: test123)"
    echo "  $0 noyitz secretpass  # authenticate as noyitz with custom password"
    echo "  $0 yossi --dry-run    # show env vars without launching Claude Code"
    exit 1
fi

USERNAME="$1"
PASSWORD="${2:-test123}"
DRY_RUN=false

if [[ "${2:-}" == "--dry-run" || "${3:-}" == "--dry-run" ]]; then
    DRY_RUN=true
    if [[ "${2:-}" == "--dry-run" ]]; then
        PASSWORD="test123"
    fi
fi

# ── Preflight checks ─────────────────────────────────────────────

if ! curl -s -o /dev/null -w '%{http_code}' "$GATEWAY_URL/" 2>/dev/null | grep -qE '(200|401|403|404|500)'; then
    echo "ERROR: Praxis gateway not reachable at $GATEWAY_URL"
    echo ""
    echo "  Start it first: ./scripts/start-gateway.sh"
    exit 1
fi

if ! curl -sf -o /dev/null "$KEYCLOAK_URL/realms/$KC_REALM/.well-known/openid-configuration" 2>&1; then
    echo "ERROR: Keycloak not reachable at $KEYCLOAK_URL"
    exit 1
fi

# ── Authenticate ──────────────────────────────────────────────────

RESPONSE=$(curl -s "$KEYCLOAK_URL/realms/$KC_REALM/protocol/openid-connect/token" \
    -d "client_id=$KC_CLIENT" \
    -d "username=$USERNAME" \
    -d "password=$PASSWORD" \
    -d "grant_type=password")

JWT=$(echo "$RESPONSE" | python3 -c "
import sys, json
d = json.load(sys.stdin)
if 'access_token' in d:
    print(d['access_token'])
else:
    print('ERROR:' + d.get('error_description', d.get('error', 'unknown')), file=sys.stderr)
    sys.exit(1)
" 2>&1) || {
    echo "ERROR: Authentication failed for user '$USERNAME'"
    echo "  $JWT"
    echo ""
    echo "  Check: is the user registered in Keycloak realm '$KC_REALM'?"
    echo "  Check: is the password correct?"
    exit 1
}

# Extract token expiry for display
EXPIRES_IN=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('expires_in', '?'))")

echo "Authenticated as: $USERNAME"
echo "Token expires in: ${EXPIRES_IN}s"
echo "Gateway:          $GATEWAY_URL"
echo ""

# ── Launch or print ───────────────────────────────────────────────

if $DRY_RUN; then
    echo "Dry run — set these env vars in your shell:"
    echo ""
    echo "  export ANTHROPIC_BASE_URL=\"$GATEWAY_URL\""
    echo "  export ANTHROPIC_API_KEY=\"\$JWT\"  # (token omitted for safety)"
    echo "  export CLAUDE_CODE_USE_VERTEX=\"\""
    echo "  export CLOUD_ML_REGION=\"\""
    echo "  export ANTHROPIC_VERTEX_PROJECT_ID=\"\""
    echo "  export GOOGLE_CLOUD_PROJECT=\"\""
    echo ""
    echo "  # Then run: claude"
    echo ""
    echo "  # Or for curl:"
    echo "  curl \$ANTHROPIC_BASE_URL/v1/messages \\"
    echo "    -H \"Content-Type: application/json\" \\"
    echo "    -H \"x-api-key: \$JWT\" \\"
    echo "    -d '{\"model\":\"claude-haiku-4-5-20251001\",\"max_tokens\":64,\"messages\":[{\"role\":\"user\",\"content\":\"Hi\"}]}'"
else
    echo "Starting Claude Code through Praxis gateway..."
    echo "  (your normal Vertex config is not affected — only this session)"
    echo ""
    exec env \
        ANTHROPIC_BASE_URL="$GATEWAY_URL" \
        ANTHROPIC_API_KEY="$JWT" \
        CLAUDE_CODE_USE_VERTEX="" \
        CLOUD_ML_REGION="" \
        ANTHROPIC_VERTEX_PROJECT_ID="" \
        GOOGLE_CLOUD_PROJECT="" \
        claude --settings '{"env":{"CLAUDE_CODE_USE_VERTEX":"","ANTHROPIC_VERTEX_PROJECT_ID":""}}'
fi
