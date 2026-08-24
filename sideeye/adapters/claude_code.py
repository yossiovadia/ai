"""Adapter #1b: Claude Code session transcript -> SessionTranscript.

Claude Code writes one JSONL per session at
`~/.claude/projects/<mangled-cwd>/<session-uuid>.jsonl`. Records are typed
(`user`, `assistant`, plus `mode`/`file-history-snapshot`/`summary` we skip).
A user/assistant record carries `message` = {role, content}, where content is a
string or a list of blocks: {type:text}, {type:tool_use}, {type:tool_result},
{type:thinking}. Assistant messages carry `message.usage`.

Same design note as the Codex adapter: this is the client's view. Production
capture belongs in the gateway; this is the POC scaffold.
"""
from __future__ import annotations

import json
import pathlib

from sideeye.judge.transcript import make_transcript

# User-turn scaffolding injected by Claude Code (slash commands, caveats, local
# command output) — not real conversation, dropped from the judged transcript.
_NOISE_MARKERS = (
    "<local-command-", "<command-name>", "<command-message>",
    "<command-stdout>", "Caveat: The messages below",
)
_TOOL_RESULT_CAP = 2000  # keep tool output as evidence, but bounded


def _looks_like_noise(text: str) -> bool:
    head = text.lstrip()[:80]
    return any(m in head or m in text[:200] for m in _NOISE_MARKERS)


def _render_content(content) -> str:
    """Flatten a message's content into judge-readable text. Includes tool use
    and tool results (the evidence tier-1 judging cross-checks against); skips
    thinking (the rubric grades the served content, not the reasoning)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for b in content:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text":
            parts.append(b.get("text", ""))
        elif bt == "tool_use":
            parts.append(f"[tool_use: {b.get('name')}({json.dumps(b.get('input', {}))[:400]})]")
        elif bt == "tool_result":
            c = b.get("content")
            if isinstance(c, list):
                c = "".join(x.get("text", "") for x in c if isinstance(x, dict))
            txt = str(c)[:_TOOL_RESULT_CAP]
            parts.append(f"[tool_result: {txt}]")
        # thinking: intentionally skipped
    return "\n".join(p for p in parts if p)


def parse_session(path):
    path = pathlib.Path(path)
    session_id = path.stem
    turns = []
    out_tokens = 0
    last_in_tokens = 0
    model = None

    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") not in ("user", "assistant"):
                continue
            msg = obj.get("message", {}) or {}
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            text = _render_content(msg.get("content"))
            if not text.strip():
                continue
            if role == "user" and _looks_like_noise(text):
                continue
            turns.append({"role": role, "text": text})
            if role == "assistant":
                model = msg.get("model") or model
                usage = msg.get("usage") or {}
                out_tokens += usage.get("output_tokens", 0)
                last_in_tokens = usage.get("input_tokens", last_in_tokens) or last_in_tokens

    if not turns:
        return None
    usage = None
    if out_tokens or last_in_tokens:
        # input = peak context (last turn); output = summed across turns.
        # Labeled an estimate downstream; the metering DB is authoritative.
        usage = {"input_tokens": last_in_tokens, "output_tokens": out_tokens,
                 "total_tokens": last_in_tokens + out_tokens}
    return make_transcript(
        session_id=session_id, source="claude_code", turns=turns,
        model=model, generation_usage=usage,
    )
