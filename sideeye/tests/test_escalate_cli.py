"""CLI-level tests for escalate (review mode): the cost ceiling must abort even
under --yes, and a judge failure must exit nonzero. No network — all boundaries
are monkeypatched."""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import sideeye.escalate as E  # noqa: E402
from sideeye.judge.transcript import make_transcript  # noqa: E402


def _wire(monkeypatch, tmp_path, *, est_cost, judge=None):
    """Stub every external boundary so main() runs offline against a fake session."""
    monkeypatch.setenv("SIDEEYE_JUDGE_BASE_URL", "https://api.anthropic.com")
    monkeypatch.setenv("SIDEEYE_JUDGE_API_KEY", "real-key")
    fake = tmp_path / "s.jsonl"
    fake.write_text("{}", encoding="utf-8")
    t = make_transcript(session_id="s", source="claude_code",
                        turns=[{"role": "user", "text": "do X"},
                               {"role": "assistant", "text": "did X"}])
    monkeypatch.setattr(E, "latest_session", lambda *a, **k: fake)
    monkeypatch.setattr(E, "load_transcript", lambda p: t)
    monkeypatch.setattr(E, "estimate_cost", lambda *a, **k: (500_000, est_cost, True, ""))
    if judge is not None:
        monkeypatch.setattr(E, "judge_session", judge)


def test_max_cost_ceiling_aborts_even_with_yes(monkeypatch, tmp_path):
    # Judge must NEVER be reached when the estimate blows the ceiling.
    def boom(*a, **k):
        raise AssertionError("judge_session called despite cost ceiling")
    _wire(monkeypatch, tmp_path, est_cost=99.0, judge=boom)
    monkeypatch.setattr(sys, "argv",
                        ["sideeye review", "--yes", "--max-cost", "5",
                         "--out", str(tmp_path / "out.jsonl")])
    with pytest.raises(SystemExit) as e:
        E.main()
    assert e.value.code != 0


def test_under_ceiling_reaches_judge(monkeypatch, tmp_path):
    called = {}
    def fake_judge(*a, **k):
        called["hit"] = True
        return ({"answered_what_was_asked": True, "correctness": "correct",
                 "claims_supported": True, "score": 5, "issues": [],
                 "overall_severity": "none", "summary": "ok"},
                {"judge_model": "claude-fable-5", "input_tokens": 10,
                 "output_tokens": 2, "cost_usd": 0.01, "retries": 0})
    _wire(monkeypatch, tmp_path, est_cost=0.5, judge=fake_judge)
    monkeypatch.setattr(sys, "argv",
                        ["sideeye review", "--yes", "--max-cost", "5",
                         "--out", str(tmp_path / "out.jsonl")])
    E.main()
    assert called.get("hit")


def test_judge_failure_exits_nonzero(monkeypatch, tmp_path):
    def failing_judge(*a, **k):
        raise RuntimeError("judge HTTP 500")
    _wire(monkeypatch, tmp_path, est_cost=0.5, judge=failing_judge)
    monkeypatch.setattr(sys, "argv",
                        ["sideeye review", "--yes", "--out", str(tmp_path / "out.jsonl")])
    with pytest.raises(SystemExit) as e:
        E.main()
    assert e.value.code != 0
    # The spend/failure is still recorded (documented), even though we exit nonzero.
    assert (tmp_path / "out.jsonl").exists()
