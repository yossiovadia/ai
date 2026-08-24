#!/usr/bin/env python3
"""Side-Eye escalate — the HUMAN stream ("I'm not sure, ask the expensive model").

The on-demand fourth trigger: a human escalates a specific session for review by
a stronger judge. Same judge machinery as the sampler, manually triggered.

Design decisions (from the Side-Eye review):
  - REVIEW, not re-answer: the expensive model critiques; it never takes over the
    turn. A review verdict is commensurable with the sampler's verdicts.
  - Stronger judge by default (Opus 4.8): the human already paid attention, so
    the prior of a real defect is elevated and volume is human-bounded.
  - SEPARATE stream (verdicts/escalated.jsonl): escalations are adversarially
    selected and must never be pooled into the random-sample scoreboard. They are
    high-yield defect discovery + a curated hard-set for calibrating the judge.
  - A TIER knob, not just a model knob: escalation buys SELECTION, not new
    capability. Tier 1 = transcript review (default). Tier 2 = agentic
    verification (run the code) — the only thing that catches execution-dependent
    defects; not built in this POC (it's a sandboxed worker, not a script).

Env: ANTHROPIC_BASE_URL, ANTHROPIC_API_KEY.

  python -m sideeye.escalate                 # latest session, tier 1, Opus 4.8
  python -m sideeye.escalate --rollout <path> --model claude-fable-5
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sideeye.adapters.codex_rollout import parse_rollout  # noqa: E402
from sideeye.judge.judge import judge_session, load_rubric, rubric_version  # noqa: E402
from sideeye.record import session_verdict_record  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent
DEFAULT_ROLLOUT_DIR = pathlib.Path.home() / ".codex" / "sessions"
DEFAULT_RUBRIC = REPO / "rubric" / "rubric_session_v1.md"
DEFAULT_OUT = REPO / "verdicts" / "escalated.jsonl"
ESCALATION_MODEL = "claude-opus-4-8"  # stronger by default


def fail(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def latest_rollout(rollout_dir):
    rollouts = sorted(pathlib.Path(rollout_dir).rglob("rollout-*.jsonl"),
                      key=lambda p: p.stat().st_mtime)
    return rollouts[-1] if rollouts else None


def main():
    ap = argparse.ArgumentParser(description="Side-Eye on-demand escalation (human stream)")
    ap.add_argument("--rollout", default=None, help="rollout file (default: latest)")
    ap.add_argument("--rollout-dir", default=str(DEFAULT_ROLLOUT_DIR))
    ap.add_argument("--tier", type=int, choices=(1, 2), default=1)
    ap.add_argument("--model", default=ESCALATION_MODEL)
    ap.add_argument("--rubric", default=str(DEFAULT_RUBRIC))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--base-url", default=os.environ.get("ANTHROPIC_BASE_URL"))
    args = ap.parse_args()

    if args.tier == 2:
        fail("tier-2 (agentic: check out the code and RUN it) is not implemented "
             "in this POC. Tier 2 is a sandboxed judge-worker with tools, not a "
             "script — it is the only tier that catches execution-dependent "
             "defects. Use --tier 1 for transcript review.")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not args.base_url:
        fail("ANTHROPIC_BASE_URL not set")
    if not api_key:
        fail("ANTHROPIC_API_KEY not set")

    rollout = pathlib.Path(args.rollout) if args.rollout else latest_rollout(args.rollout_dir)
    if not rollout or not rollout.exists():
        fail(f"no rollout found (dir: {args.rollout_dir})")
    transcript = parse_rollout(rollout)
    if transcript is None:
        fail(f"no usable turns in {rollout}")

    rubric_text = load_rubric(args.rubric)
    rv = rubric_version(args.rubric)
    print(f"Escalating session {transcript['session_id'][:24]} "
          f"({len(transcript['turns'])} turns) to {args.model}, tier {args.tier}...\n")

    verdict, meta = judge_session(transcript, rubric_text, base_url=args.base_url,
                                  api_key=api_key, model=args.model)
    record = session_verdict_record(transcript, verdict, meta, rubric_version=rv,
                                    source="escalated", tier=args.tier,
                                    judged_at=int(time.time()))

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, "a", encoding="utf-8") as out:
        out.write(json.dumps(record) + "\n")

    # Interactive: show the review prominently — the human asked to see it.
    print(f"  score={record['score']}/5   correctness={record['correctness']}   "
          f"claims_supported={record['claims_supported']}   severity={record['overall_severity']}")
    print(f"  {record['summary']}")
    for it in record["issues"]:
        print(f"    [{it['severity']}] {it['description']}")
    print(f"\n  judge cost: ${record['judge_cost_usd']:.4f}  ->  {out_path}")
    print("  (recorded in the SEPARATE escalated stream — not pooled with random samples)")


if __name__ == "__main__":
    main()
