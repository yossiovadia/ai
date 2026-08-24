# Side-Eye POC

A time-boxed proof of concept for the Side-Eye pitch
(`../docs/judge-sampling-pitch.md`): *draft cheap, review expensive* — a cheap
(here: free, internal **GLM-5.2**) model does the work; its output is sampled to
an expensive **Claude** judge, asynchronously, outside the request path. The goal
is a leadership-ready answer to *"how much can we save at held quality?"*

**This is a POC.** Nothing here touches `filters/` or `apis/`. It is plain Python
glue — deliberately: the perf-critical hot path is the gateway (Rust/praxis); the
judge worker is I/O-bound (one multi-second LLM call), so language choice is
irrelevant off-path. See `../docs/sideeye-phase-c-questions.md` for the design
decisions this implements.

## Layout

```
sideeye/
  rubric/rubric_v1.md            pair-grading rubric (Phase B)
  rubric/rubric_session_v1.md    session-grading rubric (Phase C, tier-1)
  judge/schema.py                verdict schema + validation + is_flagged()
  judge/transcript.py            capture-agnostic SessionTranscript (the pipe)
  judge/judge.py                 forced-tool-call structured verdict; judge_session()
  adapters/codex_rollout.py      adapter #1: Codex rollout log -> SessionTranscript
  record.py                      shared session-verdict record shape
  run_judge.py                   Phase B: grade a JSONL of (prompt, answer) pairs
  sampler.py                     Phase C: RANDOM stream — scan rollouts, judge, verdicts/sampled.jsonl
  escalate.py                    Phase C: HUMAN stream — "ask the expensive model", verdicts/escalated.jsonl
  cost_report.py                 the money story: counterfactual savings + quality, CLI + HTML
  tools/                         seed + hard-task generators (need VPN + GLM)
  tests/                         unit tests (no network): 34 passing
  data/                          seed_pairs, hard_tasks, ground_truth (fixtures)
  verdicts/                      run output (gitignored)
```

## The capture-agnostic pipe (the key design decision)

Every capture source produces a **`SessionTranscript`** (`judge/transcript.py`);
the judge, sampler, and cost report only ever see that shape. So the two capture
options are just two adapters into one pipe:

- **Adapter #1 — `codex_rollout`** (this POC): reads Codex's own session logs.
  In-scope, no praxis changes, full session transcript. Caveat: it's the
  *client's* view — if the gateway mutated the request, the log wouldn't show it.
- **Adapter #2 — gateway sampling tap** (production): a praxis filter that
  samples at request time and emits events. Not built here (it's Rust in the
  proxy); the schema is designed so POC results carry over unchanged.

## Phase B — pair judging (falsification gate) — DONE

```bash
python -m pytest sideeye/tests -q                       # 34 tests, no network
python -m sideeye.run_judge --pairs sideeye/data/seed_pairs.jsonl \
    --ground-truth sideeye/data/ground_truth.json       # needs ANTHROPIC_* env
```
Result: judge caught 2/2 planted defects, 0/4 clean false-positives; on hard
field-evidence tasks it correctly passed GLM's safe answers and flagged the
truncated one. ~$0.008/pair on Sonnet 5.

## Phase C — automatic capture + escalation

Two verdict streams, kept **separate** (escalations are an adversarial sample and
must never skew the random-sample scoreboard):

```bash
# RANDOM stream — unbiased quality/savings estimate (Sonnet 5)
python -m sideeye.sampler --idle-min 10 --sample-rate 1.0

# HUMAN stream — "I'm not sure, ask the expensive model" (Opus 4.8 by default)
python -m sideeye.escalate                    # latest session, tier-1 review
python -m sideeye.escalate --model claude-fable-5   # the nasty ones

# The money story (counterfactual savings + held quality; CLI + HTML)
python -m sideeye.cost_report --html sideeye/verdicts/cost-report.html
```

Escalation is **review, not re-answer**; it carries a **tier knob** (tier-1
transcript review = default; tier-2 agentic "run the code" = the only tier that
catches execution-dependent defects, and is a sandboxed worker, not a script —
not built in this POC, and it refuses honestly if asked).

The whole pipeline (capture → judge → verdict → cost) is verified end-to-end on
real Codex rollouts. The judge routes through the dogfood Anthropic route, so
judge spend is metered on the dashboard.

## Deferred: live GLM plumbing (needs VPN)

Only the live wiring waits for a "start on GLM" session: the CONNECT tunnel
(Python glue exposing GLM as localhost), the local praxis GLM config, the
all-zeros GLM pricing row (so GLM shows at $0), and the Codex provider profile.
Once those run, real Codex→praxis→GLM sessions flow into the exact pipeline above
and the cost report fills with real numbers.

## Rules honored

- Secrets from env / `~/.glm-52` only — never hardcoded, committed, or logged.
- Model string standardized on `rits/zai-org/glm-5-2-fp8`.
- Judge routes through dogfood so judge spend is visible.
- Counterfactual savings are labeled estimates; GLM actual cost is $0.
