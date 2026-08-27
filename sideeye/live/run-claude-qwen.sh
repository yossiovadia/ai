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
# Reads MAAS_API_KEY + UNIFIED_ROUTE from qwen-gpu.env
# (gitignored). Uses --settings to override the Vertex config your
# ~/.claude/settings.json forces on.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/qwen-gpu.env"
: "${MAAS_API_KEY:?MAAS_API_KEY not in qwen-gpu.env}"
# UNIFIED Anthropic-dialect route (one URL, model-based routing) so /model can
# switch between Qwen and Claude in-session.
ROUTE="${UNIFIED_ROUTE:?set UNIFIED_ROUTE in qwen-gpu.env}"
# Judge isolation is now handled by the UNIFIED route itself: a `sideeye
# review/advise` run inherits ANTHROPIC_BASE_URL=$ROUTE (the unified route) and
# sends model=claude-*, which the router sends to the real Anthropic cluster —
# never to Qwen. So SIDEEYE_JUDGE_* is no longer needed (resolve_judge_route falls
# back to ANTHROPIC_*, and judge_route_guard still blocks the old qwen-only route).
# Set SIDEEYE_JUDGE_BASE_URL only if you want to force the judge somewhere else.
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
  "ANTHROPIC_SMALL_FAST_MODEL":"$MODEL",
  "CLAUDE_CODE_AUTO_COMPACT_WINDOW":"245760"
},"model":"$MODEL","effortLevel":"xhigh",
"permissions":{"defaultMode":"acceptEdits","disableAutoMode":"disable","deny":["WebSearch","WebFetch"]}}
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
#
# permissions.deny [WebSearch, WebFetch]: those are Anthropic SERVER-side tools;
# their content blocks (server_tool_use / web_search_tool_result) aren't
# implemented by vLLM, so a "check the web" request 500s ("Unexpected item type
# in content") AND poisons the session (the bad block replays on every retry).
# Qwen has no web access anyway. deny removes the tools from the agent's context
# entirely, so it never attempts them.
#
# CLAUDE_CODE_AUTO_COMPACT_WINDOW 245760: Claude Code doesn't recognize
# Qwen3.8-27B-FP8 and assumes a 200K window for unknown models, so auto-compact
# fires at ~90% of that (~165K input — verified in the metering DB: the
# compaction call metered 165266 in + 14764 out = 90.0% of 200K), long before
# the 262144 vLLM actually serves. 245760 (240K = served limit minus 16K
# headroom for the summary) pushes the trigger to ~220K. Set HERE, not in
# ~/.zshrc: the zshrc value is GLM-tuned (273672 — above Qwen's served ceiling,
# a context-overflow 400 if inherited) and shell env doesn't reliably reach
# the claude process anyway.

echo "Claude Code -> dogfood praxis (unified route) -> Qwen3.8-27B-FP8 (\$0) | /model for Claude"
echo "Metered on the dogfood dashboard."
echo
exec claude --settings "$SETTINGS" --model "$MODEL" "$@"
