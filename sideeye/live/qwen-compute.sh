#!/usr/bin/env bash
# Power the self-hosted Qwen3.8-27B-FP8 GPU box on/off to save money when idle.
#
#   ./qwen-compute.sh status          # is it running? is vLLM serving?
#   ./qwen-compute.sh start [--wait]  # power on (--wait: block until serving)
#   ./qwen-compute.sh stop            # power off (compute billing pauses)
#   ./qwen-compute.sh restart [--wait]
#
# WHY ibmcloud and NOT oc: the Qwen model runs on a standalone IBM Cloud VPC
# virtual server (the L40S GPU instance), OUTSIDE the OpenShift cluster — ROKS in
# this VPC has no GPU nodes. `oc` only manages the cluster (pods, routes); it
# cannot power a VPC VM on/off. Only the IBM Cloud VPC API can. The praxis pod in
# OpenShift merely ROUTES traffic to this VM's IP; it can't start or stop it.
#
# Stopping pauses the expensive compute charge. The boot volume + floating IP
# still incur small charges, and the VNI/private IP (10.240.0.9) + floating IP
# PERSIST across stop/start — so once vLLM is back up, the praxis route just works
# again with no reconfiguration. vLLM auto-starts via its systemd unit on boot
# (qwen-vllm.service, Restart=always), with the torch.compile cache mounted so
# the restart is faster than a cold first boot.
#
# Auth: reads config (incl. an optional IBMCLOUD_API_KEY) from qwen-gpu.env
# (gitignored). If that key is present the script logs in non-interactively so you
# never have to run `ibmcloud login` by hand; otherwise it reuses an existing
# session, or tells you exactly how to log in. No secret is ever printed.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$HERE/qwen-gpu.env"

die() { echo "ERROR: $*" >&2; exit 1; }

[ -f "$ENV_FILE" ] || die "config not found: $ENV_FILE"
# shellcheck disable=SC1090
source "$ENV_FILE"

: "${INSTANCE_ID:?INSTANCE_ID not in qwen-gpu.env}"
: "${ZONE:?ZONE not in qwen-gpu.env}"
# Region is the zone minus its trailing "-N" (us-south-1 -> us-south). Override
# with REGION in the env if your zone naming ever differs from this convention.
REGION="${REGION:-${ZONE%-*}}"
# The readiness probe must use vLLM's actual --served-model-name, or it 404s
# ("model does not exist") even when the box is serving fine. The running unit
# serves "qwen"; override with QWEN_SERVED_MODEL in the env if you rename it.
SERVED_MODEL="${QWEN_SERVED_MODEL:-qwen}"

command -v ibmcloud >/dev/null 2>&1 || die "ibmcloud CLI not found. Install: https://cloud.ibm.com/docs/cli"
command -v python3  >/dev/null 2>&1 || die "python3 not found (needed to parse ibmcloud JSON)"

ensure_plugin() {
    # The `ibmcloud is` command group comes from the vpc-infrastructure plugin.
    # Detect it via `plugin show`, which works without being logged in — unlike
    # `ibmcloud is --help`, which demands a session first and would misreport a
    # not-logged-in state as a missing plugin.
    if ! ibmcloud plugin show vpc-infrastructure >/dev/null 2>&1; then
        die "the VPC plugin is missing. Install it:  ibmcloud plugin install vpc-infrastructure"
    fi
}

ensure_login() {
    # Already have a valid session? Reuse it.
    if ibmcloud account show >/dev/null 2>&1; then
        return
    fi
    # Non-interactive login if an API key is provided in the env (preferred, so
    # you never type `ibmcloud login`). The key stays in the gitignored env file
    # and is never echoed.
    if [ -n "${IBMCLOUD_API_KEY:-}" ]; then
        echo "Logging in to IBM Cloud (API key from qwen-gpu.env)..."
        ibmcloud login --apikey "$IBMCLOUD_API_KEY" -r "$REGION" >/dev/null \
            || die "ibmcloud login failed with the API key in qwen-gpu.env (is it valid / not expired?)"
        return
    fi
    die "not logged in to IBM Cloud. Either:
  - add IBMCLOUD_API_KEY=<key> to qwen-gpu.env (create one at
    https://cloud.ibm.com/iam/apikeys) so this script logs in for you, or
  - run 'ibmcloud login --sso' yourself, then re-run this script."
}

ensure_target() {
    ibmcloud target -r "$REGION" >/dev/null 2>&1 \
        || die "could not target region '$REGION' (derived from ZONE=$ZONE)"
    if [ -n "${IBM_RESOURCE_GROUP:-}" ]; then
        ibmcloud target -g "$IBM_RESOURCE_GROUP" >/dev/null 2>&1 || true
    fi
}

# Current instance status: running | stopped | starting | stopping | pausing | ...
# Prints the raw status string, or "notfound" if the instance is gone.
instance_status() {
    local json
    if ! json="$(ibmcloud is instance "$INSTANCE_ID" --output JSON 2>/dev/null)"; then
        echo "notfound"; return
    fi
    printf '%s' "$json" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("status","unknown"))'
}

# Poll the live Qwen route end-to-end (praxis route -> vLLM) until it answers,
# so --wait means "actually serving requests", not just "VM powered on". Uses a
# minimal 1-token message; Qwen is metered at $0 so the probe is free. Needs
# QWEN_ROUTE + MAAS_API_KEY in the env; skipped with a note if either is absent.
wait_for_serving() {
    local timeout="${1:-600}" waited=0 code
    if [ -z "${QWEN_ROUTE:-}" ] || [ -z "${MAAS_API_KEY:-}" ]; then
        echo "  (QWEN_ROUTE/MAAS_API_KEY not set — skipping serving check; VM is on,"
        echo "   vLLM typically needs another ~1-3 min to load the model + compile cache)"
        return
    fi
    echo "  waiting for vLLM to serve on the Qwen route (up to ${timeout}s)..."
    while [ "$waited" -lt "$timeout" ]; do
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
            -X POST "$QWEN_ROUTE/v1/messages" \
            -H "x-api-key: $MAAS_API_KEY" \
            -H "anthropic-version: 2023-06-01" \
            -H "content-type: application/json" \
            -d '{"model":"'"$SERVED_MODEL"'","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}' \
            2>/dev/null || echo 000)"
        if [ "$code" = "200" ]; then
            echo "  serving (HTTP 200 from $QWEN_ROUTE)"
            return
        fi
        sleep 10; waited=$((waited + 10))
        printf '  ...still warming up (%ss, last HTTP %s)\n' "$waited" "$code"
    done
    die "timed out after ${timeout}s waiting for the Qwen route to serve (last HTTP ${code:-none})"
}

# Wait for the VM to reach a target status before doing route checks.
wait_for_status() {
    local target="$1" timeout="${2:-300}" waited=0 st
    while [ "$waited" -lt "$timeout" ]; do
        st="$(instance_status)"
        [ "$st" = "$target" ] && return
        sleep 5; waited=$((waited + 5))
    done
    die "timed out after ${timeout}s waiting for instance to be '$target' (now: $(instance_status))"
}

cmd_status() {
    local st; st="$(instance_status)"
    echo "Instance : $INSTANCE_ID"
    echo "Region   : $REGION (zone $ZONE)"
    echo "Status   : $st"
    [ "$st" = "notfound" ] && die "instance not found in region $REGION — wrong INSTANCE_ID/REGION, or it was deleted."
    if [ "$st" = "running" ] && [ -n "${QWEN_ROUTE:-}" ] && [ -n "${MAAS_API_KEY:-}" ]; then
        local code
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
            -X POST "$QWEN_ROUTE/v1/messages" -H "x-api-key: $MAAS_API_KEY" \
            -H "anthropic-version: 2023-06-01" -H "content-type: application/json" \
            -d '{"model":"'"$SERVED_MODEL"'","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}' \
            2>/dev/null || echo 000)"
        if [ "$code" = "200" ]; then
            echo "Serving  : yes (HTTP 200 on the Qwen route)"
        else
            echo "Serving  : NOT yet (route HTTP $code — VM is up but vLLM is still loading, or down)"
        fi
    fi
}

cmd_start() {
    local wait="$1" st; st="$(instance_status)"
    case "$st" in
        notfound) die "instance not found in region $REGION — check INSTANCE_ID/REGION.";;
        running)  echo "Already running — nothing to start.";;
        starting) echo "Already starting...";;
        stopped|paused|"")
            echo "Starting $INSTANCE_ID ..."
            ibmcloud is instance-start "$INSTANCE_ID" >/dev/null \
                || die "instance-start failed"
            ;;
        *) echo "Instance is '$st'; issuing start anyway..."
           ibmcloud is instance-start "$INSTANCE_ID" >/dev/null || die "instance-start failed";;
    esac
    if [ "$wait" = "wait" ]; then
        wait_for_status running 300
        echo "VM running. Checking vLLM..."
        wait_for_serving 600
    else
        echo "Started. vLLM auto-starts on boot (~1-3 min). Use 'status' or 'start --wait' to confirm serving."
    fi
}

cmd_stop() {
    local st; st="$(instance_status)"
    case "$st" in
        notfound)        die "instance not found in region $REGION — check INSTANCE_ID/REGION.";;
        stopped|paused)  echo "Already $st — nothing to stop.";;
        stopping)        echo "Already stopping...";;
        *)
            echo "Stopping $INSTANCE_ID (graceful)..."
            # -f skips the interactive confirmation prompt, which otherwise fails
            # with "Could not read from input: EOF" when run non-interactively.
            ibmcloud is instance-stop "$INSTANCE_ID" -f >/dev/null || die "instance-stop failed"
            echo "Stop requested. Compute billing pauses once it powers off."
            echo "(Boot volume + floating IP still incur small charges; the IP is kept so"
            echo " the praxis route works again as soon as you start it back up.)"
            ;;
    esac
}

usage() {
    grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed '1d'
    exit "${1:-0}"
}

main() {
    local action="${1:-}" flag="${2:-}"
    local wait="nowait"
    [ "$flag" = "--wait" ] && wait="wait"
    case "$action" in
        status)  ensure_plugin; ensure_login; ensure_target; cmd_status;;
        start)   ensure_plugin; ensure_login; ensure_target; cmd_start "$wait";;
        stop)    ensure_plugin; ensure_login; ensure_target; cmd_stop;;
        restart) ensure_plugin; ensure_login; ensure_target; cmd_stop
                 echo; echo "Waiting for full stop before restart..."; wait_for_status stopped 300
                 echo; cmd_start "$wait";;
        ""|-h|--help) usage 0;;
        *) echo "unknown command: $action" >&2; echo; usage 1;;
    esac
}

main "$@"
