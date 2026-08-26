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

from sideeye.adapters import latest_session, load_transcript, resolve_current_session  # noqa: E402
from sideeye.judge.code_artifact import build_code_artifact  # noqa: E402
from sideeye.judge.judge import (  # noqa: E402
    build_request_body,
    estimate_cost,
    judge_route_guard,
    judge_session,
    load_rubric,
    resolve_model,
    rubric_version,
)
from sideeye.judge.transcript import first_user_ask, render  # noqa: E402
from sideeye.record import (  # noqa: E402
    ADAPTER_VERSION_BLIND,
    ADAPTER_VERSION_SIGHTED,
    session_verdict_record,
)

REPO = pathlib.Path(__file__).resolve().parent
DEFAULT_RUBRIC = REPO / "rubric" / "rubric_session_v2.md"
DEFAULT_OUT = REPO / "verdicts" / "escalated.jsonl"
ESCALATION_MODEL = "claude-opus-4-8"  # stronger by default

# Placeholder verdict for the error path: the judge ran (money spent) but
# produced no valid verdict. session_verdict_record needs the verdict shape to
# build a record; these sentinel values make the failure record self-explanatory
# and ensure is_flagged() won't mistake it for a real pass.
_EMPTY_VERDICT = {
    "answered_what_was_asked": False,
    "correctness": "incorrect",
    "claims_supported": False,
    "score": 1,
    "issues": [],
    "overall_severity": "critical",
    "summary": "(judge produced no valid verdict)",
}


def fail(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="Side-Eye on-demand escalation (human stream)")
    ap.add_argument("--rollout", default=None, help="session file (default: latest in scope)")
    ap.add_argument("--client", choices=("claude", "codex"), default="claude",
                    help="which client's latest session to escalate (default claude)")
    ap.add_argument("--project", default=None,
                    help="claude only: a specific project dirname under "
                         "~/.claude/projects/ (or a path). Defaults to the "
                         "current working directory's project, so 'escalate' "
                    "reviews the session you're in, not a random live one.")
    ap.add_argument("--all-projects", action="store_true",
                    help="search every project for the latest session (the old "
                         "default — a coin flip across all live sessions).")
    ap.add_argument("--current", action="store_true",
                    help="review the current project's latest session (this is the "
                         "default; explicit for the skill surface).")
    ap.add_argument("--yes", action="store_true",
                    help="skip the pre-judge confirmation (judge calls cost real money)")
    ap.add_argument("--max-cost", type=float, default=5.0,
                    help="abort if the estimated cost exceeds this ($). The safety "
                         "ceiling that survives --yes — a huge session can't silently overspend.")
    ap.add_argument("--tier", type=int, choices=(1, 2), default=1)
    ap.add_argument("--model", default=ESCALATION_MODEL)
    ap.add_argument("--rubric", default=str(DEFAULT_RUBRIC))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--repo", default=None,
                    help="git repo root for the code diff (default: cwd). The "
                         "session's touched files are diffed against HEAD here.")
    ap.add_argument("--no-code", action="store_true",
                    help="judge narrative-only (the old blind mode). For "
                         "blind-vs-sighted comparison; the verdict is stamped "
                         "v0-blind and must not pool with sighted verdicts.")
    # The judge must ALWAYS hit the real Claude route (dogfood), never GLM.
    # SIDEEYE_JUDGE_* wins so this is correct even in a run-claude-glm shell
    # where ANTHROPIC_BASE_URL points at the local GLM gateway.
    ap.add_argument("--base-url",
                    default=os.environ.get("SIDEEYE_JUDGE_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL"))
    args = ap.parse_args()
    args.model = resolve_model(args.model)   # accept fable/opus/sonnet aliases

    if args.tier == 2:
        fail("tier-2 (agentic: check out the code and RUN it) is not implemented "
             "in this POC. Tier 2 is a sandboxed judge-worker with tools, not a "
             "script — it is the only tier that catches execution-dependent "
             "defects. Use --tier 1 for transcript review.")

    api_key = os.environ.get("SIDEEYE_JUDGE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not args.base_url:
        fail("no judge route: set SIDEEYE_JUDGE_BASE_URL (or ANTHROPIC_BASE_URL) to the dogfood Claude route")
    if not api_key:
        fail("no judge key: set SIDEEYE_JUDGE_API_KEY (or ANTHROPIC_API_KEY)")
    if (prob := judge_route_guard(args.base_url)):
        fail(prob)

    # Session selection. Default scope = the current project (cwd), so escalate
    # reviews the session you're actually in — not the global-latest, which is a
    # coin flip across every live Claude session on the machine.
    if args.rollout:
        scope = None
        rollout = pathlib.Path(args.rollout)
    elif args.all_projects:
        scope = "all"
        rollout = latest_session(args.client, scope="all")
    elif args.project:
        scope = args.project
        rollout = latest_session(args.client, scope=args.project)
    else:
        scope = "cwd"
        # Robust to a drifted shell cwd (e.g. a skill's Bash tool running from
        # /tmp): try the cwd project, else fall back to the most-recently-written
        # session — the one you're actually in.
        rollout, fell_back = resolve_current_session(args.client)
        if fell_back and rollout:
            print(f"(cwd has no {args.client} session; using the most recently "
                  "active session instead)")
    if not rollout or not rollout.exists():
        if scope == "cwd":
            fail(f"no {args.client} session in this project "
                 f"({latest_session.__module__}.claude_project_dir). "
                 "Pass --all-projects to search everywhere, or run from the "
                 "session's working directory.")
        fail(f"no {args.client} session found")
    transcript = load_transcript(rollout)
    if transcript is None:
        fail(f"no usable turns in {rollout}")

    rubric_text = load_rubric(args.rubric)
    rv = rubric_version(args.rubric)

    # Show exactly what we're about to judge before spending money on it. The
    # first user turn is the cheapest recognition signal: if it's not the session
    # you meant, Ctrl-C now, before the judge call.
    first_ask = first_user_ask(transcript).replace("\n", " ")[:120]
    print(f"Session : {rollout}")
    print(f"           {transcript['session_id'][:24]}  ({len(transcript['turns'])} turns)")
    if first_ask:
        print(f"First ask: {first_ask}")
    print(f"Judge   : {args.model}, tier {args.tier}")

    # Pre-judge cost estimate on the REAL payload (the rendered transcript +
    # rubric + tool schema — the exact bytes the judge will receive). Input side
    # is exact via count_tokens; output is bounded and labeled est. This is the
    # guard against the $3.38 surprise: see the price before you commit.
    asked = first_user_ask(transcript)
    produced = render(transcript)

    # Build the code-review artifact: git diff of the session's touched files,
    # with markers, so the judge reviews the actual code, not just the narrative.
    # --no-code disables it (blind mode, for blind-vs-sighted comparison).
    adapter_version = ADAPTER_VERSION_SIGHTED
    code_artifact = None
    if not args.no_code:
        touched = transcript.get("touched_files") or []
        if touched:
            repo_root = pathlib.Path(args.repo) if args.repo else pathlib.Path.cwd()
            code_artifact = build_code_artifact(touched, repo_root)
            if code_artifact:
                produced = produced + "\n\n" + code_artifact
                nfiles = len(touched)
                print(f"Code    : {nfiles} touched file(s) diffed (sighted, adapter v1)")
            else:
                print("Code    : no diff produced (touched files unresolvable) — narrative only")
                adapter_version = ADAPTER_VERSION_BLIND
        else:
            print("Code    : no touched files in transcript — narrative only")
            adapter_version = ADAPTER_VERSION_BLIND
    else:
        print("Code    : --no-code (blind mode, v0) — narrative only")

    body = build_request_body(rubric_text, asked, produced, model=args.model)
    input_tokens, est_cost, exact, reason = estimate_cost(
        args.base_url, api_key, body, args.model)
    if exact:
        print(f"Cost    : ~${est_cost:.4f}  ({input_tokens:,} input tokens at judge rate; "
              "output est.)")
    else:
        # reason explains WHY count_tokens failed (empty URL, network error,
        # HTTP 404, …) so the user can fix the route rather than guess.
        print(f"Cost    : ~${est_cost:.4f}  (~{input_tokens:,} input tokens, chars/4 "
              f"estimate — count_tokens unavailable ({reason}); output est.)")

    # Cost ceiling: the anti-$3.38-surprise guard that survives --yes. Skips the
    # interactive prompt (needed for non-TTY skill use) but NOT the ceiling, so a
    # huge session can't overspend silently. Exits nonzero so the skill can react.
    if est_cost > args.max_cost:
        fail(f"estimated ${est_cost:.4f} exceeds --max-cost ${args.max_cost:.2f} "
             f"({input_tokens:,} input tokens). Re-run with --max-cost {est_cost + 1:.0f} to proceed.")

    if not args.yes:
        try:
            input("\nEscalate this session to the judge? [Enter to proceed, Ctrl-C to abort] ")
        except KeyboardInterrupt:
            print("\naborted before judge call (no spend).")
            return

    print(f"\nEscalating to {args.model}...\n")

    try:
        verdict, meta = judge_session(transcript, rubric_text, base_url=args.base_url,
                                      api_key=api_key, model=args.model,
                                      code_artifact=code_artifact)
    except Exception as exc:
        # The judge ran (money was spent) but produced no valid verdict even
        # after retry. Don't crash and lose the evidence — record the failure
        # so the spend is documented and the session isn't silently retried.
        from sideeye.judge.schema import VerdictError
        kind = "malformed_verdict" if isinstance(exc, VerdictError) else "judge_error"
        record = session_verdict_record(transcript, _EMPTY_VERDICT, {"judge_model": args.model,
                                        "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                                        "retries": 1}, rubric_version=rv, source="escalated",
                                        tier=args.tier, judged_at=int(time.time()),
                                        adapter_version=adapter_version)
        record["error"] = f"{kind}: {exc}"
        out_path = pathlib.Path(args.out)
        out_path.parent.mkdir(exist_ok=True)
        with open(out_path, "a", encoding="utf-8") as out:
            out.write(json.dumps(record) + "\n")
        # Failure goes to stderr (the skill reads stderr on nonzero) and we exit
        # nonzero — the spend is recorded, but there is no verdict, so the caller
        # must NOT treat this as success (and must not fabricate one).
        print(f"JUDGE FAILED after spend: {exc}", file=sys.stderr)
        print(f"failure recorded (no valid verdict) -> {out_path}", file=sys.stderr)
        sys.exit(1)

    record = session_verdict_record(transcript, verdict, meta, rubric_version=rv,
                                    source="escalated", tier=args.tier,
                                    judged_at=int(time.time()),
                                    adapter_version=adapter_version)

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
