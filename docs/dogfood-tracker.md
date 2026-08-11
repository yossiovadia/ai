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
| Provider credentials secret | DONE | From ~/.env, NOT in manifest |
| Claude Code through gateway | DONE | connect-dogfood.sh, Vertex env override |
| Codex through gateway | DONE | Bearer prefix fix, config.toml dogfood provider |
| create-api-key.sh script | DONE | Auto-names from username |

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
