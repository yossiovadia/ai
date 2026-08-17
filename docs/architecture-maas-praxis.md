# AI Gateway Architecture — MaaS + Praxis

**Multi-Provider AI Gateway with Per-User Metering, Model Access Control, and Pipeline Visualization**

Deployed on IBM Cloud OpenShift (OCP 4.21) · Namespace: `ai-gateway-dogfood`

---

## Overview

A production-grade AI gateway that proxies requests from developer tools (Claude Code, Codex CLI, SDKs) to multiple LLM providers (Anthropic, OpenAI), with per-user authentication, usage metering, model access control, and real-time dashboards.

```
                     ┌──────────────────────────────────────────────────────┐
                     │              OpenShift Cluster                      │
                     │              IBM Cloud, us-south-1                  │
                     │                                                     │
  Claude Code ───┐   │   ┌─────────────────────────────────────────────┐   │
  (Anthropic)    │   │   │              Praxis Proxy                   │   │
                 ├───┼──▶│  :8080  Anthropic listener                  │   │
  Codex CLI  ────┤   │   │  :8081  OpenAI listener                    │   │   ┌──────────────┐
  (OpenAI)       │   │   │  :8082  Benchmark listener                 │───┼──▶│ Anthropic API │
                 │   │   │  :9901  Admin (health, metrics, pipeline)  │   │   │ OpenAI API   │
  Any SDK ───────┘   │   └──────┬──────────────┬──────────────────────┘   │   └──────────────┘
                     │          │              │                          │
                     │          ▼              ▼                          │
                     │   ┌──────────┐   ┌─────────────┐                  │
                     │   │ maas-api │   │  metering   │                  │
                     │   │ :8080    │   │  service    │                  │
                     │   │          │   │  :8080      │                  │
                     │   │ API key  │   │             │                  │
                     │   │ mgmt     │   │ dashboard   │                  │
                     │   └────┬─────┘   │ pipeline UI │                  │
                     │        │         └──────┬──────┘                  │
                     │        │                │                          │
                     │        ▼                ▼                          │
                     │   ┌──────────────────────────┐                    │
                     │   │       PostgreSQL          │                    │
                     │   │       :5432               │                    │
                     │   │  api_keys │ usage_events  │                    │
                     │   │  model_pricing            │                    │
                     │   └──────────────────────────┘                    │
                     │                                                     │
                     │   ┌──────────┐   ┌──────────┐                     │
                     │   │llm-katan │   │ status   │                     │
                     │   │:8000     │   │ page     │                     │
                     │   │benchmark │   │ :8080    │                     │
                     │   └──────────┘   └──────────┘                     │
                     └──────────────────────────────────────────────────────┘
```

---

## Components

### Praxis Proxy

The core gateway — a high-performance Rust proxy built on Cloudflare's Pingora framework.

**Three listeners**, each with its own filter pipeline:

| Listener | Port | Target | Purpose |
|----------|------|--------|---------|
| Anthropic | 8080 | api.anthropic.com | Claude Code, Anthropic SDK |
| OpenAI | 8081 | api.openai.com | Codex CLI, OpenAI SDK |
| Benchmark | 8082 | llm-katan (on-cluster) | Load testing, zero cost |

**Admin endpoint** at `:9901` serves `/healthy`, `/metrics` (Prometheus).

**Resources**: 9m CPU, 76Mi memory under 50 concurrent users. **Proxy overhead: ~5ms** per request through the full 10-filter pipeline.

### Filter Pipeline

Each listener runs a chain of filters that process every request and response. Filters execute in order for requests, reverse order for responses.

```
Request path (→):
  api_key_auth → model_access → identity_header_guard → router
  → external_metering → token_count → token_usage_headers
  → credential_injection → headers → load_balancer → Provider

Response path (←):
  Provider → load_balancer → headers → credential_injection
  → token_usage_headers → token_count → external_metering
  → router → identity_header_guard → model_access → api_key_auth
```

#### Filter Descriptions

| Filter | Category | Function |
|--------|----------|----------|
| **api_key_auth** | Auth | Validates `sk-oai-*` keys via HTTP callout to maas-api. Extracts identity (username, groups, subscription) into filter metadata. In-memory cache with 300s TTL. |
| **model_access** | Auth | Reads model name from request body, checks against denylist/allowlist with per-group overrides. Uses `buffered_request_body` in header phase to access both body and metadata. |
| **identity_header_guard** | Security | Captures `x-tenant-*` headers to namespaced metadata, strips before upstream. Prevents identity leakage to providers. First-wins for duplicate headers. |
| **router** | Routing | Matches request path to upstream cluster. |
| **external_metering** | Metering | Reports token usage via CloudEvents to metering service. Fire-and-forget (async, never blocks response). Reads identity from filter metadata. |
| **token_count** | Metering | Extracts usage from response body — streaming SSE and non-streaming JSON. Supports OpenAI (Chat + Responses API), Anthropic, Google, Bedrock, Azure. |
| **token_usage_headers** | Metering | Injects `Praxis-Token-*` response headers from token count metadata. |
| **credential_injection** | Auth | Strips client API key, injects real provider credential from env var. The security boundary — users never see provider keys. |
| **headers** | Protocol | Sets static headers (Host, anthropic-version) before upstream. |
| **load_balancer** | Routing | Selects upstream endpoint. TLS (SNI), connection pooling, health checking. |

### maas-api

API key management service (Go). Part of the Models as a Service platform.

- **Create keys**: `POST /v1/api-keys` — generates `sk-oai-*` format keys with group membership and 90-day expiry
- **Validate keys**: `POST /internal/v1/api-keys/validate` — returns identity (username, groups, subscription)
- **Search/revoke**: Full CRUD operations

Requires MaaS CRDs (MaaSModelRef, MaaSSubscription, MaaSAuthPolicy) installed on the cluster. A `MaaSSubscription` defines which groups can create keys and their model access entitlements.

**Script**: `scripts/manage-keys.sh` — create, list, revoke, delete keys from the command line.

### Metering Service

Usage tracking and dashboard (Go).

- **Event ingestion**: `POST /api/v1/events` — receives CloudEvents from Praxis with token usage per request
- **Dashboard**: Real-time per-user usage analytics, model breakdown, cost attribution
- **Pipeline visualization**: `/pipeline/ui` — animated filter chain visualization, auto-detects Praxis config from ConfigMap
- **Pricing**: Seeded from LiteLLM's public pricing database (220+ models)
- **Routing tab**: Shows live Praxis filter pipeline with click-to-inspect config

### PostgreSQL

Shared database for maas-api and metering service.

| Table | Owner | Purpose |
|-------|-------|---------|
| `api_keys` | maas-api | Key storage (hash-based, never stores raw keys) |
| `usage_events` | metering | Per-request token usage with user attribution |
| `model_pricing` | metering | Model costs from LiteLLM, refreshed on startup |

10Gi PVC, auto-migration on startup for both services.

### llm-katan

Lightweight echo backend for benchmarking ([PyPI](https://pypi.org/project/llm-katan/)).

- Simulates provider latency: `--ttft-ms=800 --itl-ms=15` (realistic TTFT and inter-token latency)
- Speaks both OpenAI and Anthropic API formats
- Zero cost — all traffic through the benchmark listener uses this instead of real providers
- Used by `scripts/benchmark.sh` for multi-user load testing

### Status Page

Static HTML dashboard showing deployment status, operational gaps, and upstream issue tracking. Served by nginx from a ConfigMap.

---

## Authentication Flow

```
1. User runs: export ANTHROPIC_API_KEY="sk-oai-..."
              claude --settings '{"env":{"CLAUDE_CODE_USE_VERTEX":""}}'

2. Claude Code sends: POST /v1/messages
                      x-api-key: sk-oai-...
                      {"model":"claude-fable-5","messages":[...]}

3. api_key_auth:
   - Extracts sk-oai-* from x-api-key header
   - Strips "Bearer " prefix if present (OpenAI clients)
   - Checks in-memory cache (hash lookup, 300s TTL)
   - Cache miss → HTTP POST to maas-api /internal/v1/api-keys/validate
   - maas-api checks PostgreSQL, returns {username, groups, subscription}
   - Writes to filter_metadata: x-tenant-username, x-tenant-group

4. model_access:
   - Reads model from buffered request body: "claude-fable-5"
   - Reads group from filter_metadata: "dogfood-testing"
   - Checks group override: dogfood-testing → allowlist ["*"] → ALLOWED
   - (For ai-eng group: default denylist ["claude-fable-*"] → DENIED)

5. credential_injection:
   - Strips x-api-key: sk-oai-... (client's maas key)
   - Injects x-api-key: sk-ant-api03-... (real Anthropic key from env var)

6. Request forwarded to api.anthropic.com:443 with real credentials
```

---

## Model Access Control

Per-group allowlist/denylist with glob pattern support.

```yaml
filter: model_access
mode: denylist
models:
  - "claude-fable-*"        # blocked for everyone by default
overrides:
  - groups: ["dogfood-testing"]
    mode: allowlist
    models: ["*"]            # this group gets full access
```

- **Default rule**: applies when no group override matches
- **Overrides**: checked in order, first matching group wins
- **Patterns**: exact match or trailing `*` for prefix (`claude-opus-*`)
- **Wildcard**: `"*"` matches any model
- **Group source**: `filter_metadata` key set by `api_key_auth`

---

## Metering & Cost Attribution

Every request is metered with per-user attribution:

```
Request → api_key_auth (extracts identity)
        → token_count (extracts usage from response)
        → external_metering (sends CloudEvent to metering service)
        → metering service (writes to PostgreSQL)
        → dashboard (reads from PostgreSQL, shows per-user breakdown)
```

- **Token extraction**: supports streaming SSE (Anthropic `message_delta`, OpenAI `response.completed`) and non-streaming JSON
- **Cost calculation**: uses LiteLLM's public pricing database (220+ models)
- **Real-time**: events appear on dashboard within seconds
- **Identity**: attributed to the authenticated user, not the provider key

---

## Routes (External Access)

All routes use TLS edge termination with 300s timeout for streaming.

| Route | URL | Purpose |
|-------|-----|---------|
| Anthropic Gateway | `ai-gateway-anthropic-...us-south.containers.appdomain.cloud` | Claude Code |
| OpenAI Gateway | `ai-gateway-openai-...us-south.containers.appdomain.cloud` | Codex CLI |
| Benchmark Gateway | `ai-gateway-benchmark-...us-south.containers.appdomain.cloud` | Load testing |
| Dashboard | `dashboard-...us-south.containers.appdomain.cloud` | Usage + pipeline |
| Status Page | `status-...us-south.containers.appdomain.cloud` | Operational status |

---

## Benchmark Results

20 concurrent users, 200 requests, via `scripts/benchmark.sh`:

| Metric | Value |
|--------|-------|
| Success rate | 100% |
| Proxy overhead | **5ms** |
| p50 latency | 1,778ms (includes 800ms simulated TTFT) |
| p95 latency | 2,246ms |
| Throughput | 7.3 req/s |
| Praxis memory | 76Mi (stable, no growth) |
| Praxis CPU | 9m (at 50 concurrent users) |

Posted to [praxis-proxy Discussion #955](https://github.com/orgs/praxis-proxy/discussions/955).

---

## User Onboarding

### Create a user

```bash
./scripts/manage-keys.sh create user@redhat.com [group]
```

### Connect Claude Code

```bash
export ANTHROPIC_BASE_URL="https://ai-gateway-anthropic-..."
export ANTHROPIC_API_KEY="sk-oai-..."
claude --settings '{"env":{"CLAUDE_CODE_USE_VERTEX":"","ANTHROPIC_VERTEX_PROJECT_ID":"","CLOUD_ML_REGION":""}}'
```

### Connect Codex

```bash
OPENAI_API_KEY="sk-oai-..." codex
```

### Manage keys

```bash
./scripts/manage-keys.sh list                    # all keys
./scripts/manage-keys.sh list user@redhat.com    # one user
./scripts/manage-keys.sh revoke user@redhat.com  # disable access
./scripts/manage-keys.sh delete user@redhat.com  # remove all data
```

---

## Upstream Contributions

### Issues Filed

| # | Repo | Title | Status |
|---|------|-------|--------|
| 698 | praxis-proxy/ai | identity header guard filter | triage/accepted, assigned |
| 707 | praxis-proxy/ai | API key validation filter | needs triage |
| 708 | praxis-proxy/ai | JWT/OIDC authentication filter | Seb approved as bridge |
| 712 | praxis-proxy/ai | model access control (allowlist/denylist) | needs triage |
| 713 | praxis-proxy/ai | OpenAI Responses API token extraction | needs triage |
| 735 | praxis-proxy/ai | runtime pipeline introspection (AI side) | open |
| 975 | praxis-proxy/praxis | runtime pipeline introspection (core) | open |

### PRs Open

| # | Title | Status |
|---|-------|--------|
| 709 | identity header guard filter | Addressing reviewer feedback |
| 714 | OpenAI Responses API usage extraction | CI green, bot finding addressed |

---

## Technology Stack

| Component | Technology | Version |
|-----------|-----------|---------|
| Proxy | Praxis (Rust, Pingora) | 0.2.0 (fork) |
| Key Management | maas-api (Go) | from models-as-a-service |
| Metering | ai-gateway-metering-service (Go) | custom |
| Database | PostgreSQL | 16-alpine |
| Benchmark Backend | llm-katan (Python) | 0.21.0 |
| Platform | OpenShift | 4.21 (IBM Cloud ROKS) |
| Container Builds | OpenShift Binary Builds | Docker strategy |

---

## Source Code

| Repository | Branch | Purpose |
|-----------|--------|---------|
| [yossiovadia/ai](https://github.com/yossiovadia/ai/tree/feat/dogfood-gateway) | `feat/dogfood-gateway` | Praxis proxy + filters + deploy manifests |
| [noyitz/ai-gateway-metering-service](https://github.com/noyitz/ai-gateway-metering-service) | main | Metering service + dashboard (modified) |
| [opendatahub-io/models-as-a-service](https://github.com/opendatahub-io/models-as-a-service) | main | maas-api (upstream) |
| [yossiovadia/llm-katan](https://github.com/yossiovadia/llm-katan) | main | Benchmark echo backend |
