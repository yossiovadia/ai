"""Capture-agnostic session transcript — the normalized unit the judge consumes.

This is the decision that makes Option A (read client rollout logs) and Option B
(a gateway sampling tap) two *adapters* into the same pipe rather than a fork:
every capture source produces a SessionTranscript, and the judge/sampler/cost
report only ever see this shape. Nothing downstream knows or cares where a
transcript came from.
"""
from __future__ import annotations

ROLES = ("user", "assistant", "tool", "system")


class TranscriptError(ValueError):
    """Raised when a transcript does not conform to the schema."""


def make_transcript(*, session_id, source, turns, model=None, created_at=None,
                    generation_usage=None):
    """Build + validate a SessionTranscript.

    - session_id: stable id for the session (grouping key).
    - source: the adapter that produced it (provenance), e.g. "codex_rollout".
    - turns: ordered list of {role, text}.
    - model: the generation model (for the cost counterfactual).
    - created_at: unix seconds (optional).
    - generation_usage: {input_tokens, output_tokens, ...} for the session
      (what the generator actually spent) — drives the savings math.
    """
    t = {
        "session_id": session_id,
        "source": source,
        "model": model,
        "created_at": created_at,
        "turns": turns,
        "generation_usage": generation_usage,
    }
    return validate_transcript(t)


def validate_transcript(t):
    if not isinstance(t, dict):
        raise TranscriptError(f"transcript must be an object, got {type(t).__name__}")
    for field in ("session_id", "source"):
        if not isinstance(t.get(field), str) or not t[field].strip():
            raise TranscriptError(f"{field} must be a non-empty string")
    turns = t.get("turns")
    if not isinstance(turns, list) or not turns:
        raise TranscriptError("turns must be a non-empty list")
    for i, turn in enumerate(turns):
        if not isinstance(turn, dict):
            raise TranscriptError(f"turns[{i}] must be an object")
        if turn.get("role") not in ROLES:
            raise TranscriptError(f"turns[{i}].role must be one of {ROLES}, got {turn.get('role')!r}")
        if not isinstance(turn.get("text"), str):
            raise TranscriptError(f"turns[{i}].text must be a string")
    gu = t.get("generation_usage")
    if gu is not None and not isinstance(gu, dict):
        raise TranscriptError("generation_usage must be an object or null")
    return t


def first_user_ask(t) -> str:
    """The first real user turn — 'what was asked'. Skips leading tool/system
    turns; the adapter is responsible for having stripped instruction dumps."""
    for turn in t["turns"]:
        if turn["role"] == "user" and turn["text"].strip():
            return turn["text"].strip()
    # Fall back to the whole thing if there's no clean user turn.
    return render(t)


def render(t) -> str:
    """Flatten the transcript into text for the judge to read."""
    parts = []
    for turn in t["turns"]:
        text = turn["text"].strip()
        if text:
            parts.append(f"{turn['role'].upper()}:\n{text}")
    return "\n\n".join(parts)
