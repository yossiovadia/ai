#!/usr/bin/env bash
# Launch Claude Code pointed at GLM-5.2 through the local Side-Eye gateway.
#
# Run ./sideeye/live/setup.sh first (brings up tunnel + metering + praxis).
# Then run THIS in a fresh terminal — it starts a Claude Code session whose
# traffic goes Claude Code -> praxis (:8181) -> GLM, metered locally at $0.
#
# The model is set to the exact GLM string so no praxis model-rewrite is needed;
# the API key is a placeholder (praxis injects the real GLM key server-side).
set -euo pipefail

export ANTHROPIC_BASE_URL="http://127.0.0.1:8181"
export ANTHROPIC_API_KEY="placeholder-praxis-injects-real-key"
export ANTHROPIC_MODEL="rits/zai-org/glm-5-2-fp8"
export ANTHROPIC_SMALL_FAST_MODEL="rits/zai-org/glm-5-2-fp8"
# Make sure Claude Code uses ANTHROPIC_BASE_URL, not Vertex.
unset CLAUDE_CODE_USE_VERTEX ANTHROPIC_VERTEX_PROJECT_ID 2>/dev/null || true

if ! curl -sf -o /dev/null --max-time 3 http://127.0.0.1:8181/ 2>/dev/null; then
    # A bare GET may 404/405; just check the port is open.
    if ! lsof -nP -iTCP:8181 -sTCP:LISTEN >/dev/null 2>&1; then
        echo "ERROR: praxis is not listening on :8181. Run ./sideeye/live/setup.sh first." >&2
        exit 1
    fi
fi

echo "Claude Code -> GLM-5.2  (praxis :8181, model rits/zai-org/glm-5-2-fp8)"
echo "Usage is metered locally at \$0 -> http://127.0.0.1:9090/dashboard"
echo
exec claude "$@"
