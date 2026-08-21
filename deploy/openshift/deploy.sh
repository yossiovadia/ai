#!/usr/bin/env bash
# Deploy the AI Gateway dogfood stack to OpenShift.
#
# Prerequisites:
#   - oc login to your cluster
#   - Container images pushed to quay.io/yossiovadia/
#   - ANTHROPIC_API_KEY set in environment
#
# Usage:
#   export ANTHROPIC_API_KEY="sk-ant-..."
#   ./deploy/openshift/deploy.sh

set -euo pipefail

NAMESPACE="ai-gateway-dogfood"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Preflight ─────────────────────────────────────────────────

if ! oc whoami > /dev/null 2>&1; then
    echo "ERROR: not logged in to OpenShift. Run: oc login ..."
    exit 1
fi

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
    echo "ERROR: ANTHROPIC_API_KEY not set."
    echo "  export ANTHROPIC_API_KEY=\"sk-ant-...\""
    exit 1
fi

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
    echo "ERROR: OPENAI_API_KEY not set."
    echo "  export OPENAI_API_KEY=\"sk-...\""
    exit 1
fi

echo "Deploying to: $(oc whoami --show-server)"
echo "Namespace:    $NAMESPACE"
echo ""

# ── Generate password ─────────────────────────────────────────

PG_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=')

# ── Create namespace ──────────────────────────────────────────

oc apply -f "$SCRIPT_DIR/namespace.yaml"

# ── Create secrets with real values ───────────────────────────

oc -n "$NAMESPACE" create secret generic postgresql-credentials \
    --from-literal=POSTGRES_USER=aigateway \
    --from-literal=POSTGRES_PASSWORD="$PG_PASSWORD" \
    --from-literal=POSTGRES_DB=aigateway \
    --from-literal=MAAS_DB_URL="postgresql://aigateway:${PG_PASSWORD}@postgresql:5432/aigateway?sslmode=disable" \
    --from-literal=METERING_DB_URL="postgresql://aigateway:${PG_PASSWORD}@postgresql:5432/aigateway?sslmode=disable" \
    --dry-run=client -o yaml | oc apply -f -

oc -n "$NAMESPACE" create secret generic provider-credentials \
    --from-literal=ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
    --from-literal=OPENAI_API_KEY="$OPENAI_API_KEY" \
    --dry-run=client -o yaml | oc apply -f -

# ── Deploy PostgreSQL ─────────────────────────────────────────

echo "Deploying PostgreSQL..."
oc apply -f "$SCRIPT_DIR/postgresql.yaml"
oc -n "$NAMESPACE" rollout status statefulset/postgresql --timeout=120s

# ── Deploy maas-api ───────────────────────────────────────────

echo "Deploying maas-api..."
oc apply -f "$SCRIPT_DIR/maas-api.yaml"
oc -n "$NAMESPACE" rollout status deployment/maas-api --timeout=120s

# ── Deploy metering service ───────────────────────────────────

echo "Deploying metering service..."
oc apply -f "$SCRIPT_DIR/metering-service.yaml"
oc -n "$NAMESPACE" rollout status deployment/metering-service --timeout=120s

# ── Deploy Praxis ─────────────────────────────────────────────

echo "Deploying Praxis..."
oc apply -f "$SCRIPT_DIR/praxis.yaml"
oc -n "$NAMESPACE" rollout status deployment/praxis --timeout=120s

# ── Create routes ─────────────────────────────────────────────

echo "Creating routes..."
oc apply -f "$SCRIPT_DIR/routes.yaml"

# ── Router ALB idle timeout (IBM Cloud VPC) ───────────────────
# The VPC ALB fronting the OpenShift router defaults to a 50s idle-
# connection timeout and RSTs any turn that stays silent longer — namely
# cold-prompt-cache turns where Anthropic is quiet >50s during prefill.
# The routes' 300s haproxy timeout fixes the router hop but not the ALB
# in front of it, so bump the ALB to 600s. Inert on non-VPC clusters
# (unknown annotation is ignored), so applied unconditionally. Needs
# cluster-admin on openshift-ingress; if RBAC denies it, warn and keep
# going — the gateway works without it, just with cold-turn resets.
# Full story: docs/dogfood-tracker.md → "Ops Findings".

echo "Setting router ALB idle timeout (VPC)..."
if ! oc annotate --overwrite svc router-default -n openshift-ingress \
    "service.kubernetes.io/ibm-load-balancer-cloud-provider-vpc-idle-connection-timeout=600"; then
    echo ""
    echo "WARNING: could not annotate svc/router-default (needs cluster-admin on openshift-ingress)."
    echo "         The gateway will work, but cold-prompt-cache turns may hit ECONNRESET at ~50s."
    echo "         Apply manually with a cluster-admin account:"
    echo "           oc annotate --overwrite svc router-default -n openshift-ingress \\"
    echo "             service.kubernetes.io/ibm-load-balancer-cloud-provider-vpc-idle-connection-timeout=600"
    echo ""
fi

# ── Print connection info ─────────────────────────────────────

ANTHROPIC_ROUTE=$(oc -n "$NAMESPACE" get route ai-gateway-anthropic -o jsonpath='{.spec.host}')
OPENAI_ROUTE=$(oc -n "$NAMESPACE" get route ai-gateway-openai -o jsonpath='{.spec.host}')
DASHBOARD_URL=$(oc -n "$NAMESPACE" get route dashboard -o jsonpath='{.spec.host}')

echo ""
echo "=========================================="
echo "  AI Gateway Dogfood — Deployed"
echo "=========================================="
echo ""
echo "  Anthropic: https://$ANTHROPIC_ROUTE"
echo "  OpenAI:    https://$OPENAI_ROUTE"
echo "  Dashboard: https://$DASHBOARD_URL/dashboard"
echo ""
echo "  Connect Claude Code:"
echo "    ANTHROPIC_BASE_URL=https://$ANTHROPIC_ROUTE claude"
echo ""
echo "  Connect OpenAI SDK:"
echo "    OPENAI_BASE_URL=https://$OPENAI_ROUTE/v1 python your-script.py"
echo ""
echo "  Pods:"
oc -n "$NAMESPACE" get pods
echo ""
