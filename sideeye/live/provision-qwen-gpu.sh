#!/usr/bin/env bash
# Provision the Qwen3.8-27B-FP8 GPU box, correctly and reproducibly.
#
#   ./provision-qwen-gpu.sh            # create the new box + FIP + SG, wait until serving
#
# This is the "so it never recurs" deliverable: the box is defined as code
# (this script + qwen-cloud-init.yaml), so rebuilding it is one command instead
# of a manual SSH ritual. It:
#   - uses the RHEL-AI-nvidia image (NVIDIA driver + podman + CDI pre-baked),
#   - bakes the tuned vLLM unit with the FINAL served name (Qwen3.8-27B-FP8) in,
#   - hardens sshd (enabled + OOM-protected) so we can't get locked out again,
#   - sets a break-glass root console password,
#   - exposes :22 for admin but :8000 only inside the VPC (praxis reaches it on
#     the private IP; the old box wrongly exposed :8000 to the whole internet),
#   - verifies vLLM end-to-end over the restored SSH before declaring success,
#   - writes the new INSTANCE_ID / PRIVATE_IP / FIP back into qwen-gpu.env.
#
# It does NOT cut praxis over or delete the old box — those are deliberate,
# separately-confirmed steps (the new box is proven first).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$HERE/qwen-gpu.env"
CLOUD_INIT="$HERE/qwen-cloud-init.yaml"
IMAGE_NAME="ibm-redhat-ai-nvidia-3-4-amd64-1"
NEW_NAME="qwen-l40s-2"
BOOT_GB=250

die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo ">> $*"; }

[ -f "$ENV_FILE" ]   || die "config not found: $ENV_FILE"
[ -f "$CLOUD_INIT" ] || die "cloud-init not found: $CLOUD_INIT"
# shellcheck disable=SC1090
source "$ENV_FILE"

: "${SSH_KEY_ID:?SSH_KEY_ID not in qwen-gpu.env}"
: "${SUBNET_ID:?SUBNET_ID not in qwen-gpu.env}"
: "${VPC_ID:?VPC_ID not in qwen-gpu.env}"
: "${ZONE:?ZONE not in qwen-gpu.env}"
: "${GPU_PROFILE:?GPU_PROFILE not in qwen-gpu.env}"
: "${IBM_RESOURCE_GROUP:?IBM_RESOURCE_GROUP not in qwen-gpu.env}"
REGION="${REGION:-${ZONE%-*}}"
SSH_KEY_FILE="${SSH_PRIVATE_KEY_PATH:-$HOME/.ssh/qwen-gpu-l40s}"

command -v ibmcloud >/dev/null 2>&1 || die "ibmcloud CLI not found"
command -v python3  >/dev/null 2>&1 || die "python3 not found"

# --- auth (reuse the same non-interactive login as qwen-compute.sh) -----------
if ! ibmcloud account show >/dev/null 2>&1; then
    [ -n "${IBMCLOUD_API_KEY:-}" ] || die "not logged in and no IBMCLOUD_API_KEY in qwen-gpu.env"
    log "logging in to IBM Cloud..."
    ibmcloud login --apikey "$IBMCLOUD_API_KEY" -r "$REGION" >/dev/null || die "ibmcloud login failed"
fi
ibmcloud plugin show vpc-infrastructure >/dev/null 2>&1 || die "run: ibmcloud plugin install vpc-infrastructure"
ibmcloud target -r "$REGION" -g "$IBM_RESOURCE_GROUP" >/dev/null 2>&1 || die "could not target $REGION / $IBM_RESOURCE_GROUP"

# --- idempotency: bail if the box already exists ------------------------------
if ibmcloud is instance "$NEW_NAME" >/dev/null 2>&1; then
    die "instance '$NEW_NAME' already exists. Delete it first, or edit NEW_NAME. (Refusing to create a duplicate.)"
fi

# --- resolve the RHEL-AI-nvidia image id --------------------------------------
IMAGE_ID="$(ibmcloud is images --visibility public --output JSON 2>/dev/null \
    | python3 -c 'import sys,json;n=sys.argv[1];print(next((i["id"] for i in json.load(sys.stdin) if i.get("name")==n),""))' "$IMAGE_NAME")"
[ -n "$IMAGE_ID" ] || die "image '$IMAGE_NAME' not found in $REGION"
log "image: $IMAGE_NAME ($IMAGE_ID)"

# --- break-glass root password: reuse if set, else generate + persist ---------
if [ -z "${ROOT_CONSOLE_PASSWORD:-}" ]; then
    ROOT_CONSOLE_PASSWORD="$(openssl rand -hex 24)"   # hex only: sed/console-safe
    printf '\n# break-glass root password for the IBM VNC/serial console\nROOT_CONSOLE_PASSWORD=%s\n' \
        "$ROOT_CONSOLE_PASSWORD" >> "$ENV_FILE"
    log "generated a break-glass root password and saved it to qwen-gpu.env"
fi

# --- security group: :22 from anywhere (key-only SSH), :8000 only from VPC -----
# The old box exposed :8000 to 0.0.0.0/0 — direct model access bypassing praxis
# metering. Here :8000 is reachable only from inside the VPC (praxis hits the
# private IP), which is all that's needed.
SG_NAME="qwen-l40s-sg"
SG_ID="$(ibmcloud is security-groups --output JSON 2>/dev/null \
    | python3 -c 'import sys,json;n=sys.argv[1];print(next((s["id"] for s in json.load(sys.stdin) if s.get("name")==n),""))' "$SG_NAME")"
if [ -z "$SG_ID" ]; then
    log "creating security group $SG_NAME..."
    SG_ID="$(ibmcloud is security-group-create "$SG_NAME" "$VPC_ID" --output JSON 2>/dev/null \
        | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))')"
    [ -n "$SG_ID" ] || die "failed to create security group"
    ibmcloud is security-group-rule-add "$SG_ID" inbound tcp --port-min 22 --port-max 22 --remote 0.0.0.0/0 >/dev/null
    # Allow :8000 from every VPC address prefix (praxis -> private IP).
    for cidr in $(ibmcloud is vpc-address-prefixes "$VPC_ID" --output JSON 2>/dev/null \
        | python3 -c 'import sys,json;[print(p["cidr"]) for p in json.load(sys.stdin)]'); do
        ibmcloud is security-group-rule-add "$SG_ID" inbound tcp --port-min 8000 --port-max 8000 --remote "$cidr" >/dev/null
    done
    # Outbound allow-all. IBM VPC security groups start with NO egress rule (unlike
    # AWS), so without this the box can't do DNS or pull the vLLM image/model.
    ibmcloud is security-group-rule-add "$SG_ID" outbound all --remote 0.0.0.0/0 >/dev/null
fi
log "security group: $SG_NAME ($SG_ID)"

# --- render cloud-init with the break-glass password (never committed) --------
RENDERED="$(mktemp)"; trap 'rm -f "$RENDERED"' EXIT
sed "s/__ROOT_PW__/$ROOT_CONSOLE_PASSWORD/" "$CLOUD_INIT" > "$RENDERED"

# --- create the instance ------------------------------------------------------
log "creating $NEW_NAME ($GPU_PROFILE) in $ZONE ..."
CREATE_JSON="$(ibmcloud is instance-create "$NEW_NAME" "$VPC_ID" "$ZONE" "$GPU_PROFILE" "$SUBNET_ID" \
    --image "$IMAGE_ID" --keys "$SSH_KEY_ID" --sgs "$SG_ID" \
    --boot-volume "{\"name\":\"${NEW_NAME}-boot\",\"volume\":{\"name\":\"${NEW_NAME}-boot\",\"capacity\":${BOOT_GB},\"profile\":{\"name\":\"general-purpose\"}}}" \
    --user-data "$(cat "$RENDERED")" --ms true \
    --resource-group-id "$IBM_RESOURCE_GROUP" --output JSON 2>&1)" \
    || die "instance-create failed:\n$CREATE_JSON"

NEW_ID="$(printf '%s' "$CREATE_JSON" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))' 2>/dev/null)"
[ -n "$NEW_ID" ] || die "could not parse new instance id:\n$CREATE_JSON"
NEW_VNI="$(printf '%s' "$CREATE_JSON" | python3 -c 'import sys,json;d=json.load(sys.stdin);print((d.get("primary_network_attachment",{}) or {}).get("virtual_network_interface",{}).get("id",""))' 2>/dev/null)"
log "created instance $NEW_ID (vni $NEW_VNI)"

# --- reserve + bind a fresh floating IP for admin SSH -------------------------
FIP_NAME="${NEW_NAME}-fip"
ibmcloud is floating-ip-reserve "$FIP_NAME" --zone "$ZONE" >/dev/null 2>&1 || true
NEW_FIP_ID="$(ibmcloud is floating-ip "$FIP_NAME" --output JSON 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))')"
[ -n "$NEW_VNI" ] && ibmcloud is virtual-network-interface-floating-ip-add "$NEW_VNI" "$NEW_FIP_ID" >/dev/null 2>&1 || true
NEW_FIP="$(ibmcloud is floating-ip "$FIP_NAME" --output JSON 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("address",""))')"
log "floating IP: $NEW_FIP"

# --- wait for the private IP + running -----------------------------------------
log "waiting for instance to run..."
for _ in $(seq 1 60); do
    ST="$(ibmcloud is instance "$NEW_ID" --output JSON 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("status","?"))')"
    [ "$ST" = "running" ] && break
    sleep 5
done
NEW_PRIVATE_IP="$(ibmcloud is instance "$NEW_ID" --output JSON 2>/dev/null \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);ni=d.get("primary_network_interface",{}) or {};pi=ni.get("primary_ip") or {};print(pi.get("address") if isinstance(pi,dict) else ni.get("primary_ipv4_address",""))')"
log "status=$ST  private_ip=$NEW_PRIVATE_IP"

# --- wait for SSH (proves sshd hardening worked), then vLLM health over SSH ----
log "waiting for SSH on $NEW_FIP (cloud-init runs sshd on boot)..."
SSH_OPTS=(-i "$SSH_KEY_FILE" -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -o BatchMode=yes)
SSH_USER=""
for _ in $(seq 1 60); do
    for u in cloud-user root; do
        if ssh "${SSH_OPTS[@]}" "$u@$NEW_FIP" true 2>/dev/null; then SSH_USER="$u"; break; fi
    done
    [ -n "$SSH_USER" ] && break
    sleep 10
done
[ -n "$SSH_USER" ] || die "SSH never came up on $NEW_FIP — check the IBM console (root password is in qwen-gpu.env)"
log "SSH up as $SSH_USER@$NEW_FIP"

log "waiting for vLLM to serve (cold start: model download + compile, up to ~15 min)..."
SERVING=""
for _ in $(seq 1 90); do
    if ssh "${SSH_OPTS[@]}" "$SSH_USER@$NEW_FIP" 'curl -sf -m 5 127.0.0.1:8000/health' >/dev/null 2>&1; then
        SERVING=yes; break
    fi
    sleep 10
done
[ -n "$SERVING" ] || die "vLLM did not become healthy in time. SSH in and check: journalctl -u qwen-vllm -n 80"
SERVED="$(ssh "${SSH_OPTS[@]}" "$SSH_USER@$NEW_FIP" 'curl -s -m 8 127.0.0.1:8000/v1/models' 2>/dev/null \
    | python3 -c 'import sys,json;print(",".join(m["id"] for m in json.load(sys.stdin).get("data",[])))' 2>/dev/null)"
log "vLLM healthy. served model(s): $SERVED"

# --- persist the new box's identity to the env --------------------------------
{
  echo ""
  echo "# --- new box provisioned by provision-qwen-gpu.sh ---"
  echo "NEW_INSTANCE_ID=$NEW_ID"
  echo "NEW_PRIVATE_IP=$NEW_PRIVATE_IP"
  echo "NEW_FLOATING_IP=$NEW_FIP"
  echo "NEW_SSH_USER=$SSH_USER"
} >> "$ENV_FILE"

cat <<EOF

============================================================
 New Qwen box is up and SERVING.
   instance : $NEW_ID  ($NEW_NAME)
   private  : $NEW_PRIVATE_IP:8000   <-- point praxis here
   ssh      : ssh -i $SSH_KEY_FILE $SSH_USER@$NEW_FIP
   served   : $SERVED
   console  : break-glass root password is in qwen-gpu.env (ROOT_CONSOLE_PASSWORD)
============================================================
 NEXT (separate, confirmed steps):
   1. Cut praxis over: set the qwen cluster host to $NEW_PRIVATE_IP:8000 in
      deploy/openshift/praxis.yaml, then oc apply + hot-reload.
   2. Verify usage_events shows model=Qwen3.8-27B-FP8, provider=vllm, \$0.
   3. Delete the old box (INSTANCE_ID in qwen-gpu.env) once verified.
EOF
