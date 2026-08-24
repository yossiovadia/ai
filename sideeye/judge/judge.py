"""Side-Eye judge: grade a (request, response) with a versioned rubric via
Claude, using a forced tool call for structured output.

Reused unchanged for pair grading (Phase B) and session grading (Phase C):
`asked` is the user request, `produced` is either a single answer or a whole
session transcript. The judge routes through the dogfood Anthropic route so the
judge's own spend is metered on the dashboard.
"""
from __future__ import annotations

import pathlib

import requests

from .schema import (
    CORRECTNESS_VALUES,
    OVERALL_SEVERITY_VALUES,
    SEVERITY_VALUES,
    validate_verdict,
)
from .transcript import first_user_ask, render

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_MAX_TOKENS = 1024

# Judge-model pricing, USD per token (input, output), from the dogfood
# model_pricing table (verified 2026-08-21). Used only to report the judge
# call's own cost in each verdict; the authoritative bill is the metered spend.
PRICING = {
    "claude-sonnet-5": (2.0 / 1e6, 10.0 / 1e6),
    "claude-opus-4-8": (5.0 / 1e6, 25.0 / 1e6),
    "claude-fable-5": (10.0 / 1e6, 50.0 / 1e6),
}

RECORD_VERDICT_TOOL = {
    "name": "record_verdict",
    "description": "Record the structured grading verdict for the response under review.",
    "input_schema": {
        "type": "object",
        "properties": {
            "answered_what_was_asked": {
                "type": "boolean",
                "description": "Did the response address the actual request, all parts of it?",
            },
            "correctness": {
                "type": "string",
                "enum": list(CORRECTNESS_VALUES),
                "description": "Overall technical correctness of the response.",
            },
            "claims_supported": {
                "type": "boolean",
                "description": (
                    "False if the answer makes any claim its own content cannot "
                    "support or contradicts (e.g. 'tests pass' with no evidence)."
                ),
            },
            "score": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
                "description": "1 (fundamentally wrong) to 5 (correct, complete, well-supported).",
            },
            "issues": {
                "type": "array",
                "description": "Specific defects found; empty if none.",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "severity": {"type": "string", "enum": list(SEVERITY_VALUES)},
                    },
                    "required": ["description", "severity"],
                },
            },
            "overall_severity": {
                "type": "string",
                "enum": list(OVERALL_SEVERITY_VALUES),
                "description": "Severity of the worst issue present.",
            },
            "summary": {"type": "string", "description": "One-sentence verdict."},
        },
        "required": [
            "answered_what_was_asked",
            "correctness",
            "claims_supported",
            "score",
            "issues",
            "overall_severity",
            "summary",
        ],
    },
}


def load_rubric(path) -> str:
    return pathlib.Path(path).read_text(encoding="utf-8")


def rubric_version(path) -> str:
    """The rubric version is the file stem, e.g. rubric_v1.md -> 'rubric_v1'."""
    return pathlib.Path(path).stem


def build_request_body(rubric_text, asked, produced, model=DEFAULT_MODEL,
                       max_tokens=DEFAULT_MAX_TOKENS):
    """Build the Anthropic Messages request. Rubric goes in the system prompt;
    the pair goes in the user turn; the tool call is forced."""
    user = (
        "## What was asked\n\n"
        + asked.strip()
        + "\n\n## The response under review\n\n"
        + produced.strip()
        + "\n\nGrade the response using the rubric, then call record_verdict."
    )
    return {
        "model": model,
        "max_tokens": max_tokens,
        "system": rubric_text,
        "messages": [{"role": "user", "content": user}],
        "tools": [RECORD_VERDICT_TOOL],
        "tool_choice": {"type": "tool", "name": "record_verdict"},
    }


def parse_verdict(response_json):
    """Extract and validate the verdict from the judge's tool_use block."""
    for block in response_json.get("content", []) or []:
        if block.get("type") == "tool_use" and block.get("name") == "record_verdict":
            return validate_verdict(block.get("input"))
    raise ValueError("no record_verdict tool_use block found in judge response")


def cost_usd(model, usage) -> float:
    cin, cout = PRICING.get(model, PRICING[DEFAULT_MODEL])
    return round(usage.get("input_tokens", 0) * cin + usage.get("output_tokens", 0) * cout, 6)


def call_judge(base_url, api_key, body, timeout=60):
    url = base_url.rstrip("/") + "/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    resp = requests.post(url, headers=headers, json=body, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def judge(asked, produced, rubric_text, *, base_url, api_key,
          model=DEFAULT_MODEL, max_tokens=DEFAULT_MAX_TOKENS, timeout=60):
    """Grade one (asked, produced) pair. Returns (verdict, meta)."""
    body = build_request_body(rubric_text, asked, produced, model=model, max_tokens=max_tokens)
    response = call_judge(base_url, api_key, body, timeout=timeout)
    verdict = parse_verdict(response)
    usage = response.get("usage", {}) or {}
    meta = {
        "judge_model": model,
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cost_usd": cost_usd(model, usage),
    }
    return verdict, meta


def judge_session(transcript, rubric_text, *, base_url, api_key,
                  model=DEFAULT_MODEL, max_tokens=DEFAULT_MAX_TOKENS, timeout=90):
    """Grade a whole SessionTranscript. Renders it to (asked, produced) and
    reuses the pair judge unchanged — the judge doesn't care whether `produced`
    is one answer or a full transcript."""
    asked = first_user_ask(transcript)
    produced = render(transcript)
    return judge(asked, produced, rubric_text, base_url=base_url, api_key=api_key,
                 model=model, max_tokens=max_tokens, timeout=timeout)
