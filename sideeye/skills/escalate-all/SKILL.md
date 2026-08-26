---
name: Escalate All
description: Send the CURRENT Claude Code session to the Side-Eye judge (Fable) for a full sighted review (transcript + code diff). User-invoked only; spends real money.
disable-model-invocation: true
allowed-tools: Bash(sideeye review *)
---

# /escalate-all — full session review

Get a real, independent review of THIS session from the strong judge.

## What to do

Run exactly this command, with a **generous Bash timeout (≥ 300000 ms)** — a
full-session judge call can take a few minutes:

```
sideeye review --current --yes
```

Then:

1. **Present the command's stdout VERBATIM**, inside a fenced code block. Do not
   summarize, soften, reorder, re-score, or "clean up" any of it. That output is
   the judge's verdict — the user must see it exactly as written. After the
   verbatim block, you may add one short line offering to address the issues it
   lists.

2. **On a nonzero exit code:** show the command's stderr verbatim and **STOP**.
   Do not retry with different flags or arguments.

3. **NEVER write a review yourself.** This is the whole point of the tool: a
   verdict invented here would be produced by the *cheap* model this session runs
   on — the exact plausible-but-fake output Side-Eye exists to catch. Only the
   real stdout of `sideeye review` is a verdict. If the command fails, returns
   nothing, or is blocked, say so plainly and stop — do not fabricate scores,
   issues, or a summary under any circumstances.

## Notes
- The `sideeye` command must be on PATH and `SIDEEYE_JUDGE_BASE_URL` /
  `SIDEEYE_JUDGE_API_KEY` must point at the real Claude route (the launcher sets
  these). If you see a "no judge route" error, that env isn't set — surface it,
  don't work around it.
- If the estimate exceeds the cost ceiling, the command aborts with a message
  telling the user how to raise `--max-cost`. Show it; don't bypass it.
