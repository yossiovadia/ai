---
name: Escalate Last
description: Ask the Side-Eye judge (Fable) for a quick second opinion on the last exchange in this session. User-invoked only; spends a few cents.
disable-model-invocation: true
allowed-tools: Bash(sideeye advise *)
---

# /escalate-last — quick second opinion

A fast gut-check on the most recent exchange (e.g. "which design option should I
pick?"). Light packet, ~cents.

## What to do

Treat everything the user typed after `/escalate-last` as their question (it may
be empty — that's fine). Run:

```
sideeye advise --current --yes --question "<the user's text here, or empty>"
```

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
