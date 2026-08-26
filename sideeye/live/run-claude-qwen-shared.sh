#!/usr/bin/env bash
# Shareable launcher: point Claude Code at the dogfood UNIFIED route, for ANYONE
# with a MaaS API key. One URL, model-based routing: starts on the self-hosted
# Qwen3.8-27B-FP8 ($0), and /model switches to Claude in-session. No shared
# secrets — bring your own key:
#
#   export MAAS_API_KEY=sk-...        # your own MaaS API key
#   ./run-claude-qwen-shared.sh       # extra args pass through to `claude`
#
# This bakes in every quirk of the self-hosted route so you don't hit them one
# at a time:
#   - effortLevel=xhigh: the endpoint rejects reasoning_effort "high" (Claude
#     Code's default for this tier) with a 500 — only xhigh/medium/low work.
#   - permissions acceptEdits + disableAutoMode: "auto" mode's safety classifier
#     is a separate model call through this same (slow, single-GPU) route, so it
#     times out ("auto mode cannot determine the safety of Edit"). acceptEdits
#     skips the classifier and still gates dangerous actions.
#   - CLAUDE_CODE_USE_VERTEX/etc. blanked: avoids "Not logged in" when your shell
#     exports Vertex vars.
#   - CLAUDE_CODE_AUTO_COMPACT_WINDOW=245760: Claude Code assumes a 200K window
#     for unknown models and auto-compacts at ~90% of it (~165K input); the box
#     serves 262144. 240K (served minus 16K headroom) delays compaction ~35%.
# Cost is $0 (metered as model=Qwen3.8-27B-FP8, provider=vllm). The GPU box must
# be running — if it's stopped to save cost, requests fail until it's started.
set -euo pipefail

: "${MAAS_API_KEY:?set MAAS_API_KEY to your MaaS API key first: export MAAS_API_KEY=sk-...}"

# Unified Anthropic-dialect route: one URL, /model switches Qwen <-> Claude.
UNIFIED_ROUTE="https://ai-gateway-unified-ai-gateway-dogfood.dogfood-us-south-1-bxf-4x-f196230f74f7ff44a5b4eeb1003c5bd5-0000.us-south.containers.appdomain.cloud"
MODEL="Qwen3.8-27B-FP8"   # default model; use /model in-session to switch to Claude

SETTINGS=$(cat <<JSON
{"env":{
  "CLAUDE_CODE_USE_VERTEX":"",
  "ANTHROPIC_VERTEX_PROJECT_ID":"",
  "CLOUD_ML_REGION":"",
  "ANTHROPIC_BASE_URL":"$UNIFIED_ROUTE",
  "ANTHROPIC_API_KEY":"$MAAS_API_KEY",
  "ANTHROPIC_SMALL_FAST_MODEL":"$MODEL",
  "CLAUDE_CODE_AUTO_COMPACT_WINDOW":"245760"
},"model":"$MODEL","effortLevel":"xhigh",
"permissions":{"defaultMode":"acceptEdits","disableAutoMode":"disable","deny":["WebSearch","WebFetch"]}}
JSON
)

echo "Claude Code -> dogfood praxis -> Qwen3.8-27B-FP8 (in-VPC L40S, \$0 metered)"
exec claude --model "$MODEL" --settings "$SETTINGS" "$@"
