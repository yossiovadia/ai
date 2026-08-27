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
                    generation_usage=None, touched_files=None):
    """Build + validate a SessionTranscript.

    - session_id: stable id for the session (grouping key).
    - source: the adapter that produced it (provenance), e.g. "codex_rollout".
    - turns: ordered list of {role, text}.
    - model: the generation model (for the cost counterfactual).
    - created_at: unix seconds (optional).
    - generation_usage: {input_tokens, output_tokens, ...} for the session
      (what the generator actually spent) — drives the savings math.
    - touched_files: list of {path, count} — files the session edited, for the
      code-review artifact (diff). None/empty = no code artifact (narrative
      only); the judge falls back to transcript-review honestly.
    """
    t = {
        "session_id": session_id,
        "source": source,
        "model": model,
        "created_at": created_at,
        "turns": turns,
        "generation_usage": generation_usage,
        "touched_files": touched_files or [],
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
    tf = t.get("touched_files")
    if tf is not None:
        if not isinstance(tf, list):
            raise TranscriptError("touched_files must be a list or null")
        for i, f in enumerate(tf):
            if not isinstance(f, dict) or not f.get("path"):
                raise TranscriptError(f"touched_files[{i}] must be an object with a 'path'")
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


# Even a "sacred" human turn is capped against a pathological paste (e.g. pasting
# an entire prior model reply, or a giant log): ~40k chars ≈ 10k tokens, generous
# for real instructions/corrections but bounded so one paste can't eat the budget.
HUMAN_TURN_CHAR_CAP = 40_000


def _render_turn(role, text) -> str:
    return f"{role.upper()}:\n{text}"


def _cap(text, cap):
    """Head+tail cap with an elision marker. Returns (text, was_capped)."""
    if len(text) <= cap:
        return text, False
    head = cap * 3 // 4
    tail = cap - head
    return (text[:head] + f"\n... ({len(text) - cap:,} chars elided) ...\n" + text[-tail:], True)


def render_budgeted(t, char_budget):
    """Salience-tiered render that fits within `char_budget`, evicting by SALIENCE,
    not by AGE. This is what lets /escalate-all review a session whose full
    transcript overflows the judge's context window: it drops machine noise, never
    the human's intent.

    Tiers (per the design review):
      - Tier 0, sacred, never evicted: every human (user) turn verbatim (capped
        only against pathological pastes) + the FINAL assistant turn (the
        conclusion/claims the code diff is checked against). Recency-windowing
        would drop the real spec — which arrives in later human turns, not turn 1
        ("hello") — so age is the wrong axis; the human side is cheap and kept whole.
      - Tier 1, evidence: tool-result turns (failures, build output), newest first.
      - Tier 2, disposable: the rest of the assistant narration, newest first.

    Kept turns are re-emitted in ORIGINAL ORDER with in-band elision markers where
    runs were dropped, so the judge reads a chronological (if thinner) transcript
    and is TOLD, inside the packet, that lower-salience turns were removed.

    Returns (text, coverage). coverage carries what was kept/dropped per tier and
    `fits` (False if even the sacred tier alone exceeds the budget — the caller
    then falls to the diff-overflow rung rather than shipping an over-budget packet).
    """
    turns = t["turns"]
    n = len(turns)
    last_assistant_idx = next(
        (i for i in range(n - 1, -1, -1)
         if turns[i]["role"] == "assistant" and turns[i]["text"].strip()), None)

    # Classify each non-empty turn into a tier and precompute its rendered cost.
    items = []  # {idx, tier, text, cost, role, capped}
    for i, turn in enumerate(turns):
        text = (turn["text"] or "").strip()
        if not text:
            continue
        role = turn["role"]
        capped = False
        if role == "user":
            text, capped = _cap(text, HUMAN_TURN_CHAR_CAP)
            tier = 0
        elif role == "assistant":
            tier = 0 if i == last_assistant_idx else 2
        elif role == "tool":
            tier = 1
        else:  # system — lowest priority, only kept if nothing competes (it won't)
            tier = 3
        r = _render_turn(role, text)
        items.append({"idx": i, "tier": tier, "text": r, "cost": len(r), "role": role, "capped": capped})

    SEP = 2  # "\n\n" between parts
    keep, used = set(), 0
    coverage = {"total_turns": n, "human_capped": 0, "budget_chars": char_budget,
                "kept": {"human": 0, "assistant": 0, "tool": 0},
                "dropped": {"assistant": 0, "tool": 0, "system": 0}}

    # Tier 0 goes in unconditionally (that's what "sacred" means).
    tier0 = [it for it in items if it["tier"] == 0]
    for it in tier0:
        keep.add(it["idx"])
        used += it["cost"] + SEP
        if it["capped"]:
            coverage["human_capped"] += 1
    fits = used <= char_budget

    # Then fill remaining budget: evidence (Tier 1) before narration (Tier 2),
    # newest-first within each tier.
    for tier in (1, 2):
        for it in sorted((x for x in items if x["tier"] == tier), key=lambda x: -x["idx"]):
            if used + it["cost"] + SEP <= char_budget:
                keep.add(it["idx"])
                used += it["cost"] + SEP

    # Reassemble in original order, collapsing dropped runs into one marker each.
    parts, run = [], 0
    for it in items:
        if it["idx"] in keep:
            if run:
                parts.append(f"[... {run} lower-salience turn(s) elided (assistant narration / tool output) ...]")
                run = 0
            parts.append(it["text"])
            coverage["kept"]["human" if it["role"] == "user" else it["role"]] = \
                coverage["kept"].get("human" if it["role"] == "user" else it["role"], 0) + 1
        else:
            run += 1
            d = it["role"] if it["role"] in ("assistant", "tool") else "system"
            coverage["dropped"][d] = coverage["dropped"].get(d, 0) + 1
    if run:
        parts.append(f"[... {run} lower-salience turn(s) elided ...]")

    coverage["fits"] = fits
    coverage["kept_turns"] = len(keep)
    return "\n\n".join(parts), coverage


def _is_escalation_turn(turn) -> bool:
    """True if a turn is Side-Eye's OWN machinery — the /escalate-* skill body, the
    `sideeye advise/review` run, or its output. These surround the real exchange
    when advice mode runs, and without filtering them the packet captures its own
    invocation (empty, self-referential). Role-aware and anchored near the turn
    START so a genuine human question that QUOTES escalate output (e.g. "is this
    expected? <pasted output>") is NOT flagged — only the injected skill body is."""
    head = turn["text"].lstrip()
    role = turn["role"]
    if role == "user":
        # A human turn is machinery ONLY if it's the injected skill body; a human
        # pasting output into their own question must stay reviewable.
        return head.startswith("Base directory for this skill:") and "escalate" in head[:200]
    if role == "assistant":
        return head.startswith("[ran: sideeye ") or "sideeye · advice mode" in head[:80]
    if role == "tool":
        return head.startswith("[tool_result: sideeye")
    return False


def recent_exchanges(t, n: int = 1) -> str:
    """The last `n` user->assistant exchanges, rendered — the light packet for
    advice mode (a judgement call on recent turns, not a full session review, so
    no full transcript, no code diff). Side-Eye's own /escalate-* machinery is
    filtered out (everywhere, not just the tail) so advice reviews real exchanges
    even at n>1 — otherwise it captures its own invocation, or an older one. Renders
    user + assistant text only (tool output is dropped to keep the packet light).
    Falls back to the whole transcript if there's no clean turn, rather than
    sending an empty packet that would invite the judge to fabricate.
    """
    n = max(1, n)
    scope = [tn for tn in t["turns"] if not _is_escalation_turn(tn)]
    user_idxs = [i for i, tn in enumerate(scope)
                 if tn["role"] == "user" and tn["text"].strip()]
    if not user_idxs:
        return render(t)
    start = user_idxs[-n] if len(user_idxs) >= n else user_idxs[0]
    parts = []
    for tn in scope[start:]:
        text = tn["text"].strip()
        if not text or tn["role"] == "tool":     # keep it light — no tool dumps
            continue
        parts.append(f"{tn['role'].upper()}:\n{text}")
    return "\n\n".join(parts) if parts else render(t)


def last_exchange(t) -> str:
    """The last user->assistant exchange (recent_exchanges with n=1)."""
    return recent_exchanges(t, 1)
