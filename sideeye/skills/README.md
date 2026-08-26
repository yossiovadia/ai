# Side-Eye Claude Code skills

Source-of-truth copies of the two on-demand review skills. They must be
**installed into personal scope** (`~/.claude/skills/`) to work in every repo —
personal-scope skills aren't in git, so these repo copies are the versioned
master. Re-run the install after editing either file.

## Install

```bash
# 1. Install the engine (once) so `sideeye` is on PATH:
pip install -e sideeye/          # from the repo root; or: pipx install ./sideeye

# 2. Copy the skills into personal scope:
cp -r sideeye/skills/escalate-all  sideeye/skills/escalate-last  ~/.claude/skills/
```

Verify: `sideeye --help` works, and `/escalate-all` / `/escalate-last` appear in
Claude Code's slash-command list.

## What they do

- **`/escalate-all`** → `sideeye review --current --yes` — full sighted review of
  the current session (transcript + `git diff`), verdict shown verbatim.
- **`/escalate-last <question>`** → `sideeye advise --current --yes --question ...`
  — quick second opinion on the last exchange.

Both are **user-invoked only** (`disable-model-invocation: true`) because they
spend real money, and both **present the tool output verbatim and never let the
model improvise a verdict** — a review invented by the cheap model is the exact
failure Side-Eye exists to prevent.

## Requirements at runtime
- `sideeye` on PATH.
- `SIDEEYE_JUDGE_BASE_URL` / `SIDEEYE_JUDGE_API_KEY` set to the **real Claude
  route** (not the cheap model). `run-claude-qwen.sh` exports these; if you launch
  Claude Code another way, set them yourself or the review self-routes to Qwen.
