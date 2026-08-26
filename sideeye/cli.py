#!/usr/bin/env python3
"""`sideeye` CLI dispatcher — the single console entry point.

    sideeye review [opts]   full-session review (sighted: transcript + code diff)
    sideeye advise [opts]   quick second opinion on the last exchange

Both subcommands are thin: they delegate to the existing engine modules
(sideeye.escalate / sideeye.advise). This exists only so users get a real
`sideeye` command instead of `python -m sideeye.escalate`.
"""
from __future__ import annotations

import sys

USAGE = """sideeye — on-demand session review (draft cheap, review expensive)

  sideeye review [opts]   full-session review (sighted: transcript + code diff)
  sideeye advise [opts]   quick second opinion on the last exchange

Run `sideeye review -h` or `sideeye advise -h` for options.
"""


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return
    cmd, rest = argv[0], argv[1:]
    # Re-shape argv so the delegated module's argparse sees only its own args,
    # with a sensible prog name in help/usage.
    sys.argv = [f"sideeye {cmd}", *rest]
    if cmd == "review":
        from sideeye.escalate import main as run
    elif cmd == "advise":
        from sideeye.advise import main as run
    else:
        print(f"unknown command: {cmd!r}\n\n{USAGE}", file=sys.stderr)
        sys.exit(2)
    run()


if __name__ == "__main__":
    main()
