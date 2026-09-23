# Praxis upgrade Phase 0 audit — port surface (2026-09-16)

Goal: move the dogfood gateway to latest upstream (`praxis-proxy/ai` main, core 0.5.5)
with zero prod downtime, via port branch → shadow stack → route-weight canary.

## Headline

The branch divergence (778 behind / 659 ahead) is largely **an illusion**: upstream
rebase-merges PRs and rewrites SHAs, so most "our" commits are stale copies of
upstream work. Verified: `#598`, `#339`, `#634`, `#612` all exist on `origin/main`
under different SHAs (b78ca2d5, 1efffcbb, 061136cd, d93be2cc).

The **true port surface is 6 filters** (~21 files) + one surgical re-land + deploy
scripts. Everything else upstream already has, or has done better.

## Bin A — ours and needed (re-land onto upstream main, prod uses them today)

Per `deploy/openshift/praxis.yaml` all four routes run `api_key_auth` +
`model_access` + `external_metering` + `token_count`; unified adds
`stream_usage_inject`, anthropic adds `model_catalog`, vLLM path uses
`content_normalize`.

| Feature | Files (filters/src/) | Upstream equivalent | Notes |
|---|---|---|---|
| `api_key_auth` | 3 | **none** (they have `identity_guard` = header guard only) | Validates sk-* keys against maas-api; core to dogfood |
| `model_access` | 3 | **none** | per-group model ACL |
| `model_catalog` | 3 | **none** | GET /v1/models discovery |
| `stream_usage_inject` | `token_usage/stream_usage*` | **none** (their token_usage counts, doesn't inject) | THE hard one: lands on top of a file tree upstream rewrote (`count.rs`, `streaming.rs`, `providers.rs`) |
| `content_normalize` | 2 | **none** | vLLM compat |
| deploy scripts | `deploy/openshift/*` (11 files, 1.5k lines) | none (upstream has no openshift deploy) | keep as-is; parameterize `release.sh` constants for shadow |

`apis/src/openai/responses/*` config additions (4 small files) — inspect at
re-land time; some may be obsoleted by upstream's Responses rework.

## Bin B — superseded upstream (drop; upstream did it)

| Ours | Upstream | Evidence |
|---|---|---|
| `cache_write_tokens` extraction (68196cae) | #858 (efa98d8e) | upstream `count.rs` handles `cache_write` throughout (META_CACHE_WRITE, merge, accumulate) |
| non-streaming Responses API usage parse (a7d578fb) | token_usage Responses support | upstream `token_usage/{count,providers}.rs` cover responses |
| SSE scratch limit 1 MiB (fed9c8db) | configurable `max_scratch_bytes` (default 64 KiB) | becomes a `praxis.yaml` config line, not code |
| `identity_header_guard` (55c4851f) | `identity_guard` #709 | verify config parity, then adopt theirs |
| intelligent_route, compact #598, SubRequestClient #634, filter-registration API #612, … | upstream (history-rewritten) | we already hold copies; zero port work — they arrive with the rebase |

## Bin C — ours, not in the prod image (don't block the upgrade)

- `jwt_auth` (Keycloak/OIDC, 5 files): in the tree, **not referenced by prod
  praxis.yaml**. Decide separately whether to keep porting it; don't gate cutover on it.
- `sideeye/` (31 commits), dashboards, qwen-flash-proxy, docs: tooling/aux —
  stays on the old branch or moves out-of-band; not part of the shipped image.

## Sequencing for the careful path

1. **Port branch** `upgrade/2026-09` off `origin/main`. Land Bin A features one at
   a time (clean cherry-picks + test runs per feature); `stream_usage_inject` last
   (needs manual re-integration with rewritten `token_usage`).
2. **Config migration**: run upstream binary against prod `praxis.yaml`; fix
   renames/breaking changes from 778 commits. Add `max_scratch_bytes` equivalent
   config where the 1 MiB bump is needed.
3. **Shadow stack**: `praxis-shadow` deploy + service + `*-shadow` routes,
   `praxis-ai-shadow` imagestream — never touch `praxis-ai:latest`. Metering:
   disable `external_metering` on shadow OR point a 2nd metering-service at a
   scratch CNPG db (`aigateway_shadow`) so shadow traffic never lands in
   production `usage_events`.
4. **Prove**: upstream conformance suite; llm-katan-backed replay (zero provider
   spend); Noy + Yos daily-driver the shadow routes; before/after repros of every
   Bin A fix on both stacks.
5. **Canary**: OpenShift route `alternateBackends` weights 10 → 50 → 100.
   Rollback = weight back to 0, seconds, no pod churn. Keep old `praxis`
   deploy up-but-unexposed a week, then retire and re-point `release.sh`.

## Do-not list

- Never rebuild the `praxis-ai` imagestream `:latest` from the port branch while
  prod runs on it — shadow gets its own imagestream tag.
- Don't mirror live traffic to shadow through real providers (double spend,
  double metering side-effects). Replay via llm-katan instead.
- Don't rebase the 659-commit branch onto upstream — re-land Bin A onto clean main.

## Phase 0b open questions (before touching code)

1. `api_key_auth`/`model_access`: upstream grew auth/routing subsystems
   (`identity_guard`, `routing/overlay`, callout/ext-auth machinery). Is there now
   a first-class seam to express these instead of custom filters? (Architecture-first:
   porting as-is works; upstreaming might stick.)
2. Does upstream's `token_count`+`metering` fully replace our `token_count` usage
   once `stream_usage_inject` is re-landed, or do we keep both?
3. `jwt_auth`: keep alive or retire?
