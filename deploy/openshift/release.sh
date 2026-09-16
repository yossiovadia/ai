#!/usr/bin/env bash
# release.sh — fast, safe, repeatable Praxis code+config update to the dogfood cluster.
#
# This is the single source of truth for shipping a new Praxis build to
# ai-gateway-dogfood. It is idempotent (re-runnable), builds from a git commit
# (not the laptop working tree, so the 50GB target/ never gets uploaded and the
# image is reproducible from a SHA), version-tags each image so rollback is a
# one-liner, gates the rollout on the readiness probe, smoke-tests the live
# route, and AUTO-ROLLS-BACK image+config if anything fails.
#
# Typical use — after merging a fix to the dogfood branch and pushing it:
#   ./deploy/openshift/release.sh
#
# Selective / advanced:
#   ./deploy/openshift/release.sh --ref <sha|branch>   # build a specific commit
#   ./deploy/openshift/release.sh --config-only        # reload praxis.yaml, no rebuild
#   ./deploy/openshift/release.sh --image-only         # rebuild+roll, don't touch config
#   ./deploy/openshift/release.sh --local              # build local committed HEAD (no push needed)
#   ./deploy/openshift/release.sh --skip-smoke         # skip the /v1/models smoke test
#   ./deploy/openshift/release.sh --no-rollback        # leave a broken deploy up for debugging
#
# Smoke test needs a key with access to the unified route. Set SMOKE_API_KEY to
# enable the authenticated /v1/models check; without it the script still gates on
# the readiness probe and a 401 liveness probe (proves the binary + auth are up).
#   export SMOKE_API_KEY="sk-..."

set -euo pipefail

# ── Constants ─────────────────────────────────────────────────
NAMESPACE="ai-gateway-dogfood"
BC="praxis-ai"                       # BuildConfig + ImageStream name
DEPLOY="praxis"
IMAGE_STREAM="praxis-ai"
REGISTRY="image-registry.openshift-image-registry.svc:5000/${NAMESPACE}"
FORK_URI="https://github.com/yossiovadia/ai.git"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CONFIG_MANIFEST="$SCRIPT_DIR/praxis.yaml"
BUILD_TIMEOUT="${BUILD_TIMEOUT:-2400}"     # 40m — cold Rust build
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-180}"  # 3m — pod pull + boot + ready

# ── Args ──────────────────────────────────────────────────────
REF=""            # git ref to build; default = current branch
DO_BUILD=1
DO_CONFIG=1
LOCAL_ARCHIVE=0   # build local committed HEAD via git-archive upload instead of git-source
SKIP_SMOKE=0
ROLLBACK=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ref)          REF="$2"; shift 2 ;;
        --config-only)  DO_BUILD=0; shift ;;
        --image-only)   DO_CONFIG=0; shift ;;
        --local)        LOCAL_ARCHIVE=1; shift ;;
        --skip-smoke)   SKIP_SMOKE=1; shift ;;
        --no-rollback)  ROLLBACK=0; shift ;;
        -h|--help)      sed -n '2,40p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

log()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m⚠ %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ── Preflight ─────────────────────────────────────────────────
log "Preflight"
oc whoami >/dev/null 2>&1 || die "not logged in to OpenShift (oc login ...)"
oc get ns "$NAMESPACE" >/dev/null 2>&1 || die "namespace $NAMESPACE not found on $(oc whoami --show-server)"
command -v git >/dev/null || die "git not found"
echo "  cluster:   $(oc whoami --show-server)"
echo "  namespace: $NAMESPACE"

# Resolve the commit we're shipping.
BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD)"
REF="${REF:-$BRANCH}"
SHA="$(git -C "$REPO_ROOT" rev-parse --short "$REF")"
echo "  branch:    $BRANCH"
echo "  building:  $REF ($SHA)"
IMAGE_TAG="git-${SHA}"
NEW_IMAGE="${REGISTRY}/${IMAGE_STREAM}:${IMAGE_TAG}"

# ── Snapshot current state for rollback ───────────────────────
# Capture BEFORE we mutate anything so failure can restore exactly this.
PREV_IMAGE="$(oc -n "$NAMESPACE" get deploy "$DEPLOY" -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)"
CONFIG_BAK="$(mktemp -t praxis-config.XXXXXX.yaml)"
oc -n "$NAMESPACE" get cm praxis-config -o yaml 2>/dev/null > "$CONFIG_BAK" || true
echo "  prev image: ${PREV_IMAGE:-<none>}"

MUTATED=0   # set once we've changed cluster state, so the trap knows to roll back
rollback() {
    [[ "$ROLLBACK" == 1 && "$MUTATED" == 1 ]] || return 0
    warn "Rolling back to last-known-good (image + config)…"
    [[ -s "$CONFIG_BAK" ]] && oc -n "$NAMESPACE" apply -f "$CONFIG_BAK" >/dev/null 2>&1 || true
    if [[ -n "$PREV_IMAGE" ]]; then
        oc -n "$NAMESPACE" set image "deploy/$DEPLOY" "$DEPLOY=$PREV_IMAGE" >/dev/null 2>&1 || true
    fi
    oc -n "$NAMESPACE" rollout status "deploy/$DEPLOY" --timeout="${ROLLOUT_TIMEOUT}s" >/dev/null 2>&1 \
        && warn "Rolled back. Cluster is on the previous good revision." \
        || warn "Rollback issued but rollout did not settle — check 'oc get pods -n $NAMESPACE'."
}
cleanup() { rm -f "$CONFIG_BAK" 2>/dev/null || true; }
# shellcheck disable=SC2154  # rc is assigned ($?) at the top of the same trap body
trap 'rc=$?; if [[ $rc -ne 0 ]]; then rollback; fi; cleanup; exit $rc' EXIT

# ── Build (from git commit — reproducible, no working-tree upload) ─
if [[ "$DO_BUILD" == 1 ]]; then
    if [[ "$LOCAL_ARCHIVE" == 1 ]]; then
        # Build the local committed HEAD without needing a push. git archive
        # gives ONLY committed files (no target/, no .git), so the upload is a
        # few MB, not 50GB. Kept as an escape hatch for local iteration.
        log "Build $SHA (local committed source, archive upload)"
        SRC_DIR="$(mktemp -d -t praxis-src.XXXXXX)"
        git -C "$REPO_ROOT" archive "$REF" | tar -x -C "$SRC_DIR"
        oc -n "$NAMESPACE" start-build "$BC" --from-dir="$SRC_DIR" --wait \
            || { rm -rf "$SRC_DIR"; die "build failed (see log above)"; }
        rm -rf "$SRC_DIR"
    else
        # Build from the fork on GitHub. Requires the commit to be pushed —
        # this makes GitHub the source of truth and the image reproducible.
        log "Build $SHA (git source: $FORK_URI @ $REF)"
        REMOTE_SHA="$(git -C "$REPO_ROOT" ls-remote fork "$REF" 2>/dev/null | awk '{print $1}' | head -1)"
        LOCAL_FULL="$(git -C "$REPO_ROOT" rev-parse "$REF")"
        if [[ -z "$REMOTE_SHA" ]]; then
            die "ref '$REF' not found on fork ($FORK_URI). Push it first, or use --local."
        fi
        if [[ "$REMOTE_SHA" != "$LOCAL_FULL" ]]; then
            warn "local $REF ($LOCAL_FULL) != fork $REF ($REMOTE_SHA)."
            warn "Building what's on the FORK ($REMOTE_SHA). Push to build local commits, or use --local."
        fi
        # Ensure the BuildConfig pulls from git (idempotent — patch only if needed).
        SRC_TYPE="$(oc -n "$NAMESPACE" get bc "$BC" -o jsonpath='{.spec.source.type}' 2>/dev/null || true)"
        if [[ "$SRC_TYPE" != "Git" ]]; then
            log "Switching BuildConfig $BC to Git source (one-time)"
            oc -n "$NAMESPACE" patch bc "$BC" --type merge -p \
              "{\"spec\":{\"source\":{\"type\":\"Git\",\"git\":{\"uri\":\"$FORK_URI\",\"ref\":\"$BRANCH\"},\"binary\":null}}}"
        fi
        oc -n "$NAMESPACE" start-build "$BC" --commit="$SHA" --wait \
            || die "build failed (run: oc logs -f bc/$BC -n $NAMESPACE)"
    fi

    # Confirm the latest build actually Completed (—wait returns 0 on some
    # non-fatal paths; verify the phase explicitly).
    LAST_BUILD="$(oc -n "$NAMESPACE" get builds -l "buildconfig=$BC" \
        --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1:].metadata.name}')"
    PHASE="$(oc -n "$NAMESPACE" get build "$LAST_BUILD" -o jsonpath='{.status.phase}')"
    [[ "$PHASE" == "Complete" ]] || die "build $LAST_BUILD phase=$PHASE (expected Complete)"
    echo "  build $LAST_BUILD: Complete"

    # Version-tag the fresh image so we can pin/rollback to this exact SHA.
    log "Tag image → ${IMAGE_STREAM}:${IMAGE_TAG}"
    oc -n "$NAMESPACE" tag "${IMAGE_STREAM}:latest" "${IMAGE_STREAM}:${IMAGE_TAG}"
fi

# ── Config: apply praxis.yaml (server validates on reload/boot) ─
MUTATED=1
if [[ "$DO_CONFIG" == 1 ]]; then
    log "Apply config ($CONFIG_MANIFEST)"
    # --dry-run=server catches k8s-schema errors before we touch the live CM;
    # praxis-semantic errors surface as a failed readiness probe below.
    oc -n "$NAMESPACE" apply -f "$CONFIG_MANIFEST" --dry-run=server >/dev/null \
        || die "praxis.yaml rejected by API server (schema error) — nothing changed"
    oc -n "$NAMESPACE" apply -f "$CONFIG_MANIFEST"
fi

# ── Roll ──────────────────────────────────────────────────────
# Pin the Deployment to the SHA image (explicit version beats :latest, which
# never auto-rolls). If we didn't build this run, just restart to pick up config.
log "Roll out"
if [[ "$DO_BUILD" == 1 ]]; then
    oc -n "$NAMESPACE" set image "deploy/$DEPLOY" "$DEPLOY=$NEW_IMAGE"
    oc -n "$NAMESPACE" annotate "deploy/$DEPLOY" \
        "kubernetes.io/change-cause=release.sh $IMAGE_TAG $(date -u +%FT%TZ)" --overwrite >/dev/null
else
    oc -n "$NAMESPACE" rollout restart "deploy/$DEPLOY"
fi
# Gate on the readiness probe — a config that praxis can't parse fails to
# become Ready, so this is where a bad config is caught (→ trap → rollback).
oc -n "$NAMESPACE" rollout status "deploy/$DEPLOY" --timeout="${ROLLOUT_TIMEOUT}s" \
    || die "rollout did not become Ready in ${ROLLOUT_TIMEOUT}s (bad config or crash)"

# ── Smoke test ────────────────────────────────────────────────
if [[ "$SKIP_SMOKE" == 0 ]]; then
    log "Smoke test (unified route)"
    HOST="$(oc -n "$NAMESPACE" get route ai-gateway-unified -o jsonpath='{.spec.host}' 2>/dev/null || true)"
    [[ -n "$HOST" ]] || die "route ai-gateway-unified not found"
    if [[ -n "${SMOKE_API_KEY:-}" ]]; then
        BODY="$(curl -fsS --max-time 20 -H "x-api-key: $SMOKE_API_KEY" \
                 "https://$HOST/v1/models" 2>/dev/null)" \
            || die "GET /v1/models failed (authenticated)"
        echo "$BODY" | grep -q '"data"\|"id"' \
            || die "GET /v1/models returned no model list: $BODY"
        echo "  /v1/models OK — models: $(echo "$BODY" | grep -oE '"id"[^,]*' | wc -l | tr -d ' ')"
    else
        # No key: prove the binary is serving and auth is wired (401, not a hang/5xx).
        CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "https://$HOST/v1/models" || true)"
        [[ "$CODE" == "401" || "$CODE" == "403" ]] \
            || die "liveness probe: expected 401/403 from /v1/models, got $CODE (set SMOKE_API_KEY for a full check)"
        echo "  liveness OK — /v1/models → $CODE (auth enforced). Set SMOKE_API_KEY for a full model-list check."
    fi
fi

# Success — disarm the rollback trap.
MUTATED=0
trap - EXIT
cleanup

log "Released"
echo "  image:  ${IMAGE_STREAM}:${IMAGE_TAG}  (rollback: oc set image deploy/$DEPLOY $DEPLOY=$PREV_IMAGE -n $NAMESPACE)"
echo "  config: $(basename "$CONFIG_MANIFEST")"
echo "  pods:"
oc -n "$NAMESPACE" get pods -l app="$DEPLOY"
