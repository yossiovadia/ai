#!/usr/bin/env bash
# Launch Codex through the OpenShift dogfood gateway.
#
# Usage:
#   OPENAI_API_KEY=sk-oai-XXXX ./scripts/connect-codex.sh
#   OPENAI_API_KEY=sk-oai-XXXX ./scripts/connect-codex.sh --dry-run
#
# The API key is your MaaS key (sk-oai-*). Get one from your team lead.
# Only affects this shell session — your normal OpenAI config is untouched.

set -euo pipefail

NAMESPACE="ai-gateway-dogfood"
GATEWAY_URL="https://ai-gateway-openai-ai-gateway-dogfood.dogfood-us-south-1-bxf-4x-f196230f74f7ff44a5b4eeb1003c5bd5-0000.us-south.containers.appdomain.cloud/v1"

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
    echo "ERROR: OPENAI_API_KEY not set"
    echo ""
    echo "  export OPENAI_API_KEY=\"sk-oai-YOUR-KEY-HERE\""
    echo "  $0"
    echo ""
    echo "  Get your key from your team lead or via:"
    echo "    ./scripts/manage-keys.sh list <your-email>"
    exit 1
fi

echo "Gateway:  $GATEWAY_URL"
echo "API Key:  ${OPENAI_API_KEY:0:20}..."
echo ""

if $DRY_RUN; then
    echo "Dry run — paste this into your shell:"
    echo ""
    echo "  export OPENAI_BASE_URL=\"$GATEWAY_URL\""
    echo "  export OPENAI_API_KEY=\"$OPENAI_API_KEY\""
    echo "  codex"
    echo ""
else
    echo "Starting Codex through dogfood gateway..."
    echo "  (only this session is affected)"
    echo ""
    exec env \
        OPENAI_BASE_URL="$GATEWAY_URL" \
        OPENAI_API_KEY="$OPENAI_API_KEY" \
        codex
fi
