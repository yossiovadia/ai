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
from sideeye.judge.code_artifact import build_diff_entries, render_diff_artifact  # noqa: E402
from sideeye.judge.judge import (  # noqa: E402
    DEFAULT_MAX_TOKENS,
    build_request_body,
    context_guard,
    context_window,
    estimate_cost,
    judge,
    judge_route_guard,
    load_rubric,
    resolve_model,
    rubric_version,
)
from sideeye.judge.transcript import first_user_ask, render, render_budgeted  # noqa: E402
from sideeye.record import (  # noqa: E402
    ADAPTER_VERSION_BLIND,
    ADAPTER_VERSION_BLIND_TIERED,
    ADAPTER_VERSION_SIGHTED,
    ADAPTER_VERSION_SIGHTED_TIERED,
    session_verdict_record,
)

# Headroom reserved below the context window when packing: output budget +
# a margin for tokenizer disagreement (our count vs the provider's) and JSON
# wrapping. Fail-closed — we'd rather tier one notch further than eat a 400.
_SAFETY_TOKENS = 3000
_INEXACT_EXTRA_MARGIN = 12000   # extra headroom when count_tokens is unavailable
_MAX_FIT_PROBES = 10            # binary-search probes (free count_tokens) to fill the window
# A full session review verdict (summary + several issues, each with a
# description) needs more output room than the 1024 default — a truncated
# record_verdict drops a required field and forces the retry. Give it headroom.
_REVIEW_MAX_TOKENS = 4096

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


def _fit_packet(transcript, asked, make_diff, rubric_text, *, base_url, api_key, model,
                max_tokens=DEFAULT_MAX_TOKENS):
    """Fit the judge packet into `model`'s context window.

    make_diff(budget_chars=None) -> the code artifact (str) or None. Passing a
    budget rebuilds the diff smaller (least-edited files degrade to manifest-only),
    which is the global diff-overflow rung.

    Returns (produced, input_tokens, est_cost, exact, reason, coverage):
      - coverage is None when the FULL packet fit (normal session, unchanged);
      - otherwise it describes what tiering kept/dropped (+ coverage['diff_degraded']).
    Calls fail() only if the packet can't be made to fit at all.

    The packet is built ONCE here and returned, so the cost estimate and the real
    judge call see the identical bytes (the old path rendered twice — estimate in
    escalate, call in judge_session — which would mismatch under tiering).
    All probes use the free count_tokens endpoint — no judge call, no spend."""
    window = context_window(model)

    def margin(exact):
        # Fail-closed: reserve the (review) output budget + a safety margin, plus
        # extra headroom when count_tokens is unavailable and we're on a chars/4 guess.
        return max_tokens + _SAFETY_TOKENS + (0 if exact else _INEXACT_EXTRA_MARGIN)

    def assemble(transcript_text, diff):
        produced = transcript_text + (("\n\n" + diff) if diff else "")
        body = build_request_body(rubric_text, asked, produced, model=model)
        input_tokens, est_cost, exact, reason = estimate_cost(base_url, api_key, body, model)
        return (input_tokens + margin(exact) <= window), (produced, input_tokens, est_cost, exact, reason)

    full_text = render(transcript)

    def search(diff, diff_degraded):
        # Find the LARGEST transcript char budget that still fits, so we USE the
        # window (keep tool-result evidence + recent narration) rather than
        # over-shrinking. chars/4 underestimates token-dense content, so we
        # converge on the real (exact) count. Test the floor (budget 0) FIRST —
        # if even that overflows, this diff can't be used at all; otherwise binary-
        # search upward from it for the most transcript that fits.
        floor_text, floor_cov = render_budgeted(transcript, 0)
        fits, result = assemble(floor_text, diff)
        if not fits:
            return None
        best = (*result, {**floor_cov, "diff_degraded": diff_degraded})
        lo, hi = 1, len(full_text)
        for _ in range(_MAX_FIT_PROBES):
            if lo > hi:
                break
            mid = (lo + hi) // 2
            transcript_text, coverage = render_budgeted(transcript, mid)
            fits, result = assemble(transcript_text, diff)
            if fits:
                best = (*result, {**coverage, "diff_degraded": diff_degraded})
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    # 1) Try the FULL packet. Normal sessions fit and ship whole, exactly as before.
    full_diff = make_diff()
    fits, result = assemble(full_text, full_diff)
    if fits:
        return (*result, None)

    # 2) Overflow → salience-tiered degrade with the full diff kept intact.
    best = search(full_diff, diff_degraded=False)
    if best is not None:
        return best

    # 3) Even the transcript floor + full diff overflows → the diff is too big.
    # Degrade the diff GLOBALLY (drop least-edited files to manifest-only) so the
    # most-edited code still gets reviewed. Budget by MEASURED tokens, not a char
    # ratio: code diffs tokenize ~2.5 chars/token (dense), so a chars/4 budget
    # overshoots. Binary-search the largest diff that fits with the sacred floor;
    # make_diff is cheap now (git ran once), so this is just count_tokens probes.
    floor_text = render_budgeted(transcript, 0)[0]
    floor_fits, (_, floor_tokens, *_r) = assemble(floor_text, None)
    if not floor_fits:
        fail(f"the human turns alone are ~{floor_tokens:,} tokens vs {model}'s {window:,} "
             "window — this session is too large to review in one call even narrative-only. "
             "Escalate a shorter/fresher session or a narrower scope.")

    if full_diff is not None:
        lo, hi, best_diff = 0, len(full_diff), make_diff(0)   # 0 = manifest-only floor
        for _ in range(_MAX_FIT_PROBES):
            if lo > hi:
                break
            mid = (lo + hi) // 2
            candidate = make_diff(mid)
            fits, _res = assemble(floor_text, candidate)
            if fits:
                best_diff, lo = candidate, mid + 1
            else:
                hi = mid - 1
        # With the largest fitting diff fixed, fill the rest of the window with
        # transcript tiers (tool evidence, then narration).
        best = search(best_diff, diff_degraded=len(best_diff) < len(full_diff))
        if best is not None:
            return best

    # 4) No diff and the floor still doesn't fit shouldn't reach here (floor_fits
    # was checked), but guard anyway rather than ship an over-budget packet.
    fail(f"cannot fit the packet into {model}'s {window:,} window. Re-run with --no-code "
         "for a narrative-only review, or narrow the change set.")


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

    # Build the code-review artifact lazily via make_diff(budget_chars): the judge
    # reviews the actual code (git diff with markers), not just narrative. Passing
    # a budget rebuilds a smaller diff (global diff-overflow rung). --no-code and
    # unresolvable/absent files fall back to narrative-only (blind), honestly.
    adapter_version = ADAPTER_VERSION_SIGHTED
    touched = transcript.get("touched_files") or []
    repo_root = pathlib.Path(args.repo) if args.repo else pathlib.Path.cwd()
    # Build the diff entries ONCE (git is the slow part); make_diff then re-renders
    # them cheaply at any budget for the overflow search — no re-shelling to git.
    diff_entries = ([] if args.no_code or not touched
                    else build_diff_entries(touched, repo_root))

    def make_diff(budget_chars=None):
        return render_diff_artifact(diff_entries, budget_chars) or None

    if args.no_code:
        print("Code    : --no-code (blind mode, v0) — narrative only")
        adapter_version = ADAPTER_VERSION_BLIND
    elif not touched:
        print("Code    : no touched files in transcript — narrative only")
        adapter_version = ADAPTER_VERSION_BLIND
    elif make_diff() is None:
        print("Code    : no diff produced (touched files unresolvable) — narrative only")
        adapter_version = ADAPTER_VERSION_BLIND
    else:
        print(f"Code    : {len(touched)} touched file(s) diffed (sighted, adapter v1)")

    # Fit the packet into the judge's window (builds `produced` once, shared by
    # the estimate and the real call). coverage is None if the full packet fit;
    # otherwise the transcript was salience-tiered and coverage says what dropped.
    produced, input_tokens, est_cost, exact, reason, coverage = _fit_packet(
        transcript, asked, make_diff, rubric_text,
        base_url=args.base_url, api_key=api_key, model=args.model,
        max_tokens=_REVIEW_MAX_TOKENS)

    if coverage is not None:
        # Tiered: stamp a DISTINCT adapter_version (a partial-narrative verdict is
        # not commensurable with a full-session one) and show what was dropped.
        # The tiering itself protects the failure-evidence — every human turn and
        # tool result — that a naive recency window would have silently dropped.
        adapter_version = (ADAPTER_VERSION_SIGHTED_TIERED
                           if adapter_version == ADAPTER_VERSION_SIGHTED
                           else ADAPTER_VERSION_BLIND_TIERED)
        k, d = coverage["kept"], coverage["dropped"]
        has_code = adapter_version == ADAPTER_VERSION_SIGHTED_TIERED
        diff_desc = ("" if not has_code
                     else " + partial diff (least-edited files elided)" if coverage.get("diff_degraded")
                     else " + full diff")
        print(f"Packet  : overflows {args.model}'s {context_window(args.model):,} window "
              "— salience-tiered to fit")
        print(f"          kept: all {k['human']} human turn(s){diff_desc}"
              f" + {k['assistant']} assistant + {k['tool']} tool turn(s)")
        print(f"          dropped (lower-salience, newest kept): {d['assistant']} assistant "
              f"narration + {d['tool']} tool output(s)"
              + (f"; {coverage['human_capped']} human turn(s) capped" if coverage["human_capped"] else ""))
        print(f"          verdict stamped {adapter_version} (won't pool with full-session verdicts)")

    if exact:
        print(f"Cost    : ~${est_cost:.4f}  ({input_tokens:,} input tokens at judge rate; "
              "output est.)")
    else:
        # reason explains WHY count_tokens failed (empty URL, network error,
        # HTTP 404, …) so the user can fix the route rather than guess.
        print(f"Cost    : ~${est_cost:.4f}  (~{input_tokens:,} input tokens, chars/4 "
              f"estimate — count_tokens unavailable ({reason}); output est.)")

    # Context-window guard: a HARD limit that no --max-cost can override. With
    # tiering the packet should now fit; this is the final assertion that catches
    # any residual overflow the fit loop couldn't resolve — refuse rather than eat
    # a 400 that's still metered (474k tokens once cost ~$4.77 for a rejected call).
    if (prob := context_guard(input_tokens, args.model, max_tokens=_REVIEW_MAX_TOKENS)):
        fail(prob)

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
        # Judge the packet we already fit + estimated — same bytes, no re-render
        # (judge_session would render the FULL transcript again, undoing tiering).
        verdict, meta = judge(asked, produced, rubric_text, base_url=args.base_url,
                              api_key=api_key, model=args.model, timeout=90,
                              max_tokens=_REVIEW_MAX_TOKENS)
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
