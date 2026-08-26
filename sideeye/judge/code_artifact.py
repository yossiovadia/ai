"""Build the code-review artifact for a judged session.

Replaces the adapter's 400-char tool_use truncation, which blinded the judge to
the code: a 50-line function was rendered as its first 400 characters, so the
judge reviewed the *conversation about the code*, not the code. A confident
narrative-only score is the result — the judge fills missing code with the most
plausible interpolation and reviews its own hallucination, biased toward "looks
fine" because plausible completions are, by definition, plausible. That is a
structural failure (the artifact), not a prompt-side one ("be careful" doesn't
work).

The artifact is ONE view carrying two signals the judge needs:

  - Comprehension: final-state code in situ — imports, types, the helpers a
    changed line calls. A thin diff leaves the judge guessing at referents; the
    full file with markers lets it reason about real code.
  - Attribution: which lines the session wrote. Hand the judge a full file with
    no markers and it grades pre-existing code — crediting the cheap model for a
    nice abstraction someone wrote last year, or dinging it for a latent bug it
    never touched. That corrupts the scoreboard, which is the whole point of
    Side-Eye.

`git diff -U999999` gives both at once: the whole final file with +/- markers.
New files get full content (for a new file, final content IS the diff — the
all-green view is correct, not degenerate). Big files degrade to standard hunks
with a `view: partial` manifest flag; the rubric marks partial-view correctness
as "not assessable" rather than letting the judge guess. Generated/lockfiles get
manifest-only (one line — no point spending tokens reviewing Cargo.lock).

A manifest is always emitted, even when every file is full-view: the judge must
know its view is partial when it is (silent truncation reads as full coverage to
an LLM), and per-file edit count is free thrash signal a file rewritten five
times in a session is thrash the final-state view otherwise erases.
"""
from __future__ import annotations

import pathlib
import subprocess

# Per-file degradation thresholds. A file whose full diff exceeds this many
# lines drops to standard hunks (-U20) + a view:partial flag — the judge is told
# its view is partial and the rubric marks correctness "not assessable" rather
# than guessed. Tunable; ~400 lines / ~5k tokens per Fable's budget ladder.
FULL_VIEW_LINE_CAP = 500

# Standard context when degrading a big file to hunks.
PARTIAL_CONTEXT_LINES = 20

# Paths that match these patterns get manifest-only — generated/vendored code
# the judge should not spend tokens reviewing. Matched against the basename.
_GENERATED_BASENAMES = {
    "Cargo.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "go.sum", "poetry.lock", "Gemfile.lock", "composer.lock", "Pipfile.lock",
}
_GENERATED_SUFFIXES = (".min.js", ".min.css", ".generated.go", ".generated.py")


def _is_generated(path: pathlib.Path) -> bool:
    name = path.name
    if name in _GENERATED_BASENAMES:
        return True
    return any(name.endswith(suf) for suf in _GENERATED_SUFFIXES)


def _git(repo_root, *args):
    """Run a git command in repo_root. Returns (stdout, returncode). Uses an
    argument list (never shell=True) — file paths come from session transcripts
    and must not reach a shell."""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        return "", 1
    return r.stdout, r.returncode


def _is_tracked(repo_root, path: pathlib.Path) -> bool:
    _, rc = _git(repo_root, "ls-files", "--error-unmatch", str(path))
    return rc == 0


def _line_count(text: str) -> int:
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def _render_file(repo_root, path, diff_base) -> dict:
    """Build the artifact entry for one touched file. Returns a descriptor:
    {path, view, text, edit_count_note}. Never raises — a git/filesystem failure
    degrades to a manifest note, not a crash (a missing file shouldn't sink the
    whole judgment)."""
    p = pathlib.Path(path)
    rel = _rel_path(repo_root, p)

    if not p.exists():
        return {"path": rel, "view": "deleted", "lines": 0, "edits": 0,
                "text": f"(file no longer exists: {rel})"}

    if _is_generated(p):
        return {"path": rel, "view": "manifest-only", "lines": _line_count(p.read_text(errors="replace")),
                "edits": 0, "text": f"(excluded: generated/vendored — {rel})"}

    tracked = _is_tracked(repo_root, p)

    if not tracked:
        # New file: final content IS the diff (all additions). Show the full
        # file — the all-green view is correct here, not degenerate.
        content = p.read_text(errors="replace")
        view = "full" if _line_count(content) <= FULL_VIEW_LINE_CAP else "partial"
        if view == "partial":
            # A new big file: show head + tail with an elision marker rather
            # than dumping the whole thing; flag partial so the rubric knows.
            text = _head_tail(content)
        else:
            text = f"+++ {rel} (new file)\n" + content
        return {"path": rel, "view": view, "lines": _line_count(content), "edits": 0, "text": text}

    # Tracked file: diff against the base (default HEAD = working-tree changes).
    diff, _ = _git(repo_root, "diff", f"-U999999", diff_base, "--", str(p))
    if diff.strip():
        view = "full" if _line_count(diff) <= FULL_VIEW_LINE_CAP else "partial"
        if view == "partial":
            # Big diff: degrade to standard hunks with bounded context.
            diff, _ = _git(repo_root, "diff", f"-U{PARTIAL_CONTEXT_LINES}", diff_base, "--", str(p))
        return {"path": rel, "view": view, "lines": _line_count(diff), "edits": 0, "text": diff}

    # Tracked but no working-tree diff: the session's changes are committed
    # (in HEAD). Show the committed final content so the judge at least sees the
    # code, with an honest manifest note that attribution markers aren't
    # available (this is the stage-1 diff-base limitation; stage 2 captures the
    # session-start commit so committed work diffs correctly).
    content = p.read_text(errors="replace")
    view = "full" if _line_count(content) <= FULL_VIEW_LINE_CAP else "partial"
    text = content if view == "full" else _head_tail(content)
    return {"path": rel, "view": f"{view}-no-markers", "lines": _line_count(content),
            "edits": 0, "text": f"(no working-tree diff; committed state — {rel})\n" + text}


def _head_tail(content: str, head: int = 200, tail: int = 200) -> str:
    """First-N + last-M lines with an elision marker. Failures print mid-run in
    most test frameworks, the summary prints last, the command first — so head
    + tail covers all three."""
    lines = content.splitlines()
    if len(lines) <= head + tail:
        return content
    return ("\n".join(lines[:head])
            + f"\n... ({len(lines) - head - tail} lines elided) ...\n"
            + "\n".join(lines[-tail:]))


def _rel_path(repo_root, path) -> str:
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def build_code_artifact(touched_files, repo_root, diff_base="HEAD"):
    """Build the code-review artifact for a session.

    touched_files: list of {path, count} (path may be absolute or repo-relative;
                      the adapter extracts these from the transcript's Edit/Write
                      tool_use blocks).
    repo_root: the git toplevel (where `git` commands run).
    diff_base: the ref to diff against (default HEAD = working-tree changes;
               the stage-1 limitation is that committed work shows as no-diff).

    Returns the artifact text (manifest + per-file diffs), ready to append to
    the judge's `produced` payload. Returns "" if no touched files — the judge
    then falls back to narrative-only, honestly (the manifest records this).
    """
    if not touched_files:
        return ""

    repo_root = pathlib.Path(repo_root).resolve()
    entries = []
    for tf in touched_files:
        path = tf["path"]
        p = pathlib.Path(path)
        if not p.is_absolute():
            p = repo_root / path
        entry = _render_file(repo_root, p, diff_base)
        entry["edits"] = tf.get("count", 0)
        entries.append(entry)

    lines = ["## Code changes (adapter v1-sighted)",
             "",
             "# manifest: path, lines, edits, view"]
    for e in entries:
        lines.append(f"#   {e['path']}  {e['lines']} lines  {e['edits']} edits  view: {e['view']}")
    lines.append("")
    for e in entries:
        lines.append(f"--- {e['path']} (view: {e['view']}) ---")
        lines.append(e["text"])
        lines.append("")
    return "\n".join(lines)
