# Session Handoff: OpenShift Dogfood Deployment

## Current State (August 11, 2026)

### What's built and working locally
- Branch: `feat/metering-local` on `yossiovadia/ai` fork (pushed)
- Full pipeline: `api_key_auth` OR `jwt_auth` → `identity_header_guard` → `external_metering` → `token_count` → `credential_injection` → Anthropic
- Tested end-to-end with Claude Code through Praxis → real Anthropic API → metering dashboard
- 940+ tests pass, `make lint` clean
- Security reviewed twice (by Fable model through the gateway itself), all findings fixed

### Filters on this branch
| Filter | Lines | Tests | Purpose |
|--------|-------|-------|---------|
| `api_key_auth` | ~250 | 8 | Validates sk-oai-* keys via HTTP callout to maas-api |
| `jwt_auth` | ~500 | 10+4 | Validates JWTs via JWKS (Keycloak). Inactive in dogfood config but preserved. |
| `identity_header_guard` | ~180 | 11+3 | Captures x-tenant-* headers to metadata, strips before upstream |
| `external_metering` (modified) | ~30 lines changed | 38 | Added metadata-first identity reading, spoofing protection |
| `MeteringClient` | ~70 | - | Local reqwest wrapper replacing unreleased CalloutClient |

### OpenShift manifests
All at `deploy/openshift/`:
- `namespace.yaml` — `ai-gateway-dogfood`
- `postgresql.yaml` — StatefulSet + PVC + Secret + Service
- `metering-service.yaml` — Deployment + Service
- `praxis.yaml` — Deployment + ConfigMap (pipeline config) + Secret (Anthropic key) + Service
- `routes.yaml` — TLS-terminated Routes for gateway + dashboard
- `deploy.sh` — One-command deploy with preflight checks

### What's NOT on the branch yet
- maas-api OpenShift manifest (needs RBAC for CRD watches)
- Container images not yet pushed (CRC had disk pressure, pivoting to IBM Cloud)

## IBM Cloud OpenShift Cluster
- **Status**: Provisioning (started ~1:35 AM UTC Aug 11)
- **Config**: VPC, us-south-1, bxf.4x16 (4 vCPU/16GB), 2 workers
- **Settings**: Calico networking, public+private endpoints, no KMS/encryption/monitoring add-ons

## Deployment Plan
File: `docs/plans/2026-08-10-openshift-dogfood-deployment.md`

### Steps remaining
1. Wait for IBM Cloud cluster to be ready (~30 min)
2. `ibmcloud oc cluster config --cluster <name>` to get kubeconfig
3. Build container images using `oc new-build --binary` (same approach that worked on CRC for metering-service)
4. Deploy PostgreSQL, metering service, Praxis via manifests
5. maas-api deployment (TBD — may start without it, use simpler key service)
6. Create Routes, test with `connect-claude.sh --openshift`

### Image builds (use OpenShift binary builds — proven on CRC)
```bash
# Metering service (worked on CRC, ~55s):
cd ai-gateway-metering-service
oc -n ai-gateway-dogfood new-build --binary --name=metering-service --strategy=docker
oc -n ai-gateway-dogfood start-build metering-service --from-dir=. --follow

# Praxis AI (~15-20 min Rust compilation):
cd praxis-ai
oc -n ai-gateway-dogfood new-build --binary --name=praxis-ai --strategy=docker
oc -n ai-gateway-dogfood start-build praxis-ai --from-dir=. --follow
```

### Update manifests for imagestreams
After building, images are at `image-registry.openshift-image-registry.svc:5000/ai-gateway-dogfood/<name>:latest`
Update deployment image refs to match.

## Auth Strategy
- **Dogfood**: API keys via maas-api (`api_key_auth` filter)
- **Future SSO**: Keycloak JWT (`jwt_auth` filter, code preserved on branch)
- Both write same `filter_metadata` contract — downstream filters unchanged

## Key Decisions Made
1. No Keycloak for dogfood — API keys via maas-api instead (simpler UX, no token expiry)
2. No Kuadrant/Authorino/Limitador — Praxis validates keys directly
3. Single Anthropic API key shared by all users (per Josh decision TBD)
4. `identity_header_guard` PR ready for upstream (#698 issue created, waiting assignment)
5. CPEX vs lean filters debate ongoing (discussion #15 on praxis-extproc)
6. Posted prototype update on discussion #15 — waiting for team response

## GitHub Activity
- Issue #698 (praxis-proxy/ai): identity_header_guard filter — created, comment volunteering
- Issue #600 (praxis-proxy/ai): llm-katan multi-provider test backend proposal
- PR #601 (praxis-proxy/ai): Anthropic SDK integration test with llm-katan
- Discussion #15 (opendatahub-io/praxis-extproc): lean identity filters proposal — posted prototype update

## Scripts
- `scripts/start-gateway.sh` — starts Praxis with preflight checks
- `scripts/connect-claude.sh` — authenticates + launches Claude Code through gateway
- `deploy/openshift/deploy.sh` — one-command OpenShift deploy

## Local Services (for reference)
- Keycloak: port 8280 (HTTPS, self-signed cert, container `keycloak`)
- Metering service: port 9090 (docker compose in `../ai-gateway-metering-service`)
- Praxis: port 8180
- Keycloak users: yossi, noyitz, ahmad.ali, emma.wilson (password: test123, group: ai-eng)
- Keycloak token lifetime: 24h (should be extended or switched to API keys)
