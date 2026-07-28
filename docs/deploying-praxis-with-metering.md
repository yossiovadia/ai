# Deploying Praxis AI with the Metering Service

A complete walkthrough of a two-component deployment: the Praxis AI
gateway in front, an external metering service behind it, and real
Anthropic traffic flowing through both.

By the end you will have a gateway that authorizes every inference
request against a token budget, forwards it to Anthropic with a
server-side API key the client never sees, and records what the call
cost in a database you can query.

Everything runs on one machine. The only credential you need is an
Anthropic API key, supplied out of band and never written to a file
in this repository.

---

## 1. What you are deploying

Two processes.

**The metering service** owns the accounting. It answers "does this
caller still have budget?" and it records what each call consumed. It
keeps its state in PostgreSQL and ships a dashboard.

**Praxis AI** is the gateway. Its `external_metering` filter is the
link between the two: it asks the metering service for permission on
the way in, and reports usage on the way out.

```text
                     ┌──────────────────────────────┐
                     │      metering service        │──── PostgreSQL
                     │           :9090              │      :5432
                     └───▲──────────────────────┬───┘
       balance check     │                      │  202 Accepted
       usage CloudEvent  │                      ▼
                     ┌───┴──────────────────────────┐
   client ──HTTP──►  │        praxis-ai :8080       │  ──TLS──►  api.anthropic.com:443
                     │  router                      │
                     │  external_metering           │
                     │  token_count                 │
                     │  credential_injection        │
                     │  headers                     │
                     │  load_balancer               │
                     └──────────────────────────────┘
```

### How the filter talks to the metering service

**Before the request goes upstream**, the filter identifies the
caller from tenant headers and asks:

```text
GET {metering_url}/api/v1/customers/{username}/entitlements/{feature_key}/value?model={model}
```

A response of `{"hasAccess": true}` admits the request. `false`
rejects it with `429 token budget exhausted`.

**After the response comes back**, it reads the token counts that the
`token_count` filter extracted from the provider's usage block and
posts a CloudEvent:

```text
POST {metering_url}/api/v1/events
```

The report is fire-and-forget: it never adds latency to the response
and never fails the request.

### Ordering matters

Response hooks run in **reverse declaration order**. `external_metering`
must be declared *before* `token_count` so that on the way back out
`token_count` runs first, writes `token.input` / `token.output` /
`token.total` / `token.cache_read` / `token.cache_write` into the
filter metadata, and `external_metering` then reads them.

Declare them the other way around and every usage event reports zero.

---

## 2. Prerequisites

| Requirement | Notes |
|---|---|
| Rust stable 1.96+ | `rustup toolchain install stable` |
| Rust nightly | formatting only — `rustup toolchain install nightly` |
| CMake 3.31+ | needed to build the underlying proxy engine |
| Docker with Compose | runs the metering service and PostgreSQL |
| Access to the metering service repository | ask whoever sent you this page |
| An Anthropic API key | supplied separately, never committed |

On macOS:

```bash
brew install cmake
rustup toolchain install stable nightly
```

On Fedora/RHEL:

```bash
sudo dnf install -y cmake gcc gcc-c++ perl openssl-devel
rustup toolchain install stable nightly
```

You do **not** need to clone the Praxis core repository. This
workspace pulls the core crates from crates.io.

---

## 3. Deploy the metering service

Clone it alongside this repository:

```bash
git clone <metering-service-repo> ai-gateway-metering-service
cd ai-gateway-metering-service
```

Its Compose file publishes port 8080, which collides with the
gateway. Change that one line in `docker-compose.yaml`:

```yaml
  metering-service:
    build: .
    ports:
      - "9090:8080"      # was "8080:8080"
```

Bring both containers up:

```bash
docker compose up -d --build
```

The service creates its own schema on first start — there is no
separate migration step. Confirm it is healthy:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9090/health
```

Expect `200`. Watch its logs in a spare terminal; you will want them
during verification:

```bash
docker compose logs -f metering-service
```

The dashboard is at <http://127.0.0.1:9090/dashboard>.

---

## 4. Build the gateway

```bash
git clone https://github.com/noyitz/ai.git praxis-ai
cd praxis-ai
git checkout feat/external-metering-full

cargo build --release
```

The first build compiles the proxy engine from source and takes a
while — fifteen to twenty-five minutes is normal on a laptop.
Subsequent builds are seconds.

Confirm the filter registered:

```bash
cargo test -p praxis-ai-filters metering
```

Expect 38 passing tests.

---

## 5. Configure the gateway

Save this as `anthropic-metering.yaml` in the `praxis-ai` repository
root.

```yaml
# Metered Anthropic Gateway
#
# Terminates plain HTTP on :8080, checks the caller's token balance
# against the metering service, forwards the request to the Anthropic
# Messages API over TLS with a server-side API key, then reports the
# token usage back to the metering service.

listeners:
  - name: gateway
    address: "127.0.0.1:8080"
    filter_chains:
      - main

filter_chains:
  - name: main
    filters:
      # 1. Route everything to the Anthropic cluster.
      - filter: router
        routes:
          - path_prefix: "/"
            cluster: anthropic

      # 2. Balance check on the request path; usage report on the
      #    response path. Declared BEFORE token_count on purpose:
      #    response hooks run in reverse declaration order, so
      #    token_count populates the token metadata first and
      #    external_metering reads it afterwards.
      - filter: external_metering
        metering_url: "http://127.0.0.1:9090"
        timeout_seconds: 5
        feature_key: "inference-tokens"
        source: "praxis-ai"
        fail_open: true
        identity_header_prefix: "x-tenant-"
        default_username: "anonymous"
        default_model: "unknown"

      # 3. Parse Anthropic usage blocks into token metadata,
      #    including the prompt-cache breakdown.
      - filter: token_count
        provider: anthropic

      # 4. Inject the provider key and strip whatever the client sent.
      - filter: credential_injection
        clusters:
          - name: anthropic
            header: x-api-key
            env_var: ANTHROPIC_API_KEY
            strip_client_credential: true

      # 5. Anthropic validates Host against the SNI name and requires
      #    an explicit API version header.
      - filter: headers
        request_set:
          - name: "Host"
            value: "api.anthropic.com"
          - name: "anthropic-version"
            value: "2023-06-01"

      # 6. TLS to the upstream.
      - filter: load_balancer
        clusters:
          - name: anthropic
            tls:
              sni: "api.anthropic.com"
            endpoints:
              - "api.anthropic.com:443"
```

Then export your key:

```bash
export ANTHROPIC_API_KEY='sk-ant-...'   # sent to you separately
```

The key is read from the environment at startup by the
`credential_injection` filter and injected as the `x-api-key` header
on the way upstream. It never appears in the config file, and any
`x-api-key` a client sends is stripped before injection.

If you would rather point at a different upstream, the only fields to
change are the `endpoints`, the `tls.sni` name, and the `Host` header.

---

## 6. Run it

```bash
cargo run --release -p praxis-ai-proxy -- -c anthropic-metering.yaml
```

Then, from another terminal:

```bash
curl -sS http://127.0.0.1:8080/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-tenant-username: alice" \
  -H "x-tenant-group: engineering" \
  -d '{
        "model": "claude-sonnet-4-5",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "Say hello in five words."}]
      }'
```

---

## 7. Verify

**Check 1 — the completion came back.** The curl output is a normal
Anthropic Messages response with a `usage` block. Note the numbers.

**Check 2 — the metering service recorded the call.** Its log shows:

```text
level=INFO msg="event recorded" user=alice model=claude-sonnet-4-5 tokens=25
```

**Check 3 — the numbers are right.** Query the recorded event back
out:

```bash
curl -sS 'http://127.0.0.1:9090/api/v1/dashboard/recent?limit=1'
```

```json
[
  {
    "timestamp": "2026-01-01T00:00:00Z",
    "username": "alice",
    "group_name": "engineering",
    "model": "claude-sonnet-4-5",
    "prompt_tokens": 14,
    "completion_tokens": 11,
    "total_tokens": 25,
    "cached_input_tokens": 0,
    "cache_creation_tokens": 0,
    "cost_usd": 0.000207
  }
]
```

`prompt_tokens` and `completion_tokens` must match the `usage` block
from Check 1. If they are zero, the filter ordering is wrong — see §1.

The same row appears on the dashboard at
<http://127.0.0.1:9090/dashboard>.

**Check 4 — the gateway refuses to serve unmetered traffic.** This is
the one worth proving, because the default is to fail *open*. Set
`fail_open: false` in `anthropic-metering.yaml`, restart the gateway,
then stop the metering service:

```bash
docker compose -f ../ai-gateway-metering-service/docker-compose.yaml stop metering-service
```

Repeat the curl. You should get:

```text
metering system unavailable
```

with HTTP 503, and no request should reach Anthropic. Bring the
service back up and the same curl succeeds again.

---

## 8. Configuration reference

```yaml
- filter: external_metering
  metering_url: "http://127.0.0.1:9090"
  timeout_seconds: 5
  feature_key: "inference-tokens"
  source: "praxis-ai"
  fail_open: true
  identity_header_prefix: "x-tenant-"
  default_username: "anonymous"
  default_model: "unknown"
```

| Field | Required | Default | Meaning |
|---|---|---|---|
| `metering_url` | yes | — | Base URL of the metering service. Must not be empty. |
| `timeout_seconds` | no | `5` | HTTP timeout for both calls. Must be greater than zero. |
| `feature_key` | no | `inference-tokens` | Entitlement key in the balance-check path. |
| `source` | no | `ai-gateway` | CloudEvents `source` field. |
| `fail_open` | no | `true` | `true`: admit when metering is unreachable. `false`: reject with 503. |
| `identity_header_prefix` | no | `x-tenant-` | Prefix for the identity headers below. |
| `default_username` | no | unset | Fallback caller name. **If unset, unidentified requests are not metered at all.** |
| `default_model` | no | unset | Fallback model name. |

Unknown fields are rejected at startup rather than ignored.

### Identity headers

With the default prefix the filter reads, and then strips before
forwarding upstream:

| Header | Purpose |
|---|---|
| `x-tenant-username` | Who is being billed. Drives the balance check and the event `subject`. |
| `x-tenant-group` | Group/team attribution. |
| `x-tenant-subscription` | Subscription identifier. |
| `x-tenant-model` | Overrides the model name if the body does not carry one. |

In a real deployment these are injected by an authentication layer in
front of the gateway, not by the client.

### The usage event

```json
{
  "specversion": "1.0",
  "id": "...",
  "source": "praxis-ai",
  "type": "inference.tokens.used",
  "subject": "alice",
  "time": "2026-01-01T00:00:00+00:00",
  "datacontenttype": "application/json",
  "data": {
    "user": "alice",
    "group": "engineering",
    "subscription": null,
    "provider": "anthropic",
    "model": "claude-sonnet-4-5",
    "prompt_tokens": 14,
    "completion_tokens": 11,
    "total_tokens": 25,
    "cached_input_tokens": 0,
    "cache_creation_tokens": 0,
    "duration_ms": 812,
    "user_agent": "curl/8.4.0"
  }
}
```

### Responses the filter can produce

| Status | Body | When |
|---|---|---|
| `429` | `token budget exhausted` | Metering answered `hasAccess: false`. |
| `503` | `metering system unavailable` | Metering unreachable or unparseable **and** `fail_open: false`. |

With `fail_open: true` (the default) the second case admits the
request instead.

---

## 9. Gotchas

**No identity header and no `default_username` means silence.** The
request succeeds, the response is normal, and nothing is metered. This
is the single most common reason someone reports "the filter does
nothing." Set `default_username` while you are bringing things up.

**`fail_open` defaults to `true`.** A gateway pointed at a metering
service that is down will happily serve traffic unmetered. That is
usually what you want in production and never what you want while
verifying the wiring.

**The 429 path will not fire out of the box.** The metering service
currently applies a very large fixed quota, so `hasAccess` is
effectively always true. Exercising real budget exhaustion means
seeding a small entitlement first; §7's Check 4 tests the 503 path
instead, which needs no seeding.

**The balance check is per-user, not per-model, unless you send
`x-tenant-model`.** The check runs on request headers, before the
body is readable, so the `?model=` parameter can only be filled from
the identity header. The model in the *usage event* is resolved later
from the request body and will be correct either way. Note also that
`default_model` is applied only when the event is emitted — it does
not backfill the balance-check URL, which stays empty. If you intend
to scope entitlements per model, have your authentication layer set
`x-tenant-model`.

**Cache token fields need this branch.** `cached_input_tokens` and
`cache_creation_tokens` come from the prompt-cache breakdown in
`token_count`, which is on this branch and not yet on `main`.

**Streaming works.** Anthropic's SSE responses carry usage in the
`message_delta` event; `token_count` parses it incrementally without
buffering the stream. Add `"stream": true` to the request body and
watch the same event arrive at the end.

**Keep your local config out of git.** `anthropic-metering.yaml` is a
scratch file for your machine. Add it to `.git/info/exclude` so it is
never committed.

---

## 10. Where things live

| Path | What |
|---|---|
| `filters/src/metering/mod.rs` | Filter implementation. |
| `filters/src/metering/config.rs` | YAML config type and validation. |
| `filters/src/metering/tests.rs` | Unit tests. |
| `examples/configs/external-metering.yaml` | Minimal example against a plain HTTP backend. |
| `docs/filters/external_metering.md` | Generated filter reference. |
| `tests/integration/tests/suite/examples/external_metering.rs` | Integration test for the example config. |
