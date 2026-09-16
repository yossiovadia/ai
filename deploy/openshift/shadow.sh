#!/usr/bin/env bash
# shadow.sh — stand up / release / tear down an ISOLATED praxis shadow stack
# alongside the live one in ai-gateway-dogfood, for zero-downtime upgrades.
#
# The shadow is DERIVED from the prod manifests (praxis.yaml, routes.yaml,
# metering-service.yaml) at run time — there is no shadow YAML to drift.
# Isolation guarantees:
#   - distinct names + labels everywhere (app=praxis-shadow), so Services can
#     never select prod pods, and prod Routes keep pointing at praxis.
#   - its own imagestream/BuildConfig (praxis-ai-shadow): prod :latest is
#     never rebuilt from the upgrade branch.
#   - its own metering-service-shadow writing to its own database
#     (aigateway_shadow on the CNPG cluster): shadow traffic NEVER lands in
#     production usage_events.
#
# Subcommands:
#   render     render shadow manifests to stdout (offline, no cluster calls)
#   up         create shadow db + secret + bc + imagestream + deploy + cm +
#              service + *-shadow routes (idempotent)
#   release    build the current git branch into praxis-ai-shadow and roll
#              ONLY the shadow stack (wraps release.sh with env overrides;
#              prod is untouched)
#   status     shadow pods/routes summary
#   teardown   delete shadow k8s resources (optionally the shadow db too)
#
# Cutover to shadow is deliberately NOT in this script: that happens via
# OpenShift route alternateBackends weights (10→50→100), driven by hand, so
# no single command can ever flip prod onto an unproven build.

set -euo pipefail

NAMESPACE="${NAMESPACE:-ai-gateway-dogfood}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SHADOW_PREFIX="praxis-shadow"
SHADOW_BC="praxis-ai-shadow"
SHADOW_IS="praxis-ai-shadow"
SHADOW_ROUTE_PREFIX="ai-gateway"            # routes: ai-gateway-*-shadow
SHADOW_METERING="metering-service-shadow"
SHADOW_DB="aigateway_shadow"
SHADOW_DB_SECRET="metering-shadow-db-url"
CNPG_CLUSTER="aigateway-pg"                  # CNPG cluster serving the app db
CNPG_DB_SECRET="aigateway-db-app"            # holds the app user password

log()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m⚠ %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

require_oc() {
    oc whoami >/dev/null 2>&1 || die "not logged in to OpenShift (oc login ...)"
    oc get ns "$NAMESPACE" >/dev/null 2>&1 || die "namespace $NAMESPACE not found"
}

# ── Renderer ───────────────────────────────────────────────────
# Reads prod manifests, renames + relabels, swaps metering endpoint.
render() {
    python3 - "$SCRIPT_DIR/praxis.yaml" "$SCRIPT_DIR/routes.yaml" "$SCRIPT_DIR/metering-service.yaml" <<'PY'
import sys, yaml

praxis_f, routes_f, metering_f = sys.argv[1:4]

def docs(path):
    with open(path) as f:
        return [d for d in yaml.safe_load_all(f) if d]

def rename_label(node, old, new):
    """Rename name + app labels/selectors in one resource tree, recursively
    enough for our manifests (metadata.name, labels.app, spec selectors and
    template labels, pod env secret refs untouched)."""
    if isinstance(node, dict):
        for k, v in list(node.items()):
            if k == "name" and v == old:
                node[k] = new
            elif k == "app" and v == old:
                node[k] = new
            else:
                rename_label(v, old, new)
    elif isinstance(node, list):
        for item in node:
            rename_label(item, old, new)

out = []

# praxis.yaml: ConfigMap + Deployment + Service
for d in docs(praxis_f):
    kind = d.get("kind")
    if kind == "ConfigMap":
        d["metadata"]["name"] = "praxis-shadow-config"
        key = "praxis.yaml"
        if key in d.get("data", {}):
            # isolate usage events: shadow metering endpoint (port prefix-safe)
            d["data"][key] = d["data"][key].replace(
                "http://metering-service:", "http://metering-service-shadow:")
    elif kind == "Deployment":
        rename_label(d, "praxis", "praxis-shadow")
        d["metadata"]["name"] = "praxis-shadow"
        # rename_label also renames the container to "praxis-shadow" —
        # release.sh is driven with CONTAINER=praxis-shadow to match.
        # Selector + template labels are app=praxis-shadow, so this Service or
        # Deployment can never include prod pods.
        for c in d["spec"]["template"]["spec"]["containers"]:
            if "image" in c:
                c["image"] = c["image"].replace("/praxis-ai:", "/praxis-ai-shadow:")
        # mount the renamed ConfigMap
        for v in d["spec"]["template"]["spec"].get("volumes", []):
            if v.get("configMap", {}).get("name") == "praxis-config":
                v["configMap"]["name"] = "praxis-shadow-config"
    elif kind == "Service":
        rename_label(d, "praxis", "praxis-shadow")
        d["metadata"]["name"] = "praxis-shadow"
    out.append(d)

# routes.yaml: only the four ai-gateway-* routes pointing at praxis
for d in docs(routes_f):
    if d.get("kind") != "Route":
        continue
    if d["spec"].get("to", {}).get("name") != "praxis":
        continue                       # skip dashboard/other routes
    d["metadata"]["name"] = d["metadata"]["name"] + "-shadow"
    d["spec"]["to"]["name"] = "praxis-shadow"
    out.append(d)

# metering-service.yaml: deployment + service twin, DSN from shadow secret
for d in docs(metering_f):
    rename_label(d, "metering-service", "metering-service-shadow")
    if d.get("kind") == "Deployment":
        for c in d["spec"]["template"]["spec"]["containers"]:
            for e in c.get("env", []):
                if e.get("name") == "DATABASE_URL" and "secretKeyRef" in e.get("valueFrom", {}):
                    e["valueFrom"]["secretKeyRef"]["name"] = "metering-shadow-db-url"
                    e["valueFrom"]["secretKeyRef"]["key"] = "DATABASE_URL"
    out.append(d)

print("---\n".join(yaml.safe_dump(d, sort_keys=False, width=100) for d in out))
PY
}

render_file() {
    local f; f="$(mktemp -t praxis-shadow.XXXXXX.yaml)"
    render > "$f"
    echo "$f"
}

# ── up ─────────────────────────────────────────────────────────
up() {
    require_oc
    log "Shadow database $SHADOW_DB on $CNPG_CLUSTER"
    # Declarative: the CNPG operator runs CREATE DATABASE as its own internal
    # superuser. The app role deliberately lacks CREATEDB and this cluster has
    # no superuser secret — a Database CR is the right layer: no remote shell,
    # no password, idempotent apply. Teardown deletes the CR (reclaim: delete
    # default) so the db goes with the rest of the shadow.
    oc -n "$NAMESPACE" apply -f - <<EOF
apiVersion: postgresql.cnpg.io/v1
kind: Database
metadata:
  name: shadow-db
spec:
  cluster:
    name: $CNPG_CLUSTER
  name: $SHADOW_DB
  owner: aigateway
  ensure: present
EOF
    # CNPG Database reports success as status.applied=true, not a Ready condition.
    oc -n "$NAMESPACE" wait --for=jsonpath='{.status.applied}'=true database/shadow-db --timeout=180s \
        || die "shadow db reconcile failed (oc -n $NAMESPACE describe database shadow-db)"

    log "Shadow metering DSN secret (piped, never printed)"
    PW="$(oc -n "$NAMESPACE" get secret "$CNPG_DB_SECRET" -o jsonpath='{.data.password}' | base64 -d)"
    oc -n "$NAMESPACE" create secret generic "$SHADOW_DB_SECRET" \
        --from-literal="DATABASE_URL=postgresql://aigateway:${PW}@${CNPG_CLUSTER}-rw:5432/${SHADOW_DB}?sslmode=disable" \
        --dry-run=client -o yaml | oc -n "$NAMESPACE" apply -f -
    unset PW

    log "Shadow BuildConfig + ImageStream"
    if ! oc -n "$NAMESPACE" get bc "$SHADOW_BC" >/dev/null 2>&1; then
        oc -n "$NAMESPACE" get bc praxis-ai -o json | python3 -c '
import json, sys
bc = json.load(sys.stdin)
for k in ("creationTimestamp","resourceVersion","uid"):
    bc["metadata"].pop(k, None)
bc.pop("status", None)      # prod lastVersion would number shadow builds from 33+
bc["metadata"]["name"] = "'"${SHADOW_BC}"'"
sp = bc["spec"]
sp["output"].get("to", {})["name"] = "'"${SHADOW_IS}"':latest"  # ImageStreamTag needs name:tag
sp.pop("triggers", None)   # ImageChange triggers must not watch the prod imagestream
print(json.dumps(bc))' | oc -n "$NAMESPACE" apply -f -
    fi
    oc -n "$NAMESPACE" create imagestream "$SHADOW_IS" --dry-run=client -o yaml | oc -n "$NAMESPACE" apply -f -

    log "Apply rendered shadow stack"
    local f; f="$(render_file)"
    oc -n "$NAMESPACE" apply -f "$f" --dry-run=server >/dev/null \
        || { rm -f "$f"; die "rendered manifests rejected by API server — nothing applied"; }
    oc -n "$NAMESPACE" apply -f "$f"
    rm -f "$f"

    log "Shadow routes"
    oc -n "$NAMESPACE" get routes -o custom-columns=NAME:.metadata.name,HOST:.spec.host \
        | grep -E 'NAME|shadow' || true
    echo
    echo "shadow up. release a build with:  $0 release"
}

# ── release ────────────────────────────────────────────────────
release() {
    require_oc
    oc -n "$NAMESPACE" get deploy "$SHADOW_PREFIX" >/dev/null 2>&1 || die "run '$0 up' first"
    local cfg; cfg="$(render_file)"
    log "Building $(git -C "$SCRIPT_DIR/../.." rev-parse --abbrev-ref HEAD) → $SHADOW_IS and rolling shadow only"
    DEPLOY="$SHADOW_PREFIX" CONTAINER=praxis-shadow CM_NAME=praxis-shadow-config \
    BC="$SHADOW_BC" IMAGE_STREAM="$SHADOW_IS" SMOKE_ROUTE="ai-gateway-unified-shadow" \
    CONFIG_MANIFEST="$cfg" \
        "$SCRIPT_DIR/release.sh" "$@"
    rm -f "$cfg"
}

# ── status / teardown ─────────────────────────────────────────
status() {
    require_oc
    oc -n "$NAMESPACE" get deploy,svc,route -o wide | grep -E 'NAME|shadow'
    oc -n "$NAMESPACE" get pods -l app=praxis-shadow -o wide || true
}

teardown() {
    require_oc
    log "Deleting shadow k8s resources (prod untouched)"
    NAMES="$(oc -n "$NAMESPACE" get deploy,svc,cm,route -o name \
        | grep -E 'praxis-shadow|metering-service-shadow|ai-gateway-.*shadow')"
    [[ -n "$NAMES" ]] && echo "$NAMES" | xargs oc -n "$NAMESPACE" delete --ignore-not-found
    oc -n "$NAMESPACE" delete job shadow-db-create --ignore-not-found
    oc -n "$NAMESPACE" delete bc "$SHADOW_BC" --ignore-not-found
    oc -n "$NAMESPACE" delete is "$SHADOW_IS" --ignore-not-found
    oc -n "$NAMESPACE" delete secret "$SHADOW_DB_SECRET" --ignore-not-found
    echo "shadow resources gone. db '$SHADOW_DB' KEPT (drop it with: oc -n $NAMESPACE delete database shadow-db)."
}

case "${1:-}" in
    render)   render ;;
    up)       up ;;
    release)  shift; release "$@" ;;
    status)   status ;;
    teardown) teardown ;;
    *)        sed -n '2,32p' "$0"; exit 1 ;;
esac
