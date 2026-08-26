"""Unit tests for advise mode: last-exchange packet, free-form judge call,
judge-route env isolation. No network."""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from sideeye.judge import judge as J  # noqa: E402
from sideeye.judge.transcript import last_exchange, make_transcript  # noqa: E402


def _t(turns):
    return make_transcript(session_id="s", source="claude_code", turns=turns)


# --- last_exchange: the light packet -------------------------------------

def test_last_exchange_picks_last_user_and_assistant():
    t = _t([
        {"role": "user", "text": "first ask"},
        {"role": "assistant", "text": "first answer"},
        {"role": "user", "text": "SQLite or JSONL?"},
        {"role": "assistant", "text": "I'd use SQLite."},
    ])
    out = last_exchange(t)
    # Only the LAST exchange, not the whole transcript.
    assert "SQLite or JSONL?" in out and "I'd use SQLite." in out
    assert "first ask" not in out and "first answer" not in out


def test_last_exchange_user_only():
    t = _t([{"role": "user", "text": "just a question"}])
    out = last_exchange(t)
    assert "just a question" in out and "ASSISTANT" not in out


def test_last_exchange_falls_back_when_no_clean_turns():
    # tool-only tail still yields something (render fallback), never empty.
    t = _t([{"role": "user", "text": "q"}, {"role": "tool", "text": "toolout"}])
    assert last_exchange(t).strip()


# --- build_advice_body: free-form, no forced tool ------------------------

def test_build_advice_body_has_no_tools_and_carries_packet():
    body = J.build_advice_body("ADVICE-RUBRIC", "which one?", "USER:\nx\n\nASSISTANT:\ny")
    assert body["system"] == "ADVICE-RUBRIC"
    assert "tools" not in body and "tool_choice" not in body   # free-form, not a verdict
    user = body["messages"][0]["content"]
    assert "which one?" in user and "USER:" in user


def test_build_advice_body_omits_question_when_empty():
    body = J.build_advice_body("r", "", "the exchange")
    assert "the exchange" in body["messages"][0]["content"]
    assert "question I want your opinion" not in body["messages"][0]["content"]


# --- advise(): text + cost meta ------------------------------------------

def _text_response(text, usage=None):
    return {"content": [{"type": "thinking", "thinking": "hmm"},
                        {"type": "text", "text": text}],
            "usage": usage or {"input_tokens": 1200, "output_tokens": 90}}


def test_advise_returns_text_and_costed_meta(monkeypatch):
    monkeypatch.setattr(J, "call_judge", lambda *a, **k: _text_response("Recommendation: JSONL."))
    text, meta = J.advise("q", "packet", "rubric", base_url="http://x", api_key="k",
                          model="claude-fable-5")
    assert text == "Recommendation: JSONL."          # thinking block stripped
    assert meta["retries"] == 0
    # Fable = $10/M in, $50/M out: 1200*10/M + 90*50/M
    assert meta["cost_usd"] == pytest.approx(round(1200 * 10.0 / 1e6 + 90 * 50.0 / 1e6, 6))


def test_advise_raises_on_empty_text(monkeypatch):
    monkeypatch.setattr(J, "call_judge", lambda *a, **k: {"content": [], "usage": {}})
    with pytest.raises(ValueError):
        J.advise("q", "packet", "rubric", base_url="http://x", api_key="k")


def test_extract_text_skips_thinking_blocks():
    resp = {"content": [{"type": "thinking", "thinking": "secret"},
                        {"type": "text", "text": "visible"}]}
    assert J._extract_text(resp) == "visible"


# --- judge-route env isolation (the critical trap) -----------------------

def test_resolve_judge_route_prefers_sideeye_over_anthropic(monkeypatch):
    """Run from a Qwen session: ANTHROPIC_BASE_URL points at the cheap route,
    but SIDEEYE_JUDGE_BASE_URL must win so the review hits the real Claude route."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://ai-gateway-qwen-x.appdomain.cloud")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "qwen-key")
    monkeypatch.setenv("SIDEEYE_JUDGE_BASE_URL", "https://api.anthropic.com")
    monkeypatch.setenv("SIDEEYE_JUDGE_API_KEY", "real-key")
    base, key = J.resolve_judge_route()
    assert base == "https://api.anthropic.com" and key == "real-key"


def test_resolve_model_aliases():
    assert J.resolve_model("fable") == "claude-fable-5"
    assert J.resolve_model("OPUS") == "claude-opus-4-8"      # case-insensitive
    assert J.resolve_model("sonnet") == "claude-sonnet-5"
    assert J.resolve_model("claude-fable-5") == "claude-fable-5"   # full id passthrough
    assert J.resolve_model("some-other-model") == "some-other-model"


def test_resolve_judge_route_falls_back_to_anthropic(monkeypatch):
    monkeypatch.delenv("SIDEEYE_JUDGE_BASE_URL", raising=False)
    monkeypatch.delenv("SIDEEYE_JUDGE_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    base, key = J.resolve_judge_route()
    assert base == "https://api.anthropic.com" and key == "k"
