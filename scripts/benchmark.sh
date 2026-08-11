#!/usr/bin/env bash
# Multi-user load test through the dogfood gateway.
#
# Sends realistic multi-turn conversations through the full
# auth + metering pipeline, backed by llm-katan (zero cost).
#
# Usage:
#   ./scripts/benchmark.sh                          # 20 users, 10 turns each
#   ./scripts/benchmark.sh --users 50 --turns 5     # 50 users, 5 turns
#   ./scripts/benchmark.sh --cleanup                # delete test data only
#
# Prerequisites:
#   - oc login to the dogfood cluster
#   - benchmark listener deployed (port 8082)
#   - llm-katan deployed on the cluster

set -euo pipefail

NAMESPACE="ai-gateway-dogfood"
NUM_USERS=20
NUM_TURNS=10
CONCURRENT=10
CLEANUP_ONLY=false
USER_PREFIX="bench-user"
USER_DOMAIN="test"

show_help() {
    cat <<'HELP'
Usage: ./scripts/benchmark.sh [OPTIONS]

Multi-user load test through the Praxis AI gateway pipeline.
Creates test users, fires concurrent multi-turn conversations via
llm-katan (echo backend, zero provider cost), and reports latency,
throughput, resource usage, and proxy overhead.

Options:
  --users N        Number of test users to create (default: 20)
  --turns N        Conversation turns per user (default: 10)
  --concurrent N   Max parallel requests (default: 10)
  --cleanup        Delete test users and metering data only (no benchmark)
  --help           Show this help

Examples:
  ./scripts/benchmark.sh                              # 20 users, 10 turns
  ./scripts/benchmark.sh --users 50 --turns 5         # 50 users, 5 turns
  ./scripts/benchmark.sh --concurrent 30              # higher parallelism
  ./scripts/benchmark.sh --users 5 --turns 2          # quick smoke test
  ./scripts/benchmark.sh --cleanup                    # wipe test data

Pipeline under test:
  api_key_auth → identity_header_guard → router → external_metering
  → token_count → token_usage_headers → headers → load_balancer

Output includes:
  - Latency percentiles (p50/p95/p99)
  - Throughput (req/s)
  - Error breakdown by HTTP status
  - Pod CPU/memory before and after
  - Praxis proxy overhead (total latency minus backend TTFT)

Prerequisites:
  - oc login to the cluster
  - Benchmark listener deployed (port 8082)
  - llm-katan deployed on the cluster
HELP
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --users) NUM_USERS="$2"; shift 2 ;;
        --turns) NUM_TURNS="$2"; shift 2 ;;
        --concurrent) CONCURRENT="$2"; shift 2 ;;
        --cleanup) CLEANUP_ONLY=true; shift ;;
        --help|-h) show_help ;;
        *) echo "Unknown option: $1"; show_help ;;
    esac
done

# ── Preflight ────────────────────────────────────────────────

if ! oc whoami > /dev/null 2>&1; then
    echo "ERROR: not logged in to OpenShift"
    exit 1
fi

BENCHMARK_ROUTE=$(oc -n "$NAMESPACE" get route ai-gateway-benchmark -o jsonpath='{.spec.host}' 2>/dev/null)
if [[ -z "$BENCHMARK_ROUTE" ]]; then
    echo "ERROR: benchmark route not found. Deploy benchmark listener first."
    exit 1
fi
BENCHMARK_URL="https://$BENCHMARK_ROUTE"

TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

# ── Cleanup mode ─────────────────────────────────────────────

cleanup() {
    echo "Cleaning up benchmark data..."

    # Revoke all bench-user keys
    oc -n "$NAMESPACE" port-forward svc/maas-api 18080:8080 > /dev/null 2>&1 &
    PF_PID=$!
    sleep 2

    for i in $(seq -w 1 99); do
        USERNAME="${USER_PREFIX}-${i}@${USER_DOMAIN}"
        RESPONSE=$(curl -s -X POST "http://localhost:18080/v1/api-keys/search" \
            -H "Content-Type: application/json" \
            -H "X-MaaS-Username: $USERNAME" \
            -H 'X-MaaS-Group: ["benchmark"]' \
            -d '{}')
        IDS=$(echo "$RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for k in data.get('data', []):
    if k['status'] == 'active':
        print(k['id'])
" 2>/dev/null)
        while read -r ID; do
            [[ -z "$ID" ]] && continue
            curl -s -X DELETE "http://localhost:18080/v1/api-keys/$ID" \
                -H "Content-Type: application/json" \
                -H "X-MaaS-Username: $USERNAME" \
                -H 'X-MaaS-Group: ["benchmark"]' > /dev/null
        done <<< "$IDS"
    done
    kill $PF_PID 2>/dev/null

    # Delete metering events for benchmark users
    echo "Deleting metering events for benchmark users..."
    oc -n "$NAMESPACE" exec postgresql-0 -- psql -U aigateway -d aigateway -q \
        -c "DELETE FROM events WHERE user_id LIKE '${USER_PREFIX}-%@${USER_DOMAIN}';" 2>/dev/null || true

    echo "Cleanup complete."
}

if $CLEANUP_ONLY; then
    cleanup
    exit 0
fi

# ── Create test users ────────────────────────────────────────

echo "=========================================="
echo "  Multi-User Benchmark"
echo "=========================================="
echo "  Users:      $NUM_USERS"
echo "  Turns/user: $NUM_TURNS"
echo "  Concurrent: $CONCURRENT"
echo "  Endpoint:   $BENCHMARK_URL"
echo ""

echo "Creating $NUM_USERS test users..."
oc -n "$NAMESPACE" port-forward svc/maas-api 18080:8080 > /dev/null 2>&1 &
PF_PID=$!
sleep 2

for i in $(seq -w 1 "$NUM_USERS"); do
    USERNAME="${USER_PREFIX}-${i}@${USER_DOMAIN}"
    RESPONSE=$(curl -s -X POST "http://localhost:18080/v1/api-keys" \
        -H "Content-Type: application/json" \
        -H "X-MaaS-Username: $USERNAME" \
        -H 'X-MaaS-Group: ["benchmark"]' \
        -d "{\"name\":\"bench-${i}\",\"description\":\"Benchmark user ${i}\"}")
    KEY=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('key',''))" 2>/dev/null)
    if [[ -n "$KEY" ]]; then
        echo "$USERNAME|$KEY" >> "$TMPDIR/users.txt"
    else
        echo "  WARN: failed to create key for $USERNAME"
    fi
done
kill $PF_PID 2>/dev/null

CREATED=$(wc -l < "$TMPDIR/users.txt" | tr -d ' ')
echo "  Created $CREATED keys"
echo ""

# ── Build payload template ───────────────────────────────────

SYSTEM_PROMPT="You are an expert software engineer working on a high-performance Rust proxy called Praxis. The codebase implements HTTP filters for AI inference workloads including request classification, model routing, token counting, credential injection, and protocol translation between OpenAI and Anthropic APIs. You help with code reviews, architecture decisions, debugging, and implementation of new filters. The proxy is built on top of Pingora and uses an async pipeline architecture with filter_metadata for inter-filter communication. Key patterns include BodyMode::Stream for SSE processing, set_metadata for writing to the filter context, and request_headers_to_remove for header stripping."

build_messages() {
    local turn=$1
    local msgs="["
    for t in $(seq 1 "$turn"); do
        if (( t > 1 )); then msgs+=","; fi
        if (( t % 2 == 1 )); then
            msgs+="{\"role\":\"user\",\"content\":\"Turn $t: Can you explain how the filter pipeline handles streaming SSE responses? I need to understand the data flow from the upstream provider through each filter back to the client. Specifically how does BodyMode::Stream interact with the token_count filter to extract usage from the final SSE chunk without buffering the entire response? Also how does filter_metadata propagate between filters in the response path given that response hooks execute in reverse order? Please include code examples from the actual codebase.\"}"
        else
            msgs+="{\"role\":\"assistant\",\"content\":\"Turn $t response: The streaming pipeline works by processing each SSE chunk as it arrives. The token_count filter operates in BodyMode::Stream mode and watches for the final chunk containing usage data. In the Anthropic format this is the message_delta event with usage.output_tokens. In OpenAI format it appears in the final chunk with usage object. The filter extracts these values and writes them to filter_metadata as token.input token.output and token.total. Since response hooks run in reverse pipeline order the external_metering filter which is declared before token_count in the config actually runs after it in the response path. This means by the time metering reads filter_metadata the token counts are already populated. The key insight is that filter_metadata is shared mutable state scoped to the request lifetime so any filter can read values written by any other filter regardless of pipeline position.\"}"
        fi
    done
    msgs+="]"
    echo "$msgs"
}

# ── Run benchmark ────────────────────────────────────────────

echo "Capturing baseline resource usage..."
oc -n "$NAMESPACE" adm top pods 2>/dev/null | tee "$TMPDIR/resources_before.txt" || true

# Capture Praxis metrics before
oc -n "$NAMESPACE" port-forward svc/praxis 19901:9901 > /dev/null 2>&1 &
METRICS_PF=$!
sleep 1
curl -s http://localhost:19901/metrics > "$TMPDIR/metrics_before.txt" 2>/dev/null
kill $METRICS_PF 2>/dev/null

echo ""
echo "Running benchmark..."
echo ""

run_user_session() {
    local username="$1"
    local apikey="$2"
    local user_latencies="$TMPDIR/latency_${username}.txt"

    for turn in $(seq 1 "$NUM_TURNS"); do
        local messages
        messages=$(build_messages "$turn")
        local payload="{\"model\":\"claude-sonnet-4\",\"max_tokens\":512,\"stream\":false,\"system\":\"$SYSTEM_PROMPT\",\"messages\":$messages}"

        local start_ms
        start_ms=$(python3 -c "import time; print(int(time.time()*1000))")

        local http_code
        http_code=$(curl -sk -o /dev/null -w "%{http_code}" \
            -X POST "$BENCHMARK_URL/v1/messages" \
            -H "Content-Type: application/json" \
            -H "x-api-key: $apikey" \
            -H "anthropic-version: 2023-06-01" \
            --max-time 30 \
            -d "$payload" 2>/dev/null)

        local end_ms
        end_ms=$(python3 -c "import time; print(int(time.time()*1000))")
        local latency=$(( end_ms - start_ms ))

        echo "$latency|$http_code" >> "$user_latencies"
    done
}

export -f run_user_session build_messages
export BENCHMARK_URL NUM_TURNS TMPDIR SYSTEM_PROMPT

START_TIME=$(python3 -c "import time; print(int(time.time()*1000))")

# Run users in parallel
cat "$TMPDIR/users.txt" | xargs -P "$CONCURRENT" -I {} bash -c '
    IFS="|" read -r username apikey <<< "{}"
    run_user_session "$username" "$apikey"
'

END_TIME=$(python3 -c "import time; print(int(time.time()*1000))")
DURATION_MS=$(( END_TIME - START_TIME ))

# ── Collect results ──────────────────────────────────────────

echo ""
echo "Capturing post-benchmark resource usage..."
oc -n "$NAMESPACE" adm top pods 2>/dev/null | tee "$TMPDIR/resources_after.txt" || true

# Capture Praxis metrics after
oc -n "$NAMESPACE" port-forward svc/praxis 19901:9901 > /dev/null 2>&1 &
METRICS_PF=$!
sleep 1
curl -s http://localhost:19901/metrics > "$TMPDIR/metrics_after.txt" 2>/dev/null
kill $METRICS_PF 2>/dev/null

echo ""
echo "Collecting results..."

python3 -c "
import os, sys

tmpdir = '$TMPDIR'
num_users = $NUM_USERS
num_turns = $NUM_TURNS
duration_ms = $DURATION_MS

latencies = []
successes = 0
failures = 0
errors_by_code = {}

for f in os.listdir(tmpdir):
    if not f.startswith('latency_'):
        continue
    with open(os.path.join(tmpdir, f)) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            parts = line.split('|')
            lat = int(parts[0])
            code = parts[1] if len(parts) > 1 else '000'
            latencies.append(lat)
            if code == '200':
                successes += 1
            else:
                failures += 1
                errors_by_code[code] = errors_by_code.get(code, 0) + 1

total = len(latencies)
if total == 0:
    print('No results collected!')
    sys.exit(1)

latencies.sort()
p50 = latencies[int(total * 0.50)]
p95 = latencies[int(total * 0.95)]
p99 = latencies[int(total * 0.99)]
avg = sum(latencies) // total
duration_s = duration_ms / 1000.0
rps = total / duration_s if duration_s > 0 else 0

print()
print('==========================================')
print('  Benchmark Results')
print('==========================================')
print()
print('  Config:')
print('    Users:       {}'.format(num_users))
print('    Turns/user:  {}'.format(num_turns))
print('    Total:       {} requests ({} succeeded, {} failed)'.format(total, successes, failures))
print('    Duration:    {:.1f}s'.format(duration_s))
print()
print('  Latency (ms):')
print('    avg:   {}'.format(avg))
print('    p50:   {}'.format(p50))
print('    p95:   {}'.format(p95))
print('    p99:   {}'.format(p99))
print('    min:   {}'.format(latencies[0]))
print('    max:   {}'.format(latencies[-1]))
print()
print('  Throughput:  {:.1f} req/s'.format(rps))
if failures > 0:
    print()
    print('  Errors:')
    for code, count in sorted(errors_by_code.items()):
        print('    HTTP {}: {}'.format(code, count))
print()
print('==========================================')
"

# ── Resource usage ───────────────────────────────────────────

echo "  Resource Usage (during benchmark):"
echo ""
if [[ -f "$TMPDIR/resources_after.txt" ]]; then
    while read -r line; do
        echo "    $line"
    done < "$TMPDIR/resources_after.txt"
fi
echo ""

# ── Praxis proxy overhead ────────────────────────────────────

python3 -c "
import os

before = '$TMPDIR/metrics_before.txt'
after = '$TMPDIR/metrics_after.txt'

def parse_counter(path, name):
    total = 0
    if not os.path.exists(path):
        return 0
    with open(path) as f:
        for line in f:
            if line.startswith(name + '{') or line.startswith(name + ' '):
                parts = line.rsplit(' ', 1)
                if len(parts) == 2:
                    try:
                        total += float(parts[1])
                    except ValueError:
                        pass
    return total

def parse_sum(path, metric):
    if not os.path.exists(path):
        return 0
    with open(path) as f:
        for line in f:
            if line.startswith(metric):
                parts = line.rsplit(' ', 1)
                if len(parts) == 2:
                    try:
                        return float(parts[1])
                    except ValueError:
                        pass
    return 0

req_before = parse_counter(before, 'praxis_http_requests_total')
req_after = parse_counter(after, 'praxis_http_requests_total')
dur_before = parse_sum(before, 'praxis_http_request_duration_seconds_sum')
dur_after = parse_sum(after, 'praxis_http_request_duration_seconds_sum')

bench_reqs = req_after - req_before
bench_dur = dur_after - dur_before

if bench_reqs > 0:
    avg_total = (bench_dur / bench_reqs) * 1000
    # llm-katan TTFT is ~800ms, so proxy overhead ≈ total - 800
    proxy_overhead = max(0, avg_total - 800)
    print('  Praxis Metrics (from Prometheus):')
    print('    Requests processed:  {:.0f}'.format(bench_reqs))
    print('    Avg total latency:   {:.0f}ms (includes 800ms llm-katan TTFT)'.format(avg_total))
    print('    Proxy overhead:      ~{:.0f}ms (total - TTFT)'.format(proxy_overhead))
    print()
else:
    print('  Praxis metrics: no delta detected')
    print()
" 2>/dev/null

echo "  Dashboard: check metering for per-user breakdown"
echo "  Cleanup:   ./scripts/benchmark.sh --cleanup"
echo ""
echo "=========================================="
