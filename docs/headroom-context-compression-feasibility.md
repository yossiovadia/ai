# Headroom Context Compression as a Centralized Praxis Filter

**Status:** Feasibility study — *not* an approved plan to build.
**Date:** 2026-09-02
**Context:** praxis-ai dogfood gateway ("pricetag"), multi-user, routes through Praxis to real providers (Anthropic, self-hosted vLLM/Qwen, …).
**Related:** [`cost-savings-distilled.md`](cost-savings-distilled.md), [`ipp-port-gap-analysis.md`](ipp-port-gap-analysis.md), prior `headroom-gateway` project (Yossi's OpenShift proxy).

---

## 1. The idea

Route **every** authenticated request from **every** dogfood user through a
centralized **headroom** step *before* the real provider, so headroom "shrinks"
the context (compresses old tool outputs) to cut input-token cost — with
headroom's promise of no quality loss.

Two delivery shapes were considered:
- **(A) A Praxis `HttpFilter`** in the praxis-ai proxy that buffers the request,
  calls a headroom ML service, rewrites the body, and forwards.
- **(B) Headroom as a separate upstream proxy** in front of the provider
  (the prior `headroom-gateway` "Option A"), leaving the Rust proxy untouched.

This doc answers: is it *feasible*, and is it *worth it*?

---

## 2. Verdict (TL;DR)

| Question | Answer | Confidence |
|---|---|---|
| **Build-feasible as a Praxis filter?** | **Yes — and cheaper than expected.** "Buffer body → outbound callout → rewrite → forward" is a first-class, actively-used pattern in this repo with 5 precedents. | **Verified** (source read) |
| **Does it actually lower the pricetag?** | **Unknown. Must be measured.** The compressed tokens live in the stable prefix that prompt/prefix caching already serves cheaply; mutating it likely *busts* the cache. | **Unverified prior** (see §5.1) |
| **Net worth it?** | **Conditional.** Likely a win for *long / near-limit* contexts and *no-cache* routes; likely a **loss** for steady-state interactive coding on a cache-heavy provider. | Analysis |

**The build risk is gone. The value risk is the whole game.** Do not skip the
measurement (§7).

---

## 3. What headroom does (recap, from prior `headroom-gateway` work)

- A context-compression library. Compresses **old tool outputs** (file reads,
  build logs, API responses — the ~80% of tokens in a coding session), leaving
  **user messages, system prompts, assistant text, and recent tool outputs**
  protected.
- Session-aware via a "stable after N turns" heuristic (`STABLE_AFTER_TURN=2`)
  that works **purely off the message array in a single request** — so a
  *stateless* per-request filter can do the selection in one pass. No
  cross-request state needed.
- Compression is **deterministic ML** (Kompress / ModernBERT) for the lossy
  paths, plus an instant heuristic "smart_crusher."

> **Prior results** (from the June 2026 `headroom-gateway` learnings, *real*
> numbers — labeled as such, not re-measured here):
> - Synthetic fixtures (RAG docs, 30-pod JSON, log dumps): **50–70%**.
> - **Real Claude Code sessions: ~16% best / 15%** — real sessions are dominated
>   by *recent* (protected) content + system prompt, so realized savings are
>   modest. **Use the ~16% figure for planning, not 50–70%.**

---

## 4. Verified: the build is cheap (source findings)

*Verified by reading the pinned dependency (`praxis-filter 0.5.1` /
`praxis-proxy-filter`) and this repo. All citations below are real.*

### 4.1 The filter abstraction supports everything headroom needs

`HttpFilter` (praxis-proxy-filter `src/filter.rs`) gives:
- **Async body phase:** `async fn on_request_body(ctx, body: &mut Option<Bytes>, end_of_stream)` — can `await` an external HTTP round-trip.
- **Buffer the whole request:** override `request_body_mode() → BodyMode::StreamBuffer { max_bytes: Some(n) }` + `request_body_access() → BodyAccess::ReadWrite`. Buffered modes guarantee the full body arrives in one call with `end_of_stream == true` (also exposed as `ctx.buffered_request_body`).
- **Outbound call to a *different* service (the key question — YES, two ways):**
  - **`ctx.subrequest_client`** — a public field, a framework-injected pooled
    Pingora `SubRequestClient`, populated for **every pipeline** by this server
    (`server/src/pipelines.rs:82`). `apis/src/subrequest.rs:106-118`
    (`execute_url`) parses any `http(s)://host:port/path` and executes — i.e.
    "call a separate service on another cluster by URL."
  - **A filter-built `reqwest::Client`** — the NeMo/MCP precedent; `reqwest`
    is already a workspace dependency.
- **Rewrite + forward:** `body` is mutable under `ReadWrite`; swap bytes in
  place, return `FilterAction::Continue`. On failure: degrade to original body
  (`Continue`) or `Reject`.

> Earlier (pre-dig) assumption that "a filter can only forward to one configured
> cluster and can't call a sidecar" is **false**. The two-hop callout is the
> established pattern.

### 4.2 Five in-repo precedents for "callout then act"

| Filter | Path | What it does | Relevance |
|---|---|---|---|
| **`openai_file_resolve`** | `apis/src/openai/responses/file_resolve/mod.rs` | Buffers body → callout to a Files API/sidecar → **rewrites buffered JSON** → forwards. Has pre-commit length-cap rejection. | **Near drop-in template for headroom.** |
| **`openai_responses_compact`** | `apis/src/openai/responses/compact/` | Callout to an inference endpoint to **summarize/compact context, then rewrite the request.** | **Headroom's closest cousin — already ships.** |
| **`ai_guardrails` (NeMo)** | `filters/src/guardrails/{filter.rs,providers/nemo.rs}` | Buffers body → awaits external verdict → decide. Its body-*rewrite* on `Redact` is **deferred to issue #579** — exactly the capability headroom needs. | Provider/callout shape to copy. |
| **`openai_web_search`** | `apis/src/openai/responses/web_search/provider.rs` | Outbound callout (Brave) via `SubRequestClient`. | Shared-client callout idiom. |
| **`openai_mcp_dispatch` / `mcp_client`** | `apis/src/mcp_client/mod.rs` | Proxy calls external MCP servers over HTTP (own `reqwest`, SSRF checks, pinned client). | Security posture to copy. |

### 4.3 What's genuinely new (small)

1. **Headroom payload/response contract** — copy the NeMo provider shape (bounded response reads).
2. **Body-rewrite-from-external-result** — already solved by `file_resolve`; the *known-open* piece for NeMo (#579), so headroom would be first to land it, but on a proven template.
3. **Security posture for the auxiliary endpoint** — URL allowlist + SSRF checks (helpers in `apis/src/openai/url_security.rs`, `mcp_client`), plus the `allow_pre_security_callout` acknowledgement (see §5.4).

**Registration is one block** in `filters/src/register.rs` — copy the
`register_file_resolve` dual-constructor pattern (`from_config` /
`from_config_with_client`) so it wires under both `build_ai_registry()` and the
server runtime. House failure semantics already exist: `CalloutSettings` +
`FailureMode` (`open`/`closed`) at `apis/src/openai/responses/config_validation.rs:13-34`.

**Not needed:** `ResponseStoreRegistry` (injected as a `PipelineExtension` at
`server/src/pipelines.rs:81`) is irrelevant to headroom. The one pipeline-level
thing to respect is `max_request_bytes` (`apply_body_limits`, `pipelines.rs:70-74`)
for large buffered bodies.

### 4.4 Key files to keep open when designing

- `apis/src/openai/responses/file_resolve/mod.rs` — **the template.**
- `apis/src/openai/responses/compact/` — the context-shrink cousin.
- `filters/src/guardrails/{filter.rs,providers/nemo.rs}` — callout provider shape.
- `apis/src/subrequest.rs` + `apis/src/openai/responses/config_validation.rs` — shared callout machinery.
- `apis/src/json_body.rs` — `serialize_json_body(...).commit(...)` mutation helper.
- `filters/src/register.rs` — wiring.

---

## 5. The open risks

### 5.1 VALUE / CACHING — **the load-bearing one, unmeasured**

The reason headroom "saves cost" is fewer *input* tokens. But the dogfood
routes through **Anthropic** (prompt caching: cached-prefix reads cost **0.1×**
the input price — Anthropic's documented rate) **and self-hosted vLLM**
(automatic prefix caching makes cached prefixes ~free in prefill).

**The catch:** the tokens headroom compresses — the *old* tool outputs — live
exactly in the **stable prefix** that caching is already serving cheaply.
Mutate those bytes and you **bust the prefix cache** from that position to the
end of the request, for that turn.

- Interactive sessions cross the "compress" threshold on **different turns** for
  each tool output → a **series of cache busts** over the session's life, each
  re-prefilling everything downstream of the change point at *full* (uncached)
  cost.
- Steady state has a shorter cached prefix (good), but pays repeated one-time
  re-prefill penalties (bad).
- **Direction is unverified.** My prior: **net-negative on a cache-heavy
  provider** — you can end up *raising* the pricetag with your cost-saver. This
  was **not** measured in the prior `headroom-gateway` work (the learnings don't
  mention caching at all).

> This is an **analysis/prior, not a measurement.** The specific multiplier
> (0.1×) is Anthropic's documented cache-read rate; the *net* direction on this
> specific workload is a guess until we measure billed tokens with vs. without.

### 5.2 LATENCY

- Headroom's ML path is **~3s on CPU, <100ms on GPU**. As a filter in the
  *critical path of every user's every turn*, 3s CPU per request is a
  non-starter without GPU.
- **Cheap mitigation (established pattern):** every callout filter size-gates
  before calling out. "Only hit headroom when body > N bytes" spares small
  requests the round-trip.
- Buffering the full request body also fights the repo's "don't buffer in the
  fast proxy" ethos — but it's *accepted* for callout filters (`StreamBuffer`
  is the sanctioned mode), so it's a known trade, not a violation.

### 5.3 QUALITY BLAST RADIUS

- "Zero quality loss" was a prior A/B claim on one workload. Compression is
  lossy by nature.
- In a **centralized** filter, a silent degradation (a compressed stack trace /
  file-read / test-output) hits **every dogfood user at once** and reads as
  "the gateway got dumber." The prior project excluded Read/Write/Edit *by
  default* for a reason — you're making that safety call for everyone.
- Mitigation: **off by default**, opt-in per route; keep the Read/Write/Edit
  exclusion; shadow-log before/after to catch regressions.

### 5.4 PIPELINE ORDERING — "after auth" has a wrinkle

- `file_resolve`'s module doc: *"Praxis runs `StreamBuffer` body hooks
  before header-phase request filters."* Hence the explicit
  `allow_pre_security_callout: true` acknowledgement.
- Implication: a headroom **body-phase** callout fires **before header-phase
  filters**. If auth is header-phase, headroom would call out with
  **unauthenticated** request bodies — at odds with the "after authentication"
  intent.
- **Open question to resolve:** where does auth sit in the pipeline relative to
  body hooks? If auth must precede the callout, headroom may need a header-phase
  gate + deferred body callout, or a different pipeline position. (Not a
  blocker — a design constraint to check.)

### 5.5 SECURITY POSTURE

- Pointing at an internal headroom service needs a **URL allowlist + SSRF
  checks** (copy `mcp_client`/`url_security.rs`) and the
  `allow_pre_security_callout` acknowledgement.
- The reserved-header stripping (`x-praxis-*`, `x-ext-*`) on subrequests is
  *desirable* here — you won't leak internal routing headers to headroom.

---

## 6. The value question, in detail

### 6.1 Where headroom actually helps (vs. hurts)

| Scenario | Caching | Headroom net |
|---|---|---|
| Interactive coding, cache-heavy provider (Anthropic / vLLM+APC) | Cache hits are the whole cost model | **Likely negative** (busts to save little) |
| Context near length / cache-window limit | Cache already busting | **Likely positive** (shrinking buys headroom) |
| No-cache / cheap-cache route | No cache to bust | **Positive** (raw token savings) |
| High-volume API, large stable-ish payloads | Varies | **Positive** (if deterministic, see 6.2) |

Interactive Claude Code — the actual dogfood workload — is the *worst* case for
value *and* the case where latency hurts most.

### 6.2 Headroom vs. the existing `compact` filter (the real delta)

`openai_responses_compact` *already* shrinks context via an LLM callout. So the
question isn't "can we shrink context" — it's "what does headroom add?"

| | `compact` (exists) | headroom (proposed) |
|---|---|---|
| Compressor | LLM inference callout | Deterministic ML (Kompress/BERT) |
| Cost of compression | LLM tokens + latency | ML inference (no LLM tokens); 3s CPU / <100ms GPU |
| Fidelity | Lossy **summary** | Structure-preserving (tool output → compressed repr) |
| **Cache behavior** | **Non-deterministic (LLM) → busts every turn** | **Deterministic *if* ML inference is byte-stable → cache *stable* after one-time flip** |

> **The differentiator, if true:** a *deterministic* headroom busts the cache
> **once** per tool output (at its flip) then stays cache-stable, whereas
> `compact` busts it **every turn.** That would make headroom strictly better
> for *long-but-cached* contexts — **but only if the ML path is
> byte-deterministic, which must be verified.** If Kompress inference is
> non-deterministic, headroom collapses to `compact`-equivalent cache behavior
> and loses its main advantage. **Verify determinism before building.**

---

## 7. Recommendation

**Measure first. Then build scoped. Do not ship an always-on global filter.**

The build is a well-trodden few-hundred-line filter on proven rails (§4). The
*only* thing between you and it is the **measurement**, because enabling
headroom on a cache-heavy route is a *gamble* on the pricetag direction (§5.1).

When it is built, scope it defensively:
- **Off by default**, opt-in per route/model (gate via the existing
  classify→route→branch header mechanism).
- **Size-gated** (only call out above a body threshold) — the latency fix.
- **GPU-backed** (or accept 3s CPU only on gated, non-interactive routes).
- **Idempotent + byte-deterministic** compression (marker so we never
  double-compress; determinism so the cache stays stable after the flip).
- **`FailureMode::open` default** (forward *uncompressed* on headroom failure —
  inverts `compact`'s `closed` default; for a cost feature, degrade to original
  rather than reject).
- Keep the **Read/Write/Edit exclusion**; shadow-log before/after for quality.

---

## 8. Suggested next steps (phased)

**Phase 0 — the load-bearing measurement (do this first).**
Replay a handful of *real* captured dogfood requests, with caching on, and
compare **billed tokens + cache-hit rate**:
1. as-is (baseline), vs
2. with headroom compressing.
- If compressed **wins on billed tokens** (not raw) → it's real; proceed.
- If it **loses** (the bet) → you've saved building a latency-adding quality
  risk that makes the cost product cost *more*.
- **Also test Kompress determinism** (same input → same bytes across turns) in
  this phase; it decides the §6.2 differentiator.

**Phase 1 — a gated spike (only if Phase 0 is positive).**
Build the filter on the `file_resolve`/`compact` templates; enable on **one
route** (ideally a no-cache or long-context route), off-by-default, with a size
gate and `open` failure mode. Instrument it to report per-request: tokens
before/after, cache-hit delta, callout latency.

**Phase 2 — gated rollout.**
Expand route-by-route where the Phase-1 telemetry shows net billed-token
savings. Keep the central dashboard showing *billed* (not raw) savings so the
pricetag is the source of truth.

---

## 9. Open questions

1. **Cache net direction** on this workload — Phase 0 answers it. (§5.1)
2. **Kompress determinism** — byte-stable across identical inputs? (§6.2)
3. **Auth vs. body-hook ordering** — does the callout fire before auth, and do
   we care / how to gate? (§5.4)
4. **GPU availability** for the dogfood cluster — determines latency viability
   on interactive routes. (§5.2)
5. **Headroom vs. `compact` positioning** — is headroom a *replacement* for
   `compact` on some routes, or a complementary second lever? (§6.2)

---

## Appendix — what's verified vs. assumed

**Verified (source read this session):**
- Filter capabilities: async body phase, `StreamBuffer`+`ReadWrite`,
  `ctx.subrequest_client` (always wired), `execute_url` full-URL callout.
- Five in-repo callout precedents incl. `compact` and `file_resolve`.
- Registration mechanism, `CalloutSettings`/`FailureMode`, `json_body` helper.
- `allow_pre_security_callout` / body-hooks-before-header-filters behavior.
- NeMo `Redact` body-rewrite deferred to #579.

**Assumed / from prior work / unmeasured:**
- Real-session compression ≈ 16% (prior `headroom-gateway` data, not re-run).
- Anthropic 0.1× cache-read rate (documented, not measured on this workload).
- Caching net direction = likely negative (prior, **must measure**).
- 3s-CPU / <100ms-GPU ML latency (prior data; re-confirm on target box).
- Headroom quality parity (prior A/B claim, single workload).
