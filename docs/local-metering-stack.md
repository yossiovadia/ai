# Local Metering Stack with JWT Auth

Reference for recreating the full local development stack:
Keycloak → Praxis (jwt_auth + identity_header_guard + external_metering + token_count) → llm-katan → metering dashboard.

## Prerequisites

- Rust 1.96+, CMake 3.31+, Docker
- This repo on branch `feat/metering-local`
- Metering service repo at `../ai-gateway-metering-service`

## 1. Start metering service

```bash
cd ../ai-gateway-metering-service
# Change port to 9090 in docker-compose.yaml (8080 conflicts with praxis)
docker compose up -d --build
# May need: docker compose restart metering-service (postgres race)
curl http://127.0.0.1:9090/health  # expect 200
```

Dashboard: http://127.0.0.1:9090/dashboard

## 2. Start Keycloak

```bash
docker run -d --name keycloak \
  -p 8280:8080 \
  -e KC_BOOTSTRAP_ADMIN_USERNAME=admin \
  -e KC_BOOTSTRAP_ADMIN_PASSWORD=admin \
  quay.io/keycloak/keycloak:26.2.4 start-dev
```

Wait for readiness: `curl http://127.0.0.1:8280/health/ready`

## 3. Configure Keycloak

Admin console: http://127.0.0.1:8280 (admin/admin)

Create realm `ai-gateway`, client `praxis-gateway` (public, direct access grants), group `ai-eng`, and users with firstName/lastName/email set (Keycloak 26+ requires complete profiles).

Add a groups protocol mapper to the client to include group membership in JWT claims.

JWKS endpoint: `http://127.0.0.1:8280/realms/ai-gateway/protocol/openid-connect/certs`

## 4. Build and run Praxis

```bash
cargo build -p praxis-ai-proxy
export LLM_KATAN_API_KEY="llm-katan-openai-key"
# Or for real Anthropic: export ANTHROPIC_API_KEY="sk-ant-..."
./target/debug/praxis-ai -c dev-metering-llmkatan.yaml
```

## 5. Get a token and send a request

```bash
KC=http://127.0.0.1:8280
JWT=$(curl -s "$KC/realms/ai-gateway/protocol/openid-connect/token" \
  -d "client_id=praxis-gateway" \
  -d "username=yossi" \
  -d "password=test123" \
  -d "grant_type=password" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

curl http://127.0.0.1:8180/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $JWT" \
  -d '{"model": "llm-katan-echo", "messages": [{"role": "user", "content": "Hello!"}]}'
```

## 6. Swap to real Anthropic

In `dev-metering-llmkatan.yaml`, change:
- `env_var: LLM_KATAN_API_KEY` → `env_var: ANTHROPIC_API_KEY`
- `header: Authorization` + `header_prefix: "Bearer "` → `header: x-api-key` (no prefix)
- endpoints: `api.anthropic.com:443`, sni: `api.anthropic.com`
- `token_count` provider: `anthropic`
- Add `anthropic-version` header via `headers` filter

## Components on this branch

| Filter | File | What |
|--------|------|------|
| `jwt_auth` | `filters/src/jwt_auth/` | JWKS-based JWT validation, claim→header injection |
| `identity_header_guard` | `filters/src/identity_guard/` | Capture + strip identity headers |
| `external_metering` | `filters/src/metering/` | Balance check + usage CloudEvent reporting |
| `MeteringClient` | `filters/src/metering/mod.rs` | Local reqwest wrapper (replaces unreleased CalloutClient) |

## Ports

| Service | Port |
|---------|------|
| Praxis | 8180 |
| Keycloak | 8280 |
| Metering service | 9090 |
| PostgreSQL (metering) | 5432 |
| llm-katan (remote) | 3-147-232-199.sslip.io:443 |

## Test users (Keycloak)

All passwords: `test123`, all in group `ai-eng`
- yossi, noyitz, ahmad.ali, emma.wilson
