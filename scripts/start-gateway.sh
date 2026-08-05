#!/usr/bin/env bash
# Start the Praxis AI gateway with metering and JWT auth.
#
# Prerequisites:
#   - Keycloak running on :8280 (docker run ... quay.io/keycloak/keycloak)
#   - Metering service running on :9090 (docker compose up in ai-gateway-metering-service)
#   - ANTHROPIC_API_KEY set in the environment
#
# Usage:
#   export ANTHROPIC_API_KEY="sk-ant-..."
#   ./scripts/start-gateway.sh

set -euo pipefail

GATEWAY_PORT="${GATEWAY_PORT:-8180}"
KEYCLOAK_URL="${KEYCLOAK_URL:-http://127.0.0.1:8280}"
METERING_URL="${METERING_URL:-http://127.0.0.1:9090}"
CONFIG_FILE="dev-metering-anthropic.yaml"
BINARY="./target/debug/praxis-ai"

# ── Preflight checks ─────────────────────────────────────────────

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
    echo "ERROR: ANTHROPIC_API_KEY is not set."
    echo ""
    echo "  export ANTHROPIC_API_KEY=\"sk-ant-...\""
    echo "  ./scripts/start-gateway.sh"
    exit 1
fi

if [[ ! -x "$BINARY" ]]; then
    echo "ERROR: $BINARY not found. Build first:"
    echo ""
    echo "  cargo build -p praxis-ai-proxy"
    exit 1
fi

if ! curl -sf "$KEYCLOAK_URL/health/ready" > /dev/null 2>&1; then
    echo "ERROR: Keycloak not reachable at $KEYCLOAK_URL"
    echo ""
    echo "  docker run -d --name keycloak -p 8280:8080 \\"
    echo "    -e KC_BOOTSTRAP_ADMIN_USERNAME=admin \\"
    echo "    -e KC_BOOTSTRAP_ADMIN_PASSWORD=admin \\"
    echo "    quay.io/keycloak/keycloak:26.2.4 start-dev"
    exit 1
fi

if ! curl -sf "$METERING_URL/health" > /dev/null 2>&1; then
    echo "WARNING: Metering service not reachable at $METERING_URL"
    echo "         Metering will fail-open (requests pass, usage not recorded)"
    echo ""
fi

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "ERROR: $CONFIG_FILE not found. Run from the praxis-ai repo root."
    exit 1
fi

# ── Start ─────────────────────────────────────────────────────────

echo "Starting Praxis AI gateway"
echo "  Gateway:  http://127.0.0.1:${GATEWAY_PORT}"
echo "  Keycloak: ${KEYCLOAK_URL}"
echo "  Metering: ${METERING_URL}"
echo "  Config:   ${CONFIG_FILE}"
echo ""
echo "  Use ./scripts/connect-claude.sh <username> to start Claude Code"
echo ""

exec "$BINARY" -c "$CONFIG_FILE"
