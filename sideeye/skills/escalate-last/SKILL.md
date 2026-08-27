---
name: Escalate Last
description: Side-Eye judge's opinion on your recent exchange(s) + their code diff. Flags — --turns, --no-code, --model, --help. User-invoked; spends ~cents.
disable-model-invocation: true
allowed-tools: Bash(sideeye advise *)
---

# /escalate-last — quick second opinion on your recent work

A scoped gut-check on the most recent exchange(s): "is my recent change / decision
sound?" It's **sighted** — the judge sees the tool results and the git diff of the
files touched in those exchanges (recent-scoped, so it stays cheap). Use `--no-code`
for a pure judgement call ("which design option?") where no code was written.

## What to do

**If the user's arguments are exactly `--help` (or `help`), do NOT run the judge
— print the Flags section below and stop (no spend).**

Treat everything the user typed after `/escalate-last` as their question (it may
be empty — that's fine). Run:

```
sideeye advise --current --yes --question "<the user's text here, or empty>"
```

**Pass through any flags the user typed** (e.g. `/escalate-last --turns=3 --no-code
which one?` → `sideeye advise --current --yes --turns 3 --no-code --question "which one?"`).
Pass through only flags the user actually typed; don't invent them.

## Flags (what the user can pass after /escalate-last)

- `--turns N` — how many recent user→assistant exchanges to include (default 1).
  Raise it when the question spans the last few turns.
- `--no-code` — skip the diff; conversation-only opinion (for judgement calls
  where no code was written yet).
- `--model <fable|opus|sonnet|haiku|full-id>` — which judge model (default fable).
- `--max-cost N` — abort if the estimate exceeds N dollars (safety ceiling).
- `--repo <path>` — repo root for the diff (default: current dir).

(The authoritative list is always `sideeye advise --help`.)

Then:

1. **Present the command's stdout VERBATIM.** Do not paraphrase or "improve" the
   second opinion — show it exactly.
2. **On a nonzero exit code:** show stderr verbatim and **STOP**. Do not retry.
3. **NEVER write the opinion yourself.** Only the real stdout of `sideeye advise`
   counts — a second opinion invented here comes from the cheap model, which
   defeats the purpose. If the command fails, say so and stop.

## Notes
- Requires `sideeye` on PATH and `SIDEEYE_JUDGE_BASE_URL` / `SIDEEYE_JUDGE_API_KEY`
  set to the real Claude route (the launcher sets these). Surface a "no judge
  route" error rather than working around it.
