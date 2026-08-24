"""Shared verdict-record shape for session judging.

Both the sampler (random stream) and escalate (human stream) produce this same
record — but write to SEPARATE files, because escalated verdicts are an
adversarially selected sample and must never be pooled into the random-sample
quality/savings aggregate (they would skew the scoreboard). The record carries
the generation usage the cost report needs for the counterfactual savings math.
"""
from __future__ import annotations


def session_verdict_record(transcript, verdict, meta, *, rubric_version, source,
                           tier, judged_at):
    gu = transcript.get("generation_usage") or {}
    return {
        "session_id": transcript["session_id"],
        "source": source,               # "sampled" | "escalated"
        "tier": tier,                   # 1 = transcript review, 2 = agentic (future)
        "rubric_version": rubric_version,
        "judge_model": meta["judge_model"],
        "score": verdict["score"],
        "correctness": verdict["correctness"],
        "claims_supported": verdict["claims_supported"],
        "issues": verdict["issues"],
        "overall_severity": verdict["overall_severity"],
        "summary": verdict["summary"],
        "judge_input_tokens": meta["input_tokens"],
        "judge_output_tokens": meta["output_tokens"],
        "judge_cost_usd": meta["cost_usd"],
        # Generation side — what the cheap/free model actually spent. Drives the
        # counterfactual "what would this have cost on an expensive model" math.
        "generation_model": transcript.get("model"),
        "generation_input_tokens": gu.get("input_tokens", 0),
        "generation_output_tokens": gu.get("output_tokens", 0),
        "generation_total_tokens": gu.get("total_tokens", 0),
        "judged_at": judged_at,
    }
