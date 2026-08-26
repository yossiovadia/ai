# Model Benchmark Results — Seed Data

Supporting data for the Side-Eye / model-tiering work
(`judge-sampling-pitch.md`, `cost-savings-distilled.md`). Methodology and
first committed result row. All future runs should follow the same protocol
so rows stay comparable.

## Protocol

- **Single-shot, no tools.** Each model gets one identical prompt and one
  attempt. Claude Code sessions are instructed "no tools, no files, answer
  with code only" so the agentic harness confers no advantage over raw API
  calls.
- **Objective scoring first.** Output is compiled and run against a fixed
  test suite (including `go vet` and `-race`) before any qualitative
  judgment. LLM-as-judge review is a later, secondary layer.
- **Record:** wall-clock, tokens (where visible), pass/fail per test,
  idiom-level observations.

## Task 1: concurrent-safe LRU cache (Go)

Chosen because it is the reproduction task from
vllm-project/semantic-router#1439 — known model-discriminating history
(qwen2.5:0.5b failed it with a platitude; it also contains a subtle
correctness trap: naive `RLock()` in `Get` is a data race because Get
mutates recency order).

**Prompt (identical for all arms):**

> Write a Go package that implements a concurrent-safe LRU cache with O(1)
> get/put operations using a doubly-linked list and a sync.RWMutex. Include
> proper eviction logic. Output only the complete Go code for a single file
> lru.go with package lru, no explanations.

### Environments

| Arm | Model | Runtime |
|---|---|---|
| A | Claude Opus 4.6 (1M) | Claude Code, single-shot, no tools |
| C | Qwen3.8-27B, Q4_K_M quant (`qwen3.8-tuned`) | Ollama on MacBook M4 Pro, 48GB, 100% GPU, `num_ctx` 32768, speculative decoding on |

### Results (2026-08-16)

| Metric | Opus 4.6 (arm A) | Qwen3.8-27B local (arm C) |
|---|---|---|
| `go vet` | clean | clean |
| TestBasicPutGet / TestEviction / TestUpdateExisting / TestCapacityOne / TestConcurrent (`-race`) | 5/5 pass | 5/5 pass |
| `RLock`-in-`Get` trap | avoided (full `Lock`) | avoided (full `Lock`) |
| Wall-clock | **11 s** | **272 s** (2,420 tokens incl. thinking @ 8.9 tok/s) |
| API style | Generics (`New[K comparable, V any]`), zero-value returns, capacity panic guard, bonus `Len`/`Remove` | `interface{}` keys/values (pre-generics idiom), sentinel-node list |
| Marginal cost | cents (API) | ~electricity |

Local speed measurements (M4 Pro, Q4_K_M): prefill **35 tok/s**, decode
**5.9–8.9 tok/s**, 100% GPU, no memory pressure. Both are far below the
hardware's theoretical ~16 tok/s decode ceiling — attributed to immature
llama.cpp Metal kernels for the week-old hybrid Gated-DeltaNet architecture
and possibly the built-in speculative decoding. Re-measure after llama.cpp
updates. Prefill at 35 tok/s makes long-context interactive use impractical
on this hardware today (~14 min to ingest a 30K-token session).

### Observations

1. **Correctness parity on this task class** — both models cleared the
   concurrency trap first-shot. No frontier-quality gap appeared on a
   scoped, well-specified task.
2. **The gap is polish, not correctness** (generics, richer API, guard
   clauses) — exactly the severity a cheap review-and-revise pass fixes,
   predicting arm D ≈ arm B here.
3. **The 25× wall-clock gap is hardware/kernels, not the model** — the
   argument for a hosted vLLM tier rather than per-laptop inference.
4. **Caveat:** n=1 task, and one both models were expected to pass.
   Discriminating power comes from harder rungs (multi-file feature work,
   debugging, long-context) where drafts start failing.

## Task 2: neon space-shooter, single-file HTML (2026-08-16)

The demo task from vllm-project/semantic-router#1459 — historically
discriminating (Qwen3.5-35B failed it outright: welcome screen only).
Identical prompt both arms; Claude Code arm run with the no-tools preamble.
Scored by 14 static checks (`/tmp/score_space.py` protocol: structure,
fence discipline, canvas/rAF/WebAudio, no external resources, feature
keywords, node `--check` on every script block) plus human playtest.

| Metric | Opus 4.6 (arm A) | Qwen3.8-27B local (arm C) |
|---|---|---|
| Static checks | 14/14 | 14/14 |
| Size | 808 lines / 30KB | 685 lines / 24KB |
| Wall-clock | 2m 13s | 23m (11,830 tokens @ 8.5 tok/s) |
| Playtest | Playable; **1 major bug**: permanent wave softlock; 1 cosmetic (title crop) | Playable; no blocking defects found; reviewer: "very impressive" |

**Opus softlock root cause** (found in playtest, confirmed in code):
zigzag enemies travel a fixed direction with no off-screen policy (no cull,
no wrap, no homing), while bullets self-cull 50px past the screen edge —
an escaped zigzag is alive and unkillable; wave-advance requires the manual
`enemiesRemaining` counter to reach 0 → unwinnable state reachable through
normal play.

**Why Qwen avoided it** — three independent anti-stall mechanisms:
explicit off-screen cull (`if(e.y>H+40||e.x<-60||e.x>W+60) e.dead=true`),
circular enemies forced to dive after 5.5s, and wave advance checked
against the live array (`enemies.length===0`) instead of parallel counter
bookkeeping. The cheap model shipped the more defensive design; the
frontier model shipped the more ambitious one with a state-desync bug.

**Takeaways:** (1) frontier and local-27B are in the same competence band
on scoped coding tasks — routing by measured task class, not brand, is the
correct economics; (2) the judge layer applies to *every* tier — the major
bug in this row came from the expensive model; (3) static checks alone
scored both 14/14 — gameplay-logic defects need playtest or code review,
supporting review (arm B/D) and session-level judging over static-only
scoring.

**Honest methodology note (discovery vs. verification):** the Opus softlock
was *discovered by human playtest*, not by the LLM reviewer — the reviewer
root-caused it quickly once given the symptom, but might well have missed
it reading cold. Design consequence for Side-Eye: a judge grading artifacts
alone is weakest on emergent runtime behavior; judge rubrics must lean on
execution evidence wherever it exists (agentic session transcripts already
contain test runs and tool output — one more reason session-level judging
beats artifact-only judging), and artifact routes should attach objective
execution checks to the rubric.

**Runtime comparison (measured 2026-08-16, mlx-dspark 0.12.1, same
M4 Pro 48GB):** `mlx-dspark benchmark --model mlx-community/Qwen3.8-27B-8bit
--trials 3` — plain MLX 8-bit baseline 7.9 tok/s; **dspark drafter cap=auto
19.8 tok/s mean (2.51×): chat 14.9 / code 20.1 / math 24.3**. Independently
reproduces the community report (2.45× mean claimed) within a few percent.
Net vs our Ollama Q4 numbers: ~2.5× the decode speed at *higher* (8-bit)
quality — LRU task ≈ 2 min instead of 4.5, space shooter ≈ 10 min instead
of 23. Speculation is lossless (target verifies every drafted token).
Serves OpenAI + Anthropic Messages APIs (can back Claude Code, or register
as a Praxis upstream), with prefix caching for multi-turn agents. Still
unmeasured here: cold long-context prefill (README's own figure: ~62s for
8K cold, 0.2–1s on cached turns) — decode sped up 2.5×, prefill did not.

### Not yet run

- Arm B (Opus + Fable review → revise), arm D (Qwen + Fable review →
  revise) — the money comparison.
- Harder task rungs; Q6_K quant on 48GB hardware; base `qwen3.8:27b`
  vs `-tuned` speed check; hosted FP8 via vLLM.

## Field evidence 1: jwt_auth PR review (2026-08-18)

Uncontrolled, in-the-wild instance of arm B's shape (cheap model drafts,
Fable reviews). Recorded as an anecdote, not a benchmark row — the reviewer
had full repo and tool access and the generator ran agentically, so it does
not follow the single-shot protocol above.

- **Setup:** the `jwt_auth` filter PR (praxis-proxy/ai #769, ~1,600 added
  lines: JWKS fetching/caching, JWT validation, unit + integration tests,
  docs) was drafted in an Opus 4.6 agentic session and passed its local
  gates (clippy, fmt, 11 unit + 4 integration tests green). Fable 5 was
  then asked for an adversarial architectural + correctness review.
- **Result:** Fable confirmed the crypto core was sound (algorithm-confusion
  defenses, exp/nbf/aud semantics, fail-closed posture — verified against
  the vendored `jsonwebtoken` source, not its docs) and found one CRITICAL
  merge blocker: `danger_accept_invalid_certs(true)` hardcoded on the JWKS
  fetch — the trust anchor of the entire filter — enabling token forgery
  via MITM of the proxy→IdP path. Plus two MEDIUMs (config/docs claim
  claims become request headers when they only reach filter_metadata;
  thundering-herd JWKS refresh with no single-flight) and a core test gap
  (no forged-signature test — the filter's central security property was
  untested).
- **Generator's verdict on the review:** "Fable found a real one — the
  CRITICAL TLS finding is a legit merge blocker, and it's dead right that
  shipping an auth filter with cert validation off would be a bad look
  upstream. The rest of the review is sound too. Let me act on it."
- **Why it matters for Side-Eye:** the blocker sat in the diff the whole
  time — the generator had even flagged it in its own code comment ("in
  production... this should be removed") — and it survived every static
  gate and the generator's self-review. One input-heavy review-tier pass
  caught what clippy, fmt, and a green test suite structurally could not.
  This is the arm-B mechanism working on production-bound code rather than
  a toy task, and it reinforces the Task 2 takeaway: the judge layer earns
  its keep on defects that objective gates score as passing.
- **Second cycle — reviewing the revision (same day):** Opus pushed a fix
  commit addressing all findings; a second Fable review pass of that
  commit found two leftovers. One was the *original* finding surviving in
  a file the fix missed (the "claims become request headers" doc lie,
  still present in the examples README table). The other was a new defect
  *introduced by the fix itself*: the loopback allowlist matched `"::1"`,
  but `url::Url::host_str()` returns IPv6 hosts bracketed (`"[::1]"` —
  confirmed against the url crate's own unit tests), making the arm dead
  code and producing spurious MITM warnings. Both were fixed and verified
  in a third commit; a spot-check pass on that commit found nothing.
- **Design consequence:** review-revise is not one-shot — convergence here
  took two full judge cycles (find 8 → fix → find 2 → fix → clean).
  Revisions deserve judge sampling at least as much as first drafts: a fix
  commit both *carries* residue of the original defect (incomplete
  application across files) and *creates* new defects of its own. Side-Eye
  Phase 2's synchronous review-gate mode should assume iteration, and
  Phase 1 sampling should not exempt "fix" traffic as already-reviewed.

## Field evidence 2: hindsight spinner fix, pre-publication gate (2026-08-18)

Second uncontrolled arm-B data point, this time with the review gate placed
*before* publication. An Opus 4.8 session drafted a fix for
`vectorize-io/hindsight` (CLI emitted ANSI spinner frames to non-TTY
output): correct root-cause, correct minimal fix, full suite green,
before/after byte-count proof, and a clean review packet — then stopped at
the agreed review gate instead of opening the PR. The Fable judge pass
confirmed the production code but found a blocker in the *new tests*: two
tests called the real `ansi_enabled()` and assumed `cargo test` always runs
non-TTY. libtest captures `print!` at the macro level but never redirects
fd 1, so the suite passes piped (CI, agent harnesses) and **fails in an
interactive terminal** — reproduced both ways under a PTY. Green for the
author, red on the maintainer's machine: a first-impression killer that no
gate the author ran could see, because the author's own harness is what
made the tests pass. Generator confirmed: "The Fable review is correct on
all counts — the test-injection blocker is real."

Takeaways: (1) the gate-before-publish placement converts a public
embarrassment into a private fix — strongest argument yet for Phase 2's
synchronous review-gate mode on PR-bound routes; (2) the decisive finding
again required *execution* (running the tests under a PTY), not artifact
reading — see the deployment-tiers consequence in `judge-sampling-pitch.md`;
(3) the defect class was "the author's environment lies to them" — a class
structurally invisible to the generator regardless of model quality.

## Field evidence 3: metering cost double-charge + the "out of scope" self-regression (2026-08-21)

Third uncontrolled arm-B data point, and the sharpest one yet on *what the
generator cannot review about itself*. The defect lived in numbers on a
billing dashboard, where no objective gate applies at all — there were no
tests on the SQL cost math, and every figure looked plausible.

- **Setup:** an Opus agentic session had written and deployed the metering
  dashboard's cost queries. Anthropic usage fields are disjoint, but the
  `token_count` filter sums input + cache-read + cache-creation into one
  `prompt_tokens`, and the cost SQL subtracted only cache-*read* from the
  "uncached" term. Net effect: cache-*creation* tokens were billed at the
  base input rate **and** the cache-write rate. All CI green (the bug is in
  SQL arithmetic no test exercised); the dashboard rendered confident dollar
  figures.
- **Trigger:** Fable reviewed a *screenshot's math*, not code — reverse-engineered
  a cold (cache-miss) turn as 234.8K × $22.50/M ≈ $5.29 where the correct
  cache-write rate gives ≈ $2.94, i.e. a ~1.8× overstatement, and noted warm
  (cache-read) rows validated exactly — which is precisely why the bug hid:
  only cold rows were wrong. Generator verified against the code and confirmed
  the double-charge, DRY'd the formula out of six duplicated queries into one
  const, added a reference cost model + cold/warm tests, and fixed it.
- **The standout — reviewing the revision:** the fix PR shipped with an
  "out of scope" note deferring the `cache_write` fallback (defaulted to 0).
  Fable caught that the note was **hiding a regression the fix itself
  introduced**: because the corrected formula now subtracts cache-creation
  from the uncached term, a 0 cache-write fallback turns the over-bill into a
  *silent $0 under-bill* for any unpriced model — "on a billing dashboard,
  showing $0 for real spend is the worse lie," and it was created in the exact
  line the PR was editing. One-token fix (fallback → 18.75, 1.25× the input
  fallback). Generator confirmed and shipped it.
- **Why it matters for Side-Eye:**
  1. **No gate models dollar-correctness.** The primary defect was in
     numbers rendered to users; clippy/tests/fmt have nothing to say about
     whether a cost is *right*. Adversarial review of the *output* (a
     screenshot) is the only thing that caught it.
  2. **The reviewer caught the author's own rationalization.** The "out of
     scope" deferral was the generator talking itself out of a fix that was
     actually a self-inflicted regression. A model cannot reliably review its
     own excuse for *not* doing something — this is structurally invisible to
     the generator regardless of model quality, and it is the cleanest case
     so far of the judge adding what self-review cannot.
  3. **Revisions need judging (again).** As in Field evidence 1, the new
     defect was introduced *by the fix*. Three-for-three now: every revision
     cycle reviewed has surfaced either surviving-original or fix-introduced
     defects. Phase 1 sampling must not exempt "fix" traffic; Phase 2's gate
     must assume iteration.
  4. **Tier signal.** The decisive findings needed the live numbers + the
     code together (session/execution context), not a single artifact —
     consistent with the tier-1/tier-2 distinction in
     `judge-sampling-pitch.md`.

## Appendix A: test harness (lru_test.go, interface{} variant)

The generics variant is the same suite with `New[string, int](n)` /
`New[int, int](128)` and `v.(int)` → `v`. Detection: `grep -q 'func New\['`.

```go
package lru

import (
	"sync"
	"testing"
)

func TestBasicPutGet(t *testing.T) {
	c := New(2)
	c.Put("a", 1)
	c.Put("b", 2)
	if v, ok := c.Get("a"); !ok || v.(int) != 1 {
		t.Fatalf("expected a=1, got %v %v", v, ok)
	}
}

func TestEviction(t *testing.T) {
	c := New(2)
	c.Put("a", 1)
	c.Put("b", 2)
	c.Get("a")    // a becomes MRU
	c.Put("c", 3) // must evict b
	if _, ok := c.Get("b"); ok {
		t.Fatal("expected b evicted")
	}
	if _, ok := c.Get("a"); !ok {
		t.Fatal("expected a retained")
	}
	if _, ok := c.Get("c"); !ok {
		t.Fatal("expected c present")
	}
}

func TestUpdateExisting(t *testing.T) {
	c := New(2)
	c.Put("a", 1)
	c.Put("a", 99)
	if v, _ := c.Get("a"); v.(int) != 99 {
		t.Fatalf("expected 99, got %v", v)
	}
}

func TestCapacityOne(t *testing.T) {
	c := New(1)
	c.Put("a", 1)
	c.Put("b", 2)
	if _, ok := c.Get("a"); ok {
		t.Fatal("a should be evicted")
	}
	if v, ok := c.Get("b"); !ok || v.(int) != 2 {
		t.Fatal("b should be present")
	}
}

func TestConcurrent(t *testing.T) {
	c := New(128)
	var wg sync.WaitGroup
	for w := 0; w < 8; w++ {
		wg.Add(1)
		go func(seed int) {
			defer wg.Done()
			for i := 0; i < 2000; i++ {
				k := (seed*7 + i) % 256
				c.Put(k, i)
				c.Get((k + 1) % 256)
			}
		}(w)
	}
	wg.Wait()
}
```

## Appendix B: Opus 4.6 output (arm A)

```go
package lru

import "sync"

type entry[K comparable, V any] struct {
	key        K
	value      V
	prev, next *entry[K, V]
}

type Cache[K comparable, V any] struct {
	mu       sync.RWMutex
	capacity int
	items    map[K]*entry[K, V]
	head     *entry[K, V]
	tail     *entry[K, V]
}

func New[K comparable, V any](capacity int) *Cache[K, V] {
	if capacity <= 0 {
		panic("lru: capacity must be positive")
	}
	return &Cache[K, V]{
		capacity: capacity,
		items:    make(map[K]*entry[K, V], capacity),
	}
}

func (c *Cache[K, V]) Get(key K) (V, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()

	e, ok := c.items[key]
	if !ok {
		var zero V
		return zero, false
	}
	c.moveToFront(e)
	return e.value, true
}

func (c *Cache[K, V]) Put(key K, value V) {
	c.mu.Lock()
	defer c.mu.Unlock()

	if e, ok := c.items[key]; ok {
		e.value = value
		c.moveToFront(e)
		return
	}

	e := &entry[K, V]{key: key, value: value}
	c.pushFront(e)
	c.items[key] = e

	if len(c.items) > c.capacity {
		c.evict()
	}
}

func (c *Cache[K, V]) Len() int {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return len(c.items)
}

func (c *Cache[K, V]) Remove(key K) bool {
	c.mu.Lock()
	defer c.mu.Unlock()

	e, ok := c.items[key]
	if !ok {
		return false
	}
	c.unlink(e)
	delete(c.items, key)
	return true
}

func (c *Cache[K, V]) evict() {
	if c.tail == nil {
		return
	}
	delete(c.items, c.tail.key)
	c.unlink(c.tail)
}

func (c *Cache[K, V]) pushFront(e *entry[K, V]) {
	e.prev = nil
	e.next = c.head
	if c.head != nil {
		c.head.prev = e
	}
	c.head = e
	if c.tail == nil {
		c.tail = e
	}
}

func (c *Cache[K, V]) moveToFront(e *entry[K, V]) {
	if c.head == e {
		return
	}
	c.unlink(e)
	c.pushFront(e)
}

func (c *Cache[K, V]) unlink(e *entry[K, V]) {
	if e.prev != nil {
		e.prev.next = e.next
	} else {
		c.head = e.next
	}
	if e.next != nil {
		e.next.prev = e.prev
	} else {
		c.tail = e.prev
	}
	e.prev = nil
	e.next = nil
}
```

## Appendix C: Qwen3.8-27B output (arm C)

```go
package lru

import "sync"

// Node represents a single entry in the doubly-linked list.
type Node struct {
	key   interface{}
	value interface{}
	prev  *Node
	next  *Node
}

// Cache is a concurrent-safe LRU cache with O(1) Get and Put operations.
type Cache struct {
	mu       sync.RWMutex
	capacity int
	head     *Node // sentinel head (most recently used side)
	tail     *Node // sentinel tail (least recently used side)
	items    map[interface{}]*Node
	size     int
}

// New creates a new LRU cache with the given capacity.
// If capacity is less than 1, it defaults to 1.
func New(capacity int) *Cache {
	if capacity < 1 {
		capacity = 1
	}
	c := &Cache{
		capacity: capacity,
		head:     &Node{},
		tail:     &Node{},
		items:    make(map[interface{}]*Node, capacity),
	}
	c.head.next = c.tail
	c.tail.prev = c.head
	return c
}

// Get returns the value associated with key and marks it as most recently used.
// The second return value indicates whether the key was present.
func (c *Cache) Get(key interface{}) (interface{}, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()

	node, ok := c.items[key]
	if !ok {
		return nil, false
	}
	c.moveToFront(node)
	return node.value, true
}

// Put inserts or updates the key-value pair. If the cache is at capacity and the
// key is new, the least recently used entry is evicted.
func (c *Cache) Put(key, value interface{}) {
	c.mu.Lock()
	defer c.mu.Unlock()

	if node, ok := c.items[key]; ok {
		node.value = value
		c.moveToFront(node)
		return
	}

	if c.size >= c.capacity {
		c.evict()
	}

	node := &Node{key: key, value: value}
	c.items[key] = node
	c.addToFront(node)
	c.size++
}

// Delete removes the entry with the given key. Returns true if the key was present.
func (c *Cache) Delete(key interface{}) bool {
	c.mu.Lock()
	defer c.mu.Unlock()

	node, ok := c.items[key]
	if !ok {
		return false
	}
	c.removeNode(node)
	delete(c.items, key)
	c.size--
	return true
}

// Len returns the number of entries currently in the cache.
func (c *Cache) Len() int {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.size
}

// Contains reports whether the key is present in the cache without affecting
// the access order.
func (c *Cache) Contains(key interface{}) bool {
	c.mu.RLock()
	defer c.mu.RUnlock()
	_, ok := c.items[key]
	return ok
}

// removeNode unlinks node from the doubly-linked list.
func (c *Cache) removeNode(node *Node) {
	node.prev.next = node.next
	node.next.prev = node.prev
	node.prev = nil
	node.next = nil
}

// addToFront inserts node immediately after the head sentinel.
func (c *Cache) addToFront(node *Node) {
	node.next = c.head.next
	node.prev = c.head
	c.head.next.prev = node
	c.head.next = node
}

// moveToFront repositions an existing node to the front of the list.
func (c *Cache) moveToFront(node *Node) {
	c.removeNode(node)
	c.addToFront(node)
}

// evict removes the least recently used entry (the node before the tail sentinel).
func (c *Cache) evict() {
	lru := c.tail.prev
	c.removeNode(lru)
	delete(c.items, lru.key)
	c.size--
}
```
