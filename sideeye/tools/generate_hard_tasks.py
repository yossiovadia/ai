#!/usr/bin/env python3
"""Generate GLM-5.2 answers to genuinely hard tasks drawn from real field
evidence (defect classes where a strong reviewer caught a subtle bug). Unlike
the seed set, these have NO pre-known verdict: GLM may get each right or wrong.
The point is to judge them and compare the judge's verdict against reality.

Writes sideeye/data/hard_tasks.jsonl ({id, prompt, answer}). Needs VPN + proxy
+ ~/.glm-52. Flushes after each task so slow reasoning calls persist partial
progress. Run in the background — GLM-5.2 reasoning is slow (~30-60s/task).

  HTTPS_PROXY=http://10.2.32.57:3128 python sideeye/tools/generate_hard_tasks.py
"""
from __future__ import annotations

import json
import os
import pathlib

import requests

GLM_BASE = "https://ete-litellm.ai-models.vpc.res.ibm.com/v1"
GLM_MODEL = "rits/zai-org/glm-5-2-fp8"
KEY_FILE = pathlib.Path.home() / ".glm-52"
OUT = pathlib.Path(__file__).resolve().parent.parent / "data" / "hard_tasks.jsonl"

TASKS = [
    ("hard-01", "rust/security",
     "In Rust, write a function that builds a reqwest::Client and fetches a "
     "JWKS JSON document from an identity provider's HTTPS endpoint, to be used "
     "for verifying JWT signatures in production. Show the client construction "
     "and the fetch."),
    ("hard-02", "rust/correctness",
     "In Rust, using the `url` crate, write a function "
     "`is_loopback(u: &url::Url) -> bool` that returns true when the URL host is "
     "loopback: localhost, 127.0.0.1, or IPv6 ::1. This is used to allow-list "
     "local endpoints in a security check."),
    ("hard-03", "rust/testing",
     "Write a Rust unit test that verifies a library disables ANSI color output "
     "when stdout is NOT a terminal. Assume the library exposes "
     "`fn ansi_enabled() -> bool` which checks the terminal. Show the test."),
    ("hard-04", "rust/concurrency",
     "In Rust, write an async JWKS cache that fetches keys from a remote HTTPS "
     "endpoint and refreshes them on a cache miss (unknown key id), safe under "
     "high concurrency from many async tasks. Show the lookup + refresh logic "
     "and explain how it behaves when many tasks miss at once."),
]


def ask(prompt, key, proxies):
    r = requests.post(
        GLM_BASE + "/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": GLM_MODEL, "messages": [{"role": "user", "content": prompt}],
              "max_tokens": 3000, "temperature": 0},
        proxies=proxies, timeout=240,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"].get("content")


def main():
    key = KEY_FILE.read_text().strip()
    proxy = os.environ.get("HTTPS_PROXY") or "http://10.2.32.57:3128"
    proxies = {"https": proxy, "http": proxy}
    OUT.parent.mkdir(exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        for pid, topic, prompt in TASKS:
            ans = ask(prompt, key, proxies)
            if not ans or not ans.strip():
                print(f"  {pid}: EMPTY (reasoning consumed budget)", flush=True)
                continue
            f.write(json.dumps({"id": pid, "prompt": prompt, "answer": ans}) + "\n")
            f.flush()
            print(f"  {pid} ({topic}): {len(ans)} chars", flush=True)
    print(f"wrote {OUT}", flush=True)


if __name__ == "__main__":
    main()
