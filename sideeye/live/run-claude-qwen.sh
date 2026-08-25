#!/usr/bin/env bash
# Launch Claude Code against your self-hosted Qwen3.8-27B-FP8, via the REAL
# dogfood praxis gateway — so usage lands on the dogfood dashboard, per user.
#
#   Claude Code -> dogfood praxis (qwen route, :8083) -> Qwen vLLM (in-VPC L40S)
#
# vLLM serves the Anthropic Messages API natively, so praxis just passes through
# (no translation). Metered on the dogfood dashboard at $0 (qwen pricing row=0).
#
# Reads MAAS_API_KEY + QWEN_ROUTE from qwen-gpu.env (gitignored). Uses --settings
# to override the Vertex config your ~/.claude/settings.json forces on.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/qwen-gpu.env"
: "${MAAS_API_KEY:?MAAS_API_KEY not in qwen-gpu.env}"
: "${QWEN_ROUTE:?QWEN_ROUTE not in qwen-gpu.env}"
MODEL="qwen"

# ~/.claude/settings.json force-sets CLAUDE_CODE_USE_VERTEX=1; blank it here or
# Claude Code ignores ANTHROPIC_BASE_URL and tries Vertex. Route the small/fast
# (background) model to qwen too, or background calls hit a model Qwen rejects.
SETTINGS=$(cat <<JSON
{"env":{
  "CLAUDE_CODE_USE_VERTEX":"",
  "ANTHROPIC_VERTEX_PROJECT_ID":"",
  "CLOUD_ML_REGION":"",
  "ANTHROPIC_BASE_URL":"$QWEN_ROUTE",
  "ANTHROPIC_API_KEY":"$MAAS_API_KEY",
  "ANTHROPIC_DEFAULT_SONNET_MODEL":"",
  "ANTHROPIC_DEFAULT_OPUS_MODEL":"",
  "ANTHROPIC_DEFAULT_HAIKU_MODEL":"",
  "ANTHROPIC_SMALL_FAST_MODEL":"$MODEL"
},"model":"$MODEL","effortLevel":"medium"}
JSON
)
# effortLevel "medium": Qwen's vLLM only accepts reasoning_effort xhigh/medium/low,
# NOT the "high" your global settings.json sends (that 500s every request).

echo "Claude Code -> dogfood praxis -> Qwen3.8-27B-FP8 (in-VPC L40S)"
echo "Metered on the dogfood dashboard (model 'qwen', \$0)."
echo
exec claude --settings "$SETTINGS" --model "$MODEL" "$@"
