#!/usr/bin/env bash
# Launch Claude Code through the OpenShift dogfood gateway.
#
# Usage:
#   ./scripts/connect-dogfood.sh                    # uses default API key
#   ./scripts/connect-dogfood.sh sk-oai-XXXX...     # uses custom API key
#   ./scripts/connect-dogfood.sh --dry-run           # print env vars only
#
# Unsets all Vertex/GCP env vars so Claude Code uses the gateway
# instead of Vertex AI. Only affects this shell session.

set -euo pipefail

NAMESPACE="ai-gateway-dogfood"
DEFAULT_KEY="sk-oai-UXtX5CnDCu7gzAmn_vQR5JhhqkGheHaR8q115f4B7recA9peqKYRxZUZv6cI"

DRY_RUN=false
API_KEY="$DEFAULT_KEY"

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        sk-oai-*) API_KEY="$arg" ;;
    esac
done

# ── Resolve gateway URL ──────────────────────────────────────────

if oc whoami > /dev/null 2>&1; then
    GATEWAY_URL="https://$(oc -n "$NAMESPACE" get route ai-gateway-anthropic -o jsonpath='{.spec.host}' 2>/dev/null)"
else
    echo "WARNING: not logged in to OpenShift — using cached route"
    GATEWAY_URL="https://ai-gateway-anthropic-ai-gateway-dogfood.dogfood-us-south-1-bxf-4x-f196230f74f7ff44a5b4eeb1003c5bd5-0000.us-south.containers.appdomain.cloud"
fi

echo "Gateway:  $GATEWAY_URL"
echo "API Key:  ${API_KEY:0:20}..."
echo ""

# ── Launch or print ──────────────────────────────────────────────

if $DRY_RUN; then
    echo "Dry run — paste this into your shell:"
    echo ""
    echo "  unset CLAUDE_CODE_USE_VERTEX ANTHROPIC_VERTEX_PROJECT_ID CLOUD_ML_REGION"
    echo "  export ANTHROPIC_BASE_URL=\"$GATEWAY_URL\""
    echo "  export ANTHROPIC_API_KEY=\"$API_KEY\""
    echo "  claude"
    echo ""
else
    echo "Starting Claude Code through dogfood gateway..."
    echo "  (Vertex overridden for this session only)"
    echo ""
    exec env -u CLAUDE_CODE_USE_VERTEX \
             -u ANTHROPIC_VERTEX_PROJECT_ID \
             -u CLOUD_ML_REGION \
             -u GOOGLE_CLOUD_PROJECT \
             ANTHROPIC_BASE_URL="$GATEWAY_URL" \
             ANTHROPIC_API_KEY="$API_KEY" \
             claude --settings '{"env":{"CLAUDE_CODE_USE_VERTEX":"","ANTHROPIC_VERTEX_PROJECT_ID":"","CLOUD_ML_REGION":""}}'
fi
