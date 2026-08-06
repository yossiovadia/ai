# IPP → praxis-ai Gap Analysis

> Generated 2026-07-28. Compares ai-gateway-payload-processing (Go, K8s sidecar)
> plugin features against praxis-ai (Rust, standalone proxy) filter capabilities.

## Legend

| Status | Meaning |
|--------|---------|
| **PORTED** | Equivalent or better capability exists in praxis-ai |
| **PARTIAL** | Some functionality exists, key pieces missing |
| **MISSING** | No equivalent in praxis-ai |
| **IN-FLIGHT** | Open PR (#581 — Noy's external metering filter) |

---

## Plugin-by-Plugin Comparison

### 1. `model-provider-resolver` — PARTIAL (biggest gap)

**IPP**: Watches `ExternalModel` + `ExternalProvider` K8s CRDs via controller-runtime
reconcilers. Resolves model name → provider endpoint, target model, API format, auth
type, credential secret ref, path override. Weighted random selection across multiple
provider refs. Writes 10 CycleState keys that every downstream plugin reads. This is
the linchpin — everything else depends on it.

**praxis-ai**: Has `model_to_header` which promotes `model` from JSON body to an
`x-praxis-ai-model` header. The classifier detects request format (Responses /
Anthropic Messages / Chat Completions). But there's **no model→provider resolution,
no weighted routing, no endpoint/path/credential resolution**.

**Gap**: This is the big architectural one. IPP is a K8s controller; praxis is a
standalone proxy. The equivalent would be a YAML-driven model routing table — model
aliases map to upstream clusters with API format, credential source, path templates,
weighted backends. Everything in praxis-ai's filter metadata / internal headers
instead of CycleState.

---

### 2. `api-translation` — PARTIAL

**IPP**: Single plugin with a translator registry. Supports these translation pairs:

| Input → Output | Translator |
|---|---|
| OpenAI Chat → OpenAI Chat | Path rewrite only |
| OpenAI Chat → Anthropic Messages | Full bidirectional body translation (~750 lines) |
| OpenAI Chat → Vertex Anthropic | Delegates to Anthropic + Vertex adjustments |
| OpenAI Chat → Vertex Gemini | Full bidirectional (~750 lines) |
| OpenAI Chat → Azure OpenAI | Path rewrite + response field stripping |
| OpenAI Chat → Bedrock OpenAI | Path rewrite only |
| Vertex Messages → Vertex Messages | Passthrough with `anthropic_version` injection |

Also handles: passthrough detection, `:path` rewriting, `authorization` header
stripping, `ConfigAwareTranslator` for per-model config.

**praxis-ai has**:
- `anthropic_to_openai` — Anthropic Messages → OpenAI Chat (request + response, streaming)
- `anthropic_messages_format` — classifies/normalizes Anthropic Messages
- `anthropic_stream_events` — transforms OpenAI SSE → Anthropic SSE (streaming)
- `openai_responses_format` — classifies Responses API
- `translation/chat_completions.rs` — Responses API → Chat Completions body mapping
- Body classifier that detects format from content

**Missing translations**:
- **Vertex Gemini** (`GenerateContent` API — full bidirectional, ~750 lines of Go)
- **Vertex Anthropic** (Anthropic-on-Vertex adjustments — `anthropic_version` injection, field stripping)
- **Azure OpenAI** (path rewrite + `ResponseFieldStripper`)
- **Bedrock OpenAI** (simple path rewrite)
- **OpenAI → Anthropic** direction (praxis only does Anthropic → OpenAI, not reverse)
- **Passthrough detection with path rewriting** logic
- **`ConfigAwareTranslator`** pattern for per-model provider config

**Note**: Praxis's per-filter-pair approach is more composable than IPP's monolithic
translator registry. But the actual translation logic for Vertex/Azure/Bedrock doesn't
exist yet.

---

### 3. `apikey-injection` — MISSING

**IPP**: Watches K8s Secrets via label-filtered informer. Three auth generators:
- **APIKey** — `Authorization: Bearer {key}` (configurable header name/prefix per provider)
- **SigV4** — Full AWS Signature V4 request signing (region auto-detection, session tokens)
- **OAuth2** — GCP service account JSON → OAuth2 access token (with caching + pre-expiry refresh)

Reads credential refs from CycleState (set by `model-provider-resolver`), looks up
Secret data, generates provider-appropriate auth headers.

**praxis-ai**: Has a `credential-injection.yaml` example that uses core `set_header`
builtins, but **no dedicated credential injection filter** with auth strategy awareness.
No SigV4. No OAuth2 token exchange.

**Gap**: The simple Bearer token case could be handled via existing `set_header` or a
thin filter. SigV4 and GCP OAuth2 are the complex parts — SigV4 needs request body
hashing, canonical request construction, and signing; OAuth2 needs token caching with
thread-safe refresh.

---

### 4. `maas-headers-guard` — MISSING

**IPP**: Captures all `x-maas-*` headers into CycleState map, then **strips them from
the request**. Prevents identity headers from leaking to upstream providers. Dead simple
(~40 lines of logic) but security-critical.

**praxis-ai**: No equivalent. Core praxis has `remove_header` and `set_header` but no
MaaS-specific header guard.

**Gap**: Conceptually trivial — a filter that pattern-matches request headers, copies
them to filter metadata, strips from request. Could be a generic "header guard" filter
(configurable prefix, capture-to-metadata + strip). Maybe 50-80 lines of Rust.

---

### 5. `nemo-request-guard` + `nemo-response-guard` — PORTED

**IPP**: Two plugins — request guard (input rails) and response guard (output rails).
Both POST to NeMo `/v1/guardrail/checks`. Support OpenAI chat messages + MCP JSON-RPC
argument extraction. Pass/Modified/Blocked handling.

**praxis-ai**: `ai_guardrails` filter with NeMo provider, configurable `phase`
(request/response). Same NeMo protocol. Pluggable provider pattern allows future
non-NeMo guardrail providers.

**Status**: **PORTED**. Praxis's implementation is arguably better — single filter with
phase config vs two separate plugins, and a provider abstraction layer.

---

### 6. `external-metering` — IN-FLIGHT

**IPP**: Two variants — buffered (`external-metering`) and streaming
(`external-metering-streaming`).
- **Request**: Read MaaS identity, check balance, fail-open/closed
- **Response (buffered)**: Extract `usage` from response body, report CloudEvent
- **Response (streaming)**: SSE chunk accumulation, cross-boundary reassembly, usage
  extraction from final chunk
- **Error events**: Detects 4xx/5xx provider errors, reports `inference.request.error`
  CloudEvents
- Token breakdown: prompt, completion, total, cached_input, cache_creation, reasoning
  tokens + duration

**praxis-ai existing**:
- `token_count` — extracts token usage from 6 provider formats (OpenAI, Anthropic,
  Google, Bedrock, Azure) into filter metadata. Handles both streaming and
  non-streaming. This is the data extraction piece.
- `token_usage_headers` — exposes counts as response headers

**praxis-ai in-flight**: Noy's PR #581 (`feat/external-metering-identity-headers`) —
+468/-0 across 11 files. Adds metering filter with identity header handling. Currently
blocked on review.

**Gap**: Token extraction is **PORTED** (and broader — praxis supports 6 providers vs
IPP's generic extraction). The metering service integration (balance check + usage
reporting) is what #581 addresses. Need to verify #581 covers error event reporting and
the CloudEvent format.

---

### 7. `stream-usage-enforcer` — MISSING

**IPP**: Injects `stream_options: { include_usage: true }` into OpenAI Chat Completion
streaming requests. Only acts on `OpenAIChatCompletions` format when `stream: true`.
Ensures upstream includes a final usage chunk in SSE.

**praxis-ai**: No equivalent. `token_count` parses usage from responses but nothing
injects `stream_options` into requests.

**Gap**: ~30 lines of Rust. Read request body, check if streaming OpenAI chat, inject
`stream_options.include_usage = true`. Trivial but necessary for metering to work with
streaming.

---

## What praxis-ai Has That IPP Doesn't

| praxis-ai | IPP equivalent |
|-----------|---------------|
| A2A protocol (Agent-to-Agent routing, task routing with persistence) | None |
| MCP broker (static catalog, tool aggregation, stateless profile) | None |
| MCP tool resolve + dispatch (resolve MCP tools → function defs, execute calls) | None |
| OpenAI Responses API (full pipeline: format, validate, rehydrate, store, stream events, proxy) | None |
| OpenAI Conversations (full CRUD, local persistence) | None |
| Response Store (SQLite/PostgreSQL persistence for Responses API) | None |
| Anthropic SSE events (OpenAI→Anthropic streaming transformation) | None |
| Doc extract / File resolve (Responses API file handling) | None |
| Model rewrite (alias maps with wildcard support) | None |
| Prompt enrichment (inject system/user messages) | None |
| Web search (model-driven web search tool) | None |

---

## Shared Infrastructure Differences

| Aspect | IPP | praxis-ai |
|--------|-----|-----------|
| **State passing** | `CycleState` (typed KV store) | Filter metadata + internal headers (`x-praxis-ai-*`) |
| **Plugin lifecycle** | K8s controller-runtime | Config-driven YAML, hot-reload via file watcher |
| **Config source** | K8s CRDs (ExternalModel/ExternalProvider) | YAML config files |
| **Credentials** | K8s Secrets via filtered informer | Config-driven (needs design) |
| **Format detection** | Path-suffix matching | Body-content classification (richer) |
| **Translation arch** | Monolithic translator registry | Separate filter per pair (more composable) |

---

## Priority Ranking for Porting

| Priority | Plugin | Effort | Why |
|----------|--------|--------|-----|
| **1** | `stream-usage-enforcer` | **S** (~30 lines) | Needed for metering with streaming. Trivial. |
| **2** | `maas-headers-guard` → generic header guard | **S** (~80 lines) | Security-critical. Simple capture + strip pattern. |
| **3** | `model-provider-resolver` → YAML model routing | **XL** (design + impl) | Linchpin for multi-provider gateway. Needs design for praxis's config-driven world. |
| **4** | `api-translation` gaps (Vertex/Azure/Bedrock) | **L** per provider | ~750 lines each for Vertex Gemini. Azure/Bedrock are simpler path rewrites. |
| **5** | `apikey-injection` → credential injection | **L** | Bearer is easy. SigV4 and OAuth2 are substantial. |
| **6** | `external-metering` gaps | **M** (verify #581) | Most is covered by existing token_count + #581. Verify error events. |

---

## Architectural Note

The fundamental difference: IPP is a **Kubernetes sidecar controller** that watches
CRDs and passes rich state between plugins via `CycleState`. Praxis is a **standalone
proxy** that uses YAML config and passes context via internal headers and filter
metadata.

Porting isn't line-for-line translation — it's **re-expressing K8s-native patterns as
proxy-native patterns**. The model resolver especially needs a clean design for
praxis's world: YAML model routing tables that map model names to upstream clusters with
format/credential/path config, loaded at startup and hot-reloaded on config change.

---

## IPP Plugin Data Flow Reference

```
Request → maas-headers-guard (capture+strip x-maas-* headers)
        → model-provider-resolver (resolve model → provider + write CycleState)
        → stream-usage-enforcer (inject stream_options.include_usage)
        → nemo-request-guard (content safety check)
        → external-metering (balance check)
        → api-translation (translate request body + rewrite path)
        → apikey-injection (inject provider auth headers)
        → [upstream provider]

Response → api-translation (translate response body back)
         → nemo-response-guard (content safety check)
         → external-metering / external-metering-streaming (report usage)
         → [client]
```

## IPP Plugin Source Reference

All plugin source: `~/code/redhat/ai-gateway-payload-processing/pkg/plugins/`

| Plugin | Key files |
|--------|-----------|
| `model-provider-resolver` | `plugin.go`, `store.go`, `external_model_reconciler.go`, `external_provider_reconciler.go` |
| `api-translation` | `plugin.go`, `translator/anthropic/`, `translator/vertex/`, `translator/azure/`, `translator/bedrock/` |
| `apikey-injection` | `plugin.go`, `store.go`, `reconciler.go`, `auth-generator/` |
| `external-metering` | `plugin.go`, `client.go` |
| `maas-headers-guard` | `plugin.go` |
| `nemo` | `request_guard.go`, `response_guard.go`, `nemo_guard_base.go` |
| `stream-usage-enforcer` | `plugin.go` |
| Shared types | `common/apiformat/`, `common/auth/`, `common/provider/`, `common/state/` |
