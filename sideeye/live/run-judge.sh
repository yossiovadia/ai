#!/usr/bin/env bash
# Run the Side-Eye judge (escalate / sampler) against the REAL Claude route.
#
# The judge must NEVER grade via the Qwen route — a Qwen judge reviewing a Qwen
# session is a self-graded scoreboard, and vLLM serves the Anthropic API
# natively, so that mistake fails silently. This script pins SIDEEYE_JUDGE_* to
# JUDGE_ROUTE (the dogfood Anthropic route) so it's correct no matter what
# ANTHROPIC_BASE_URL the calling shell happens to have (e.g. the qwen route in a
# run-claude-qwen shell). Judge traffic meters onto the SAME dogfood dashboard
# as the Qwen generation traffic — one gateway, one metering service.
#
# Reads MAAS_API_KEY + JUDGE_ROUTE from qwen-gpu.env (gitignored). Pass all
# judge args through, e.g.:
#   ./run-judge.sh --model claude-opus-4-8
#   ./run-judge.sh --model claude-fable-5 --rollout <path>
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/qwen-gpu.env"
: "${MAAS_API_KEY:?MAAS_API_KEY not in qwen-gpu.env}"
: "${JUDGE_ROUTE:?JUDGE_ROUTE not in qwen-gpu.env}"

export SIDEEYE_JUDGE_BASE_URL="$JUDGE_ROUTE"
export SIDEEYE_JUDGE_API_KEY="$MAAS_API_KEY"
# The sideeye package lives at the praxis-ai repo root, not in sideeye/live.
export PYTHONPATH="$(cd "$HERE/../.." && pwd):${PYTHONPATH:-}"

echo "Judge -> dogfood Anthropic route (real Claude), never Qwen."
echo "Usage lands on the same dogfood dashboard as the Qwen traffic."
echo

exec python -m sideeye.escalate "$@"
