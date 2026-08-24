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

MODEL="rits/zai-org/glm-5-2-fp8"

if ! lsof -nP -iTCP:8181 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "ERROR: praxis is not listening on :8181. Run ./sideeye/live/setup.sh first." >&2
    exit 1
fi

# Claude Code's ~/.claude/settings.json force-sets CLAUDE_CODE_USE_VERTEX=1 (and
# Vertex project/region + default-model aliases) via its own `env` block, which
# overrides the shell — so a plain `unset` won't stop it using Vertex. Override
# at the settings layer instead: blank the Vertex vars and the model aliases,
# point at the local GLM gateway, and route the small/fast (background) model to
# GLM too (otherwise background calls hit a haiku model GLM rejects). Blanks win
# on merge over settings.json.
GLM_SETTINGS=$(cat <<JSON
{"env":{
  "CLAUDE_CODE_USE_VERTEX":"",
  "ANTHROPIC_VERTEX_PROJECT_ID":"",
  "CLOUD_ML_REGION":"",
  "ANTHROPIC_BASE_URL":"http://127.0.0.1:8181",
  "ANTHROPIC_API_KEY":"placeholder-praxis-injects-real-key",
  "ANTHROPIC_DEFAULT_SONNET_MODEL":"",
  "ANTHROPIC_DEFAULT_OPUS_MODEL":"",
  "ANTHROPIC_DEFAULT_HAIKU_MODEL":"",
  "ANTHROPIC_SMALL_FAST_MODEL":"$MODEL"
},"model":"$MODEL"}
JSON
)

echo "Claude Code -> GLM-5.2  (praxis :8181, model $MODEL)"
echo "Usage is metered locally at \$0 -> http://127.0.0.1:9090/dashboard"
echo
exec claude --settings "$GLM_SETTINGS" --model "$MODEL" "$@"
