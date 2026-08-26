"""Tests for session resolution — especially resolve_current_session's
robustness to a drifted shell cwd (the /tmp face-plant fix). No network."""
from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import sideeye.adapters as A  # noqa: E402


def test_resolve_current_prefers_cwd_project(monkeypatch, tmp_path):
    """When the shell IS in the session's project dir, use that session."""
    monkeypatch.setattr(A, "CLAUDE_DIR", tmp_path)
    cwd = "/Users/x/proj"
    monkeypatch.setattr(os, "getcwd", lambda: cwd)
    pdir = tmp_path / cwd.replace("/", "-")
    pdir.mkdir(parents=True)
    sess = pdir / "sess.jsonl"
    sess.write_text("{}", encoding="utf-8")
    got, fell_back = A.resolve_current_session("claude")
    assert got == sess and fell_back is False


def test_resolve_current_falls_back_when_cwd_has_no_session(monkeypatch, tmp_path):
    """The face-plant fix: shell drifted to /private/tmp (no project dir), so fall
    back to the most-recently-written session across all projects."""
    monkeypatch.setattr(A, "CLAUDE_DIR", tmp_path)
    monkeypatch.setattr(os, "getcwd", lambda: "/private/tmp")  # mangles to a dir that won't exist
    other = tmp_path / "-Users-x-otherproj"
    other.mkdir(parents=True)
    old = other / "old.jsonl"
    old.write_text("{}", encoding="utf-8")
    new = other / "new.jsonl"
    new.write_text("{}", encoding="utf-8")
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))   # newest by mtime == the active session
    got, fell_back = A.resolve_current_session("claude")
    assert got == new and fell_back is True


def test_resolve_current_none_when_no_sessions_anywhere(monkeypatch, tmp_path):
    monkeypatch.setattr(A, "CLAUDE_DIR", tmp_path)
    monkeypatch.setattr(os, "getcwd", lambda: "/private/tmp")
    got, fell_back = A.resolve_current_session("claude")
    assert got is None and fell_back is True
