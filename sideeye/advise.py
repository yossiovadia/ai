#!/usr/bin/env python3
"""Side-Eye ADVISE — a quick second opinion on the last exchange.

The light sibling of `escalate`: instead of reviewing a whole session with the
code diff, it sends only the LAST user->assistant exchange (plus an optional
--question) to the strong judge and prints a free-form recommendation. This is
the `/escalate-last` surface — "which of these should I pick?" — a judgement
call, not a code review, so the packet is tiny (~$0.02-0.05).

Like escalate, the judge MUST hit the real Claude route, never the cheap model:
SIDEEYE_JUDGE_BASE_URL / SIDEEYE_JUDGE_API_KEY win over ANTHROPIC_* so this is
correct even when run from inside a Qwen session (whose ANTHROPIC_BASE_URL points
at the cheap route). judge_route_guard is the last line of defense.

  python -m sideeye.advise --current --question "SQLite or JSONL for verdicts?"
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sideeye.adapters import latest_session, load_transcript  # noqa: E402
from sideeye.judge.judge import (  # noqa: E402
    advise as run_advise,
    build_advice_body,
    estimate_cost,
    judge_route_guard,
    load_rubric,
    resolve_judge_route,
)
from sideeye.judge.transcript import last_exchange  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent
DEFAULT_RUBRIC = REPO / "rubric" / "rubric_advice_v1.md"
DEFAULT_OUT = REPO / "verdicts" / "advice.jsonl"
ADVICE_MODEL = "claude-fable-5"      # advice is human-bounded + tiny; use the top tier
DEFAULT_MAX_COST = 0.50              # advice packets are tiny; this only trips on a bug


def fail(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="Side-Eye advise (quick second opinion)")
    ap.add_argument("--question", default="", help="the judgement call you want an opinion on")
    ap.add_argument("--current", action="store_true",
                    help="use the current project's latest session (default behavior; "
                         "explicit for the skill surface)")
    ap.add_argument("--rollout", default=None, help="a specific session file")
    ap.add_argument("--client", choices=("claude", "codex"), default="claude")
    ap.add_argument("--all-projects", action="store_true",
                    help="search every project for the latest session (default: current project)")
    ap.add_argument("--project", default=None, help="a specific project dirname under ~/.claude/projects/")
    ap.add_argument("--model", default=ADVICE_MODEL)
    ap.add_argument("--rubric", default=str(DEFAULT_RUBRIC))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--max-cost", type=float, default=DEFAULT_MAX_COST,
                    help="abort if the estimated cost exceeds this (safety ceiling)")
    ap.add_argument("--yes", action="store_true", help="(compat) advise never prompts; accepted and ignored")
    # Judge route: SIDEEYE_JUDGE_* wins over ANTHROPIC_* so a Qwen-session shell
    # can't route the expensive review to the cheap model. --base-url overrides.
    ap.add_argument("--base-url", default=None)
    args = ap.parse_args()

    env_base, api_key = resolve_judge_route()
    args.base_url = args.base_url or env_base
    if not args.base_url:
        fail("no judge route: set SIDEEYE_JUDGE_BASE_URL to the real Claude route")
    if not api_key:
        fail("no judge key: set SIDEEYE_JUDGE_API_KEY")
    if (prob := judge_route_guard(args.base_url)):
        fail(prob)

    # Session selection: default = this project's latest session (so advise
    # reviews the session you're in, not a coin flip across all live sessions).
    if args.rollout:
        rollout = pathlib.Path(args.rollout)
    elif args.all_projects:
        rollout = latest_session(args.client, scope="all")
    elif args.project:
        rollout = latest_session(args.client, scope=args.project)
    else:  # --current or default
        rollout = latest_session(args.client, scope="cwd")
    if not rollout or not rollout.exists():
        fail(f"no {args.client} session found (run from the session's project dir, "
             "or pass --rollout / --all-projects)")

    transcript = load_transcript(rollout)
    if transcript is None:
        fail(f"no usable turns in {rollout}")

    produced = last_exchange(transcript)
    rubric_text = load_rubric(args.rubric)

    # Cost ceiling: input count is exact (count_tokens on the real payload);
    # abort before spending if it somehow exceeds the ceiling.
    body = build_advice_body(rubric_text, args.question, produced, model=args.model)
    input_tokens, est_cost, exact, _ = estimate_cost(args.base_url, api_key, body, args.model)
    if est_cost > args.max_cost:
        fail(f"estimated ${est_cost:.4f} exceeds --max-cost ${args.max_cost:.2f} "
             f"({input_tokens:,} input tokens). Re-run with --max-cost to override.")

    print("sideeye · advice mode")
    print(f"  packet: last exchange ({input_tokens:,} tokens{'' if exact else ', est'}) "
          f"-> judge: {args.model}")
    print("  route: metered praxis gateway\n")

    try:
        text, meta = run_advise(args.question, produced, rubric_text,
                                base_url=args.base_url, api_key=api_key, model=args.model)
    except Exception as exc:  # noqa: BLE001
        fail(f"judge call failed: {exc}")

    record = {
        "session_id": transcript["session_id"],
        "source": "advice",
        "question": args.question,
        "advice": text,
        "judge_model": meta["judge_model"],
        "judge_input_tokens": meta["input_tokens"],
        "judge_output_tokens": meta["output_tokens"],
        "judge_cost_usd": meta["cost_usd"],
        "judged_at": int(time.time()),
    }
    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, "a", encoding="utf-8") as out:
        out.write(json.dumps(record) + "\n")

    print("SECOND OPINION")
    for line in text.splitlines():
        print(f"  {line}" if line.strip() else "")
    print(f"\n  judge cost: ${meta['cost_usd']:.4f}  ->  {out_path}")


if __name__ == "__main__":
    main()
