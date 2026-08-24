"""Capture adapters — each turns some capture source into a SessionTranscript.

Adapters (this POC, both client-side scaffolds):
  - codex_rollout  : the Codex CLI's session rollout logs
  - claude_code    : the Claude Code session transcripts
Production adapter #2 would be a praxis gateway sampling-tap emitting events.
All produce the same SessionTranscript, so everything downstream is
capture-agnostic.
"""
from __future__ import annotations

import json
import pathlib

from sideeye.adapters import claude_code, codex_rollout

CLAUDE_DIR = pathlib.Path.home() / ".claude" / "projects"
CODEX_DIR = pathlib.Path.home() / ".codex" / "sessions"


def load_transcript(path):
    """Auto-detect the capture source from the file and parse it."""
    path = pathlib.Path(path)
    with open(path, encoding="utf-8") as f:
        for _ in range(10):
            line = f.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = obj.get("type")
            if t in ("session_meta", "turn_context", "response_item"):
                return codex_rollout.parse_rollout(path)
            if t in ("user", "assistant") and "message" in obj:
                return claude_code.parse_session(path)
    # Fallback: try both.
    return codex_rollout.parse_rollout(path) or claude_code.parse_session(path)


def session_files(client):
    """List candidate session files for a client, oldest -> newest by mtime."""
    if client == "codex":
        files = CODEX_DIR.rglob("rollout-*.jsonl")
    elif client == "claude":
        # Top-level session files only (skip nested subagents/ transcripts).
        files = (p for p in CLAUDE_DIR.glob("*/*.jsonl"))
    else:
        raise ValueError(f"unknown client: {client!r} (use 'claude' or 'codex')")
    return sorted(files, key=lambda p: p.stat().st_mtime)


def latest_session(client):
    files = session_files(client)
    return files[-1] if files else None
