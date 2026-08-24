"""Unit tests for the Side-Eye judge module: rubric loading, verdict schema
validation, and structured-output (tool_use) parsing. No network."""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from sideeye.judge import judge as J  # noqa: E402
from sideeye.judge.schema import (  # noqa: E402
    VerdictError,
    is_flagged,
    validate_verdict,
)


def _good_verdict(**overrides):
    v = {
        "answered_what_was_asked": True,
        "correctness": "correct",
        "claims_supported": True,
        "score": 5,
        "issues": [],
        "overall_severity": "none",
        "summary": "Clean and correct.",
    }
    v.update(overrides)
    return v


# --- rubric loading -------------------------------------------------------

def test_load_rubric_and_version(tmp_path):
    p = tmp_path / "rubric_v1.md"
    p.write_text("grade it", encoding="utf-8")
    assert J.load_rubric(p) == "grade it"
    assert J.rubric_version(p) == "rubric_v1"


# --- verdict schema validation --------------------------------------------

def test_valid_verdict_passes():
    assert validate_verdict(_good_verdict()) is not None


def test_valid_verdict_with_issues():
    v = _good_verdict(
        correctness="incorrect", score=2, overall_severity="major",
        issues=[{"description": "wrong API", "severity": "major"}],
    )
    assert validate_verdict(v)


@pytest.mark.parametrize("bad", [
    _good_verdict(score=0),
    _good_verdict(score=6),
    _good_verdict(score=True),          # bool is not a valid int score
    _good_verdict(score="5"),
])
def test_bad_score_rejected(bad):
    with pytest.raises(VerdictError):
        validate_verdict(bad)


def test_missing_field_rejected():
    v = _good_verdict()
    del v["summary"]
    with pytest.raises(VerdictError):
        validate_verdict(v)


def test_bad_correctness_rejected():
    with pytest.raises(VerdictError):
        validate_verdict(_good_verdict(correctness="mostly"))


def test_bad_severity_rejected():
    with pytest.raises(VerdictError):
        validate_verdict(_good_verdict(overall_severity="catastrophic"))


def test_non_bool_claims_rejected():
    with pytest.raises(VerdictError):
        validate_verdict(_good_verdict(claims_supported="yes"))


def test_malformed_issue_rejected():
    with pytest.raises(VerdictError):
        validate_verdict(_good_verdict(issues=[{"description": "x"}]))  # no severity
    with pytest.raises(VerdictError):
        validate_verdict(_good_verdict(issues=[{"description": "", "severity": "minor"}]))


def test_empty_summary_rejected():
    with pytest.raises(VerdictError):
        validate_verdict(_good_verdict(summary="   "))


# --- structured-output parsing --------------------------------------------

def _response_with_tool(input_dict):
    return {
        "content": [
            {"type": "text", "text": "thinking..."},
            {"type": "tool_use", "name": "record_verdict", "input": input_dict},
        ],
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }


def test_parse_verdict_extracts_and_validates():
    resp = _response_with_tool(_good_verdict())
    v = J.parse_verdict(resp)
    assert v["score"] == 5


def test_parse_verdict_no_tool_use_raises():
    resp = {"content": [{"type": "text", "text": "no tool call here"}]}
    with pytest.raises(ValueError):
        J.parse_verdict(resp)


def test_parse_verdict_invalid_input_raises():
    resp = _response_with_tool(_good_verdict(score=99))
    with pytest.raises(VerdictError):
        J.parse_verdict(resp)


# --- request building -----------------------------------------------------

def test_build_request_body_forces_tool_and_carries_pair():
    body = J.build_request_body("RUBRIC-TEXT", "the question", "the answer")
    assert body["system"] == "RUBRIC-TEXT"
    assert body["tool_choice"] == {"type": "tool", "name": "record_verdict"}
    assert body["tools"][0]["name"] == "record_verdict"
    user = body["messages"][0]["content"]
    assert "the question" in user and "the answer" in user


# --- cost + flagging ------------------------------------------------------

def test_cost_uses_model_pricing():
    # Sonnet 5 = $2/M in, $10/M out.
    c = J.cost_usd("claude-sonnet-5", {"input_tokens": 1_000_000, "output_tokens": 1_000_000})
    assert c == pytest.approx(12.0)


def test_is_flagged_thresholds():
    assert not is_flagged(_good_verdict())
    assert is_flagged(_good_verdict(score=2))
    assert is_flagged(_good_verdict(overall_severity="major"))
    assert is_flagged(_good_verdict(claims_supported=False))
    assert not is_flagged(_good_verdict(overall_severity="minor",
                                        issues=[{"description": "nit", "severity": "minor"}]))
