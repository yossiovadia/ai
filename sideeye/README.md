# Side-Eye POC

A time-boxed proof of concept for the Side-Eye pitch
(`docs/judge-sampling-pitch.md`): *draft cheap, review expensive* — have a cheap
(here: free, internal **GLM-5.2**) model do the work, and sample its output to an
expensive **Claude** judge, asynchronously, outside the request path.

**This is a POC.** Nothing here touches `filters/` or `apis/`. It is plain Python
glue and scripts. The gates route through a human — see the phase gates below.

## Layout

```
sideeye/
  rubric/rubric_v1.md        versioned grading rubric (rubric_version = file stem)
  judge/schema.py            verdict schema + validation + is_flagged()
  judge/judge.py             build request -> forced tool call -> parse verdict
  run_judge.py               CLI: grade a JSONL of pairs -> verdicts + summary
  tools/generate_seed_pairs.py   build the POC-0 seed set (real GLM + 2 planted)
  tests/test_judge.py        unit tests (rubric load, schema, tool parsing) — no net
  data/seed_pairs.jsonl      what the judge sees: {id, prompt, answer}
  data/ground_truth.json     planted-defect ground truth (NEVER sent to the judge)
  verdicts/                  run output (gitignored)
```

## Phase B — POC-0: the judge loop (the falsification point)

The judge grades each `(prompt, answer)` pair through the **dogfood Anthropic
route** (so its own spend is metered) using **claude-sonnet-5** with a forced
tool call for structured output — no parse-and-pray.

Objective harness: the seed set contains two **planted defects** — one
plausible-but-wrong API claim, one unsupported "tests pass" claim — mixed among
real GLM answers. The judge is not told which. Gate B measures whether it catches
both planted defects **without** flagging the clean answers.

```bash
# 1. build the seed set (needs VPN + proxy + ~/.glm-52)
HTTPS_PROXY=http://10.2.32.57:3128 python sideeye/tools/generate_seed_pairs.py

# 2. tests (offline)
python -m pytest sideeye/tests -q

# 3. run the judge (needs ANTHROPIC_BASE_URL + ANTHROPIC_API_KEY in env)
python -m sideeye.run_judge \
    --pairs sideeye/data/seed_pairs.jsonl \
    --ground-truth sideeye/data/ground_truth.json
```

Cost: ~$0.01–0.02 per pair on Sonnet 5 (input-heavy; verdicts are tiny).

**Gate B:** a human reads the verdicts against their own opinion of the answers.
If the judge's signal is noise, the project stops here — the cheapest
falsification point Side-Eye will ever have.

## Carry-forwards for Phase C (capture pipeline — not built yet)

1. **`stream_options.include_usage`** — streamed responses carry usage only if
   the client asks for it. Verify Codex sets it, or plan for praxis to inject it,
   or GLM tokens show as zero on the dashboard.
2. **`reasoning_content`** — GLM-5.2 is a reasoning model and emits thinking in a
   separate field. Store it, but the rubric grades the served `content` only.

## Rules honored

- Secrets from env / `~/.glm-52` only — never hardcoded, committed, or logged.
- Model string standardized on `rits/zai-org/glm-5-2-fp8` everywhere.
- Judge routes through dogfood so judge spend is visible on the dashboard.
- Rubric is versioned; verdicts record the rubric version and judge model.
