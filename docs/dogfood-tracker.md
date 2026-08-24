# Dogfood Gateway — Task Tracker

Last updated: 2026-08-11

## Deployment (IBM Cloud OpenShift)

| Item | Status | Notes |
|------|--------|-------|
| PostgreSQL StatefulSet | DONE | 10Gi PVC, PGDATA fix for OpenShift |
| maas-api (key management) | DONE | CRDs installed, RBAC, MaaSSubscription created |
| metering-service (dashboard) | DONE | 220 model prices, auto-migration |
| Praxis proxy (dual listener) | DONE | Anthropic :8080, OpenAI :8081, admin :9901 |
| Routes (TLS edge) | DONE | 300s timeout for streaming |
| VPC ALB idle timeout | DONE (2026-08-21) | 600s on router-default svc — see Ops Findings below |
| Provider credentials secret | DONE | From ~/.env, NOT in manifest |
| Claude Code through gateway | DONE | connect-dogfood.sh, Vertex env override |
| Codex through gateway | DONE | Bearer prefix fix, config.toml dogfood provider |
| create-api-key.sh script | DONE | Auto-names from username |

## Ops Findings

### ECONNRESET retries in Claude Code — VPC ALB 50s idle timeout (fixed 2026-08-21)

**Symptom:** Claude Code shows `API error · Retrying` / `Connection dropped
(ECONNRESET)`, always on the first turn after a break, never mid-session.

**Fingerprint (how to recognize a recurrence):** praxis logs show
`Fail to proxy: Downstream ReadError … Connection reset by peer (os error
104) … status: 0` on `POST /v1/messages`, repeating at ~50-second
intervals while the client retry-loops. `status: 0` = the kill landed
before Anthropic's first response byte.

**Root cause:** the cluster is IBM Cloud VPC Gen2, so the OpenShift
router sits behind a VPC ALB whose idle connection timeout defaults to
**50 seconds** (account-dependent; annotation range 50–7200). A
cold-prompt-cache turn sends ~200K+ uncached tokens; Anthropic is silent
until prefill completes (>50s), the ALB RSTs both directions, and the
client's instant retry dies the same way until one attempt gets
first-byte under the wire. The earlier route fix
(`haproxy.router.openshift.io/timeout: 300s`) patched the router hop but
not the ALB in front of it. Warm-cache turns answer fast and never trip
it — which is why the symptom looks random.

**Fix (applied 2026-08-21, verified `EnsuredLoadBalancer`):**

```bash
oc annotate svc router-default -n openshift-ingress \
  "service.kubernetes.io/ibm-load-balancer-cloud-provider-vpc-idle-connection-timeout=600"
```

Cluster-wide (all routes behind router-default). Revert by deleting the
annotation (trailing `-`). Any new cluster gets the 50s default again —
this belongs in the deploy script.

**Side effects to know about:** each killed attempt likely still incurs
prefill processing at Anthropic but produces no gateway usage event —
invisible spend, invisible to the dashboard (metering has no
error-event coverage yet). Longer prompt-cache TTL reduces cold turns
and therefore both the cost and the reset exposure — same root
phenomenon as the cache-miss tax.

### Residual reset mode after the ALB fix — stale upstream connections (fix applied 2026-08-21)

After the 600s ALB fix, the 50s kill *chains* stopped, but a rarer single-kill
mode remains (2 events post-fix vs 17/48h before): same praxis log signature
(`Downstream ReadError … status: 0`), but on *warm-cache* turns, one kill
~5 minutes into the attempt, and the client's immediate retry succeeds
instantly. Best-fit mechanism: praxis (Pingora) reuses a pooled keepalive
connection to `api.anthropic.com` that the upstream already silently dropped
(Anthropic idle-kills / rotates IPs); the POST is written into a dead socket,
nothing ever returns (`tries: 1, retry: false` — Pingora can't auto-retry a
non-idempotent request already written), and the route's 300s `timeout server`
eventually reaps the hung client leg. The fresh connection on retry works
immediately — the tell that the pooled conn, not Anthropic, was dead.

**Fix (config, not code — verified in praxis source 2026-08-21):** praxis
already exposes this: cluster-level `idle_timeout_ms` plumbs straight to
Pingora's pool idle timeout (`ConnectionOptions::from(&Cluster)` →
`apply_connection_options` → `peer.options.idle_timeout`). Default is unset
= pooled connections never age out. Add `idle_timeout_ms: 45000` (under the
~60s provider keepalive window) to the anthropic cluster — and any external
upstream cluster — in `deploy/openshift/praxis.yaml`, then apply. Note:
config reload rebuilds pipelines and resets filter state; do it at a quiet
moment. Optional upstream follow-up (docs-grade, not a bug): praxis docs
could recommend `idle_timeout_ms` for keepalive-killing upstreams, or
discuss a safer default.

**Applied 2026-08-21 20:37 UTC** via `oc apply -f deploy/openshift/praxis.yaml`
(anthropic + openai clusters, 45000ms). Hot reload confirmed in logs
(`config reload complete swapped=["anthropic","benchmark","openai"]`), no
pod restart, no traffic disruption. Success criterion: zero new
`Downstream ReadError … status: 0` entries on warm-cache turns (baseline
was ~2/day) — check logs after a few days.

## Upstream Issues (praxis-proxy/ai)

| # | Title | Status | Assigned | Comments |
|---|-------|--------|----------|----------|
| 698 | identity header guard filter | triage/accepted | yossiovadia | 1 thumbs-up |
| 707 | API key validation filter | triage/needs-triage | yossiovadia | volunteered |
| 708 | JWT/OIDC authentication filter | triage/needs-triage | — | volunteered |
| 712 | model access control (allowlist/denylist) | triage/needs-triage | — | new |
| 600 | llm-katan as multi-provider test backend | triage/needs-triage | yossiovadia | 0 comments |
| 577 | external metering filter (Noy) | triage/accepted | noyitz | PR #581 open |

## Upstream PRs (praxis-proxy/ai)

| # | Title | Status | Review | CI |
|---|-------|--------|--------|-----|
| 709 | identity header guard filter | OPEN | no review yet | pending |
| 601 | Anthropic SDK integration tests | OPEN | no review yet | blocked |

## Upstream Discussions

| # | Repo | Title | Status |
|---|------|-------|--------|
| 15 | opendatahub-io/praxis-extproc | lean identity filters proposal | posted update, waiting |

## Open Work (not started)

| Item | Effort | Depends on | Notes |
|------|--------|------------|-------|
| Routing tab Praxis adapter | ~3-4h | — | Plan at docs/plans/2026-08-11-routing-tab-praxis-adapter.md |
| model_access filter implementation | ~2h | #712 triage | allowlist/denylist, body-level enforcement |
| maas-api Route + auth | TBD | — | Currently no external access, port-forward only |
| stream_options injection | ~30 lines | — | IPP gap: inject include_usage=true for streaming metering |
| Per-tenant model policies | M | #712 + auth | Read identity from metadata, apply per-tenant lists |
| Replace MeteringClient with CalloutClient | S | core 0.6 | Current reqwest wrapper is a shim |

## Branch Status

| Branch | Repo | Purpose | Pushed |
|--------|------|---------|--------|
| feat/dogfood-gateway | yossiovadia/ai | Full dogfood (filters + deploy) | yes |
| feat/identity-header-guard | yossiovadia/ai | Clean PR branch for #698/#709 | yes |

## Key URLs

| What | URL |
|------|-----|
| Anthropic gateway | https://ai-gateway-anthropic-ai-gateway-dogfood.dogfood-us-south-1-bxf-4x-f196230f74f7ff44a5b4eeb1003c5bd5-0000.us-south.containers.appdomain.cloud |
| OpenAI gateway | https://ai-gateway-openai-ai-gateway-dogfood.dogfood-us-south-1-bxf-4x-f196230f74f7ff44a5b4eeb1003c5bd5-0000.us-south.containers.appdomain.cloud |
| Dashboard | https://dashboard-ai-gateway-dogfood.dogfood-us-south-1-bxf-4x-f196230f74f7ff44a5b4eeb1003c5bd5-0000.us-south.containers.appdomain.cloud/dashboard |
| Cluster | IBM Cloud, us-south-1, OCP 4.21 |
