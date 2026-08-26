"""Unit tests for the capture-agnostic SessionTranscript schema."""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from sideeye.judge.transcript import (  # noqa: E402
    TranscriptError,
    first_user_ask,
    make_transcript,
    render,
    validate_transcript,
)


def _t(**over):
    base = dict(
        session_id="s1", source="codex_rollout",
        turns=[{"role": "user", "text": "do X"}, {"role": "assistant", "text": "did X"}],
        model="glm", generation_usage={"input_tokens": 10, "output_tokens": 2},
    )
    base.update(over)
    return make_transcript(**base)


def test_make_transcript_valid():
    t = _t()
    assert t["session_id"] == "s1" and t["source"] == "codex_rollout"


def test_missing_session_id_rejected():
    with pytest.raises(TranscriptError):
        validate_transcript({"source": "x", "turns": [{"role": "user", "text": "hi"}]})


def test_empty_turns_rejected():
    with pytest.raises(TranscriptError):
        make_transcript(session_id="s", source="x", turns=[])


def test_bad_role_rejected():
    with pytest.raises(TranscriptError):
        make_transcript(session_id="s", source="x", turns=[{"role": "boss", "text": "hi"}])


def test_generation_usage_must_be_dict_or_none():
    with pytest.raises(TranscriptError):
        make_transcript(session_id="s", source="x",
                        turns=[{"role": "user", "text": "hi"}], generation_usage=5)


def test_first_user_ask():
    t = _t(turns=[{"role": "assistant", "text": "hello"},
                  {"role": "user", "text": "the real ask"},
                  {"role": "assistant", "text": "ok"}])
    assert first_user_ask(t) == "the real ask"


def test_render_includes_turns():
    r = render(_t())
    assert "USER:" in r and "do X" in r and "ASSISTANT:" in r and "did X" in r


def test_touched_files_default_empty():
    t = _t()
    assert t["touched_files"] == []


def test_touched_files_validated():
    with pytest.raises(TranscriptError):
        make_transcript(session_id="s", source="x",
                        turns=[{"role": "user", "text": "hi"}],
                        touched_files=[{"no_path": True}])
    # a valid touched_files list round-trips
    t = _t(touched_files=[{"path": "/x.py", "count": 2}])
    assert t["touched_files"] == [{"path": "/x.py", "count": 2}]
