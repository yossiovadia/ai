#!/usr/bin/env bash
# Bring up the Side-Eye live GLM stack, idempotently.
#
#   tunnel (18443) -> GLM via corp proxy    [needs Red Hat VPN]
#   metering (:9090) + postgres             [docker compose]
#   praxis (:8180 openai, :8181 anthropic)  [-> GLM via tunnel]
#
# Safe to run repeatedly: it starts only what isn't already up and the GLM $0
# pricing row uses ON CONFLICT DO NOTHING. Secrets come from ~/.glm-52 and env
# only — never hardcoded, never logged.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
METERING_REPO="${METERING_REPO:-$REPO/../ai-gateway-metering-service}"
KEY_FILE="$HOME/.glm-52"
PROXY_HOST="10.2.32.57"; PROXY_PORT="3128"
TUNNEL_PORT="18443"
GLM_MODEL="rits/zai-org/glm-5-2-fp8"

say() { echo "  $*"; }

echo "== Side-Eye live GLM stack =="

# --- Preflight ---------------------------------------------------------
[[ -f "$KEY_FILE" ]] || { echo "ERROR: GLM key not found at $KEY_FILE" >&2; exit 1; }
if ! timeout 6 bash -c "exec 3<>/dev/tcp/$PROXY_HOST/$PROXY_PORT" 2>/dev/null; then
    echo "ERROR: corp proxy $PROXY_HOST:$PROXY_PORT unreachable — connect the Red Hat VPN." >&2
    exit 1
fi
say "VPN/proxy reachable."
docker info >/dev/null 2>&1 || { echo "ERROR: docker is not running." >&2; exit 1; }
[[ -d "$METERING_REPO" ]] || { echo "ERROR: metering repo not at $METERING_REPO" >&2; exit 1; }

# --- Tunnel ------------------------------------------------------------
if lsof -nP -iTCP:$TUNNEL_PORT -sTCP:LISTEN >/dev/null 2>&1; then
    say "tunnel already up on :$TUNNEL_PORT"
else
    ( cd "$REPO" && GLM_TUNNEL_PORT=$TUNNEL_PORT nohup python -m sideeye.live.glm_tunnel \
        > /tmp/glm_tunnel.log 2>&1 & )
    sleep 2
    lsof -nP -iTCP:$TUNNEL_PORT -sTCP:LISTEN >/dev/null 2>&1 \
        && say "tunnel started on :$TUNNEL_PORT" \
        || { echo "ERROR: tunnel failed to start (see /tmp/glm_tunnel.log)" >&2; exit 1; }
fi

# --- Metering stack ----------------------------------------------------
( cd "$METERING_REPO" && docker compose up -d >/dev/null 2>&1 )
for _ in $(seq 1 20); do
    [[ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:9090/health 2>/dev/null)" == "200" ]] && break
    # postgres race: nudge the service once
    ( cd "$METERING_REPO" && docker compose up -d metering-service >/dev/null 2>&1 || true )
    sleep 2
done
[[ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:9090/health 2>/dev/null)" == "200" ]] \
    && say "metering healthy on :9090" \
    || { echo "ERROR: metering service not healthy on :9090" >&2; exit 1; }

# GLM $0 pricing row (idempotent).
( cd "$METERING_REPO" && docker compose exec -T postgres psql -U metering -d metering -c \
    "INSERT INTO model_pricing (model, provider, input_cost_per_mtok, output_cost_per_mtok, cache_write_cost_per_mtok, cache_read_cost_per_mtok) VALUES ('$GLM_MODEL','glm',0,0,0,0) ON CONFLICT (model) DO NOTHING;" >/dev/null 2>&1 )
say "GLM \$0 pricing row ensured."

# --- Praxis ------------------------------------------------------------
if lsof -nP -iTCP:8181 -sTCP:LISTEN >/dev/null 2>&1; then
    say "praxis already up on :8180/:8181"
else
    [[ -x "$REPO/target/debug/praxis-ai" ]] || { echo "ERROR: build praxis first: cargo build -p praxis-ai-proxy" >&2; exit 1; }
    ( cd "$REPO" && LITELLM_API_KEY="$(tr -d '[:space:]' < "$KEY_FILE")" \
        nohup ./target/debug/praxis-ai -c sideeye/live/praxis-glm.yaml \
        > /tmp/praxis-glm.log 2>&1 & )
    sleep 3
    lsof -nP -iTCP:8181 -sTCP:LISTEN >/dev/null 2>&1 \
        && say "praxis started (:8180 openai, :8181 anthropic)" \
        || { echo "ERROR: praxis failed to start (see /tmp/praxis-glm.log)" >&2; exit 1; }
fi

cat <<EOF

Stack is up. Next:
  1. Point Claude Code at GLM:   ./sideeye/live/run-claude-glm.sh
  2. Watch GLM usage (\$0):        http://127.0.0.1:9090/dashboard
  3. Ask the expensive judge:     python -m sideeye.escalate
  4. Savings report:              python -m sideeye.cost_report --html sideeye/verdicts/cost-report.html
EOF
