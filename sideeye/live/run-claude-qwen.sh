#!/usr/bin/env bash
# Launch Claude Code against the dogfood praxis gateway via the UNIFIED route —
# one URL where /model switches between the self-hosted Qwen3.8-27B-FP8 (default,
# $0) and Claude. Usage lands on the dogfood dashboard, per user.
#
#   Claude Code -> dogfood praxis (unified route, :8084) -> model-based routing:
#       Qwen3.8-27B-FP8 -> in-VPC vLLM (L40S, $0)
#       claude-*        -> Anthropic
#
# Both speak the Anthropic Messages API natively, so no translation. Starts on
# Qwen ($0); /model to Claude when you want it (subject to the Anthropic cap).
#
# Reads MAAS_API_KEY + UNIFIED_ROUTE (or QWEN_ROUTE) from qwen-gpu.env
# (gitignored). Uses --settings to override the Vertex config your
# ~/.claude/settings.json forces on.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/qwen-gpu.env"
: "${MAAS_API_KEY:?MAAS_API_KEY not in qwen-gpu.env}"
# Prefer the UNIFIED Anthropic-dialect route (one URL, model-based routing) so
# /model can switch between Qwen and Claude in-session. Falls back to the
# qwen-only route if UNIFIED_ROUTE isn't set.
ROUTE="${UNIFIED_ROUTE:-${QWEN_ROUTE:?set UNIFIED_ROUTE or QWEN_ROUTE in qwen-gpu.env}}"
# Must EXACTLY match vLLM's --served-model-name (qwen-vllm.service) AND the
# model_pricing row, or vLLM 400s (unknown model) or the gateway bills the
# $15/M default (no $0 pricing row). All three are "Qwen3.8-27B-FP8".
MODEL="Qwen3.8-27B-FP8"

# ~/.claude/settings.json force-sets CLAUDE_CODE_USE_VERTEX=1; blank it here or
# Claude Code ignores ANTHROPIC_BASE_URL and tries Vertex. Route the small/fast
# (background) model to qwen too, or background calls hit a model Qwen rejects.
SETTINGS=$(cat <<JSON
{"env":{
  "CLAUDE_CODE_USE_VERTEX":"",
  "ANTHROPIC_VERTEX_PROJECT_ID":"",
  "CLOUD_ML_REGION":"",
  "ANTHROPIC_BASE_URL":"$ROUTE",
  "ANTHROPIC_API_KEY":"$MAAS_API_KEY",
  "ANTHROPIC_DEFAULT_SONNET_MODEL":"",
  "ANTHROPIC_DEFAULT_OPUS_MODEL":"",
  "ANTHROPIC_DEFAULT_HAIKU_MODEL":"",
  "ANTHROPIC_SMALL_FAST_MODEL":"$MODEL"
},"model":"$MODEL","effortLevel":"xhigh",
"permissions":{"defaultMode":"acceptEdits","disableAutoMode":"disable"}}
JSON
)
# effortLevel "xhigh": the model's full-reasoning default. We only override at all
# because your global settings.json sends "high", which Qwen's vLLM rejects
# (it accepts xhigh/medium/low). This is NOT dialed down — speed comes from MTP
# speculative decoding on the server, not from reducing reasoning. Dial to
# "medium"/"low" yourself only if you personally want less thinking.
#
# permissions: acceptEdits + disableAutoMode. Claude Code's "auto" permission
# mode runs a per-action SAFETY CLASSIFIER as a separate model call — and that
# call goes through ANTHROPIC_BASE_URL, i.e. to THIS Qwen box. On a single L40S
# already busy serving the main session, the classifier starves and times out
# ("auto mode cannot determine the safety of Edit"). acceptEdits auto-approves
# edits + safe Bash (no classifier) and still prompts for genuinely dangerous
# actions; disableAutoMode removes auto from the cycle so it can't be re-enabled
# and start hitting the classifier again. Pass --permission-mode to override.

echo "Claude Code -> dogfood praxis (unified route) -> Qwen3.8-27B-FP8 (\$0) | /model for Claude"
echo "Metered on the dogfood dashboard."
echo
exec claude --settings "$SETTINGS" --model "$MODEL" "$@"
