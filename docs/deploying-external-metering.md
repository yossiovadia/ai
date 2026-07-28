# Deploying the External Metering Filter

A self-contained walkthrough: build this branch, put a metering
service behind it, proxy real Anthropic traffic through it, and watch
the token usage land on the other side.

Everything below runs on one laptop. No Kubernetes, no cluster, no
second repository. The only credential you need is an Anthropic API
key, which is supplied out of band and never written to a file in
this repository.

This page is the only thing you need to read. Every file it asks you
to create is included inline.

---

## 1. What the filter does

`external_metering` sits in the request pipeline and does two things.

**Before the request goes upstream**, it identifies the caller from
tenant headers and asks a metering service whether that caller still
has budget:

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

```text
                     ┌──────────────────────────────┐
                     │      metering service        │
                     │           :9090              │
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
| Python 3.9+ | only for the mock metering service in §4 |
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

## 3. Get the code and build

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

## 4. Run a metering service

Pick one. Option A proves the whole loop in thirty seconds. Option B
gives you a real database and a dashboard.

### Option A — mock service (recommended for a first run)

Save this as `mock-metering.py` in the repository root. It implements
both endpoints and prints everything it receives.

```python
#!/usr/bin/env python3
"""Minimal stand-in for an external metering service."""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("PORT", "9090"))
DENY = os.environ.get("DENY", "") not in ("", "0", "false")


class Handler(BaseHTTPRequestHandler):
    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parts = self.path.split("?", 1)
        segments = parts[0].strip("/").split("/")
        # api/v1/customers/{username}/entitlements/{feature_key}/value
        if len(segments) == 7 and segments[2] == "customers" and segments[6] == "value":
            username, feature_key = segments[3], segments[5]
            query = parts[1] if len(parts) > 1 else ""
            print(
                f"[balance] user={username} feature={feature_key} {query} "
                f"-> hasAccess={not DENY}",
                flush=True,
            )
            self._json(200, {
                "hasAccess": not DENY,
                "balance": 0 if DENY else 1_000_000,
                "usage": 0,
                "overage": 0,
            })
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        if self.path.rstrip("/") == "/api/v1/events":
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                print(f"[event] unparseable body: {raw!r}", flush=True)
                self._json(400, {"error": "invalid json"})
                return
            print("[event] " + json.dumps(event, indent=2, sort_keys=True), flush=True)
            self._json(202, {"status": "accepted"})
            return
        self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        """Silence the default access log."""


if __name__ == "__main__":
    mode = "DENY (balance check fails)" if DENY else "ALLOW"
    print(f"mock metering service on http://127.0.0.1:{PORT} [{mode}]", flush=True)
    try:
        HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
```

Run it in its own terminal and leave it there:

```bash
python3 mock-metering.py
```

```text
mock metering service on http://127.0.0.1:9090 [ALLOW]
```

To exercise the rejection path later, restart it with
`DENY=1 python3 mock-metering.py`.

### Option B — the real metering service

If you have access to the metering service repository, bring it up
with Docker Compose. Its Compose file publishes port 8080, which
collides with the gateway, so remap it to 9090:

```bash
git clone <metering-service-repo> ai-gateway-metering-service
cd ai-gateway-metering-service
docker compose up -d --build postgres
docker compose run -d --service-ports -p 9090:8080 metering-service
cd -
```

Check it:

```bash
curl -sS http://127.0.0.1:9090/health
```

The dashboard is at <http://127.0.0.1:9090/dashboard>.

---

## 5. Configure the gateway

Save this as `anthropic-metering.yaml` in the repository root.

```yaml
# Metered Anthropic Gateway
#
# Terminates plain HTTP on :8080, checks the caller's token balance
# against an external metering service, forwards the request to the
# Anthropic Messages API over TLS with a server-side API key, then
# reports the token usage back to the metering service.

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

Then, from a third terminal:

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

**Check 1 — the balance check fired.** The metering terminal shows:

```text
[balance] user=alice feature=inference-tokens model=claude-sonnet-4-5 -> hasAccess=True
```

**Check 2 — the completion came back.** The curl output is a normal
Anthropic Messages response with a `usage` block.

**Check 3 — the usage event landed.** The metering terminal shows a
CloudEvent shortly after:

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
    "model": "claude-sonnet-4-5",
    "prompt_tokens": 14,
    "completion_tokens": 11,
    "total_tokens": 25,
    "cached_input_tokens": 0,
    "cache_creation_tokens": 0,
    "duration_ms": 812
  }
}
```

The numbers must match the `usage` block in the curl output. If they
are all zero, the filter ordering is wrong — see §1.

**Check 4 — the rejection path works.** Stop the mock service and
restart it in deny mode:

```bash
DENY=1 python3 mock-metering.py
```

Repeat the curl. You should get:

```text
token budget exhausted
```

with HTTP 429, and no request should reach Anthropic.

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
nothing." Set `default_username` while you are testing.

**`fail_open` defaults to `true`.** A gateway pointed at a metering
service that is down will happily serve traffic unmetered. That is
usually what you want in production and never what you want while
verifying the wiring — turn it off when testing.

**The mock service always says yes.** Its balance check is not backed
by any accounting. The real service in Option B ships with a large
fixed quota, so the 429 path will not fire there either without
seeding an entitlement. Use `DENY=1` to exercise rejection.

**Cache token fields need this branch.** `cached_input_tokens` and
`cache_creation_tokens` come from the prompt-cache breakdown in
`token_count`, which is on this branch and not yet on `main`.

**Streaming works.** Anthropic's SSE responses carry usage in the
`message_delta` event; `token_count` parses it incrementally without
buffering the stream. Add `"stream": true` to the request body and
watch the same event arrive at the end.

**Keep the two local files out of git.** `mock-metering.py` and
`anthropic-metering.yaml` are scratch files for your machine. Add
them to `.git/info/exclude` so they are never committed.

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
