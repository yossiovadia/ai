"""Adapter #1b: Claude Code session transcript -> SessionTranscript.

Claude Code writes one JSONL per session at
`~/.claude/projects/<mangled-cwd>/<session-uuid>.jsonl`. Records are typed
(`user`, `assistant`, plus `mode`/`file-history-snapshot`/`summary` we skip).
A user/assistant record carries `message` = {role, content}, where content is a
string or a list of blocks: {type:text}, {type:tool_use}, {type:tool_result},
{type:thinking}. Assistant messages carry `message.usage`.

Same design note as the Codex adapter: this is the client's view. Production
capture belongs in the gateway; this is the POC scaffold.

Code-review artifact: this adapter extracts the set of files the session
touched (Edit/Write tool_use `file_path`s) so the judge can be given a real
git diff of the code (see sideeye.judge.code_artifact). The tool_use blocks
themselves are rendered as *references* (`[edited <path>]`), not as truncated
code — the 400-char cap that used to blind the judge to the code is gone; the
actual code lives in the diff artifact, once, not double-spent in both places.
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

# Evidence (test/build/command output) is kept head-N + tail-M with an elision
# marker: failures print mid-run, the summary prints last, the command first,
# so head+tail covers all three. Replaces the old head-only 2000-char cap,
# which cut the summary line off a long test run.
_EVIDENCE_HEAD = 1500
_EVIDENCE_TAIL = 500

# Claude Code tools that write files — their `file_path` is collected for the
# code-review artifact. Bash/Read/etc. are not file edits.
_FILE_EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def _looks_like_noise(text: str) -> bool:
    head = text.lstrip()[:80]
    return any(m in head or m in text[:200] for m in _NOISE_MARKERS)


def _head_tail(text: str, head: int, tail: int) -> str:
    if len(text) <= head + tail:
        return text
    return text[:head] + f"\n... ({len(text) - head - tail} chars elided) ...\n" + text[-tail:]


def _render_content(content, touched=None) -> str:
    """Flatten a message's content into judge-readable text. Tool use is
    rendered as a reference (the code lives in the diff artifact); tool results
    (evidence) are kept head+tail; thinking is skipped (the rubric grades served
    content, not reasoning). If `touched` (a dict) is given, file paths from
    Edit/Write tool_use are collected into it for the code artifact builder."""
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
            name = b.get("name")
            inp = b.get("input", {}) or {}
            # Collect file paths for the code-review artifact.
            if name in _FILE_EDIT_TOOLS and touched is not None:
                fp = inp.get("file_path") or inp.get("notebook_path")
                if fp:
                    touched[fp] = touched.get(fp, 0) + 1
            # Render as a reference, not the code. The actual code lives in the
            # diff artifact — rendering it here too would double-spend tokens
            # and hand the judge two views of the same code to correlate.
            if name in _FILE_EDIT_TOOLS:
                fp = inp.get("file_path") or inp.get("notebook_path") or "?"
                parts.append(f"[{name}: {fp}]")
            elif name == "Bash":
                cmd = str(inp.get("command", ""))[:120]
                parts.append(f"[ran: {cmd}]")
            else:
                parts.append(f"[tool_use: {name}]")
        elif bt == "tool_result":
            c = b.get("content")
            if isinstance(c, list):
                c = "".join(x.get("text", "") for x in c if isinstance(x, dict))
            txt = _head_tail(str(c), _EVIDENCE_HEAD, _EVIDENCE_TAIL)
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
    touched = {}  # {file_path: edit_count} — for the code-review artifact

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
            text = _render_content(msg.get("content"), touched)
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
        touched_files=[{"path": p, "count": c} for p, c in sorted(touched.items())],
    )
