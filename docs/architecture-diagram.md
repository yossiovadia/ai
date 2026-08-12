# Dogfood Architecture

## Deployment Diagram

```
                          ┌─────────────────────────────────────────────────┐
                          │           OpenShift Cluster (IBM Cloud)         │
                          │           namespace: ai-gateway-dogfood         │
                          │                                                 │
  ┌──────────┐            │  ┌─────────────────────────────────────────┐    │
  │          │  Route:8080│  │           Praxis Proxy                  │    │
  │  Claude  ├────────────┼──┤  :8080 Anthropic listener               │    │
  │  Code    │  (TLS edge)│  │    api_key_auth ──► maas-api            │    │
  │          │            │  │    identity_header_guard                 │    │
  └──────────┘            │  │    router ──► external_metering ────┐   │    │
                          │  │    token_count                      │   │    │
  ┌──────────┐            │  │    credential_injection             │   │    │
  │          │  Route:8081│  │    headers (Host: api.anthropic.com) │   │    │
  │  Codex   ├────────────┼──┤  :8081 OpenAI listener              │   │    │
  │  CLI     │  (TLS edge)│  │    (same pipeline, Bearer auth)     │   │    │
  │          │            │  │                                     │   │    │
  └──────────┘            │  │  :8082 Benchmark listener ──────┐   │   │    │
                          │  │    (same pipeline → llm-katan)  │   │   │    │
  ┌──────────┐            │  │                                 │   │   │    │
  │  curl /  │  Route:8082│  │  :9901 Admin (health, metrics)  │   │   │    │
  │  bench   ├────────────┼──┤                                 │   │   │    │
  │          │  (TLS edge)│  └────────┬────────────┬───────────┘   │   │    │
  └──────────┘            │           │            │               │   │    │
                          │           │            │               │   │    │
                          │           ▼            ▼               ▼   │    │
                          │  ┌────────────┐ ┌──────────┐ ┌─────────────┤    │
                          │  │  maas-api  │ │llm-katan │ │  metering   │    │
                          │  │  :8080     │ │  :8000   │ │  service    │    │
                          │  │            │ │          │ │  :8080      │    │
                          │  │ API key    │ │ echo     │ │             │    │
                          │  │ create     │ │ backend  │ │ usage       │    │
                          │  │ validate   │ │ TTFT:    │ │ dashboard   │    │
                          │  │ revoke     │ │ 800ms    │ │ CloudEvents │    │
                          │  │ search     │ │ ITL:15ms │ │ model       │    │
                          │  │            │ │          │ │ pricing     │    │
                          │  └─────┬──────┘ └──────────┘ └──────┬──────┘    │
                          │        │                            │           │
                          │        ▼                            ▼           │
                          │  ┌──────────────────────────────────────┐       │
                          │  │           PostgreSQL                 │       │
                          │  │           :5432                      │       │
                          │  │                                      │       │
                          │  │  api_keys table    usage_events table │       │
                          │  │  model_pricing table                 │       │
                          │  │  10Gi PVC                            │       │
                          │  └──────────────────────────────────────┘       │
                          │                                                 │
                          │  ┌──────────┐  ┌──────────────────────┐        │
                          │  │  status  │  │  nginx + ConfigMap   │        │
                          │  │  page    │  │  project status      │        │
                          │  │  :8080   │  │  & operational gaps  │        │
                          │  └──────────┘  └──────────────────────┘        │
                          │                                                 │
                          └─────────────────────────────────────────────────┘
                                        │                    │
                                        ▼                    ▼
                               ┌──────────────┐    ┌──────────────┐
                               │ api.anthropic │    │ api.openai   │
                               │ .com:443      │    │ .com:443     │
                               │               │    │              │
                               │ Real provider │    │ Real provider│
                               └──────────────┘    └──────────────┘
```

## Request Flow (Claude Code)

```
User ──► ANTHROPIC_BASE_URL ──► OpenShift Route (TLS edge)
                                       │
                                       ▼
                              Praxis :8080 (Anthropic listener)
                                       │
                    ┌──────────────────┼──────────────────┐
                    ▼                  ▼                  ▼
              api_key_auth      identity_guard     external_metering
              validate key      capture headers    record usage event
              via maas-api      to metadata,       to metering service
              HTTP callout      strip before       (async, fire-and-forget)
                    │           upstream                  │
                    ▼                                     ▼
              credential_injection              metering service
              swap sk-oai-* key                 writes to PostgreSQL
              for real Anthropic key            dashboard reads from DB
                    │
                    ▼
              token_count
              extract usage from
              response body/SSE
                    │
                    ▼
              api.anthropic.com:443
              (TLS, real provider)
```

## Components Summary

| Pod | Image | Purpose | Port | Resources |
|-----|-------|---------|------|-----------|
| praxis | praxis-ai:latest | AI proxy gateway | 8080, 8081, 8082, 9901 | 9m CPU, 76Mi |
| maas-api | maas-api:latest | API key management | 8080 | 2m CPU, 17Mi |
| metering-service | metering-service:latest | Usage dashboard | 8080 | 7m CPU, 14Mi |
| postgresql | postgres:16-alpine | Shared database | 5432 | 4m CPU, 47Mi |
| llm-katan | llm-katan:latest | Echo backend (benchmark) | 8000 | 15m CPU, 36Mi |
| status-page | nginx:alpine | Project status page | 8080 | <1m CPU, 6Mi |

## Routes

| Route | Target | Purpose |
|-------|--------|---------|
| ai-gateway-anthropic | praxis:8080 | Claude Code / Anthropic SDK |
| ai-gateway-openai | praxis:8081 | Codex / OpenAI SDK |
| ai-gateway-benchmark | praxis:8082 | Load testing via llm-katan |
| dashboard | metering-service:8080 | Usage dashboard |
| status | status-page:8080 | Project status & gaps |
