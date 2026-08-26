#!/usr/bin/env python3
"""Detect the max context window of the GLM-5.2 model served by the internal
LiteLLM proxy, so Claude Code's auto-compact window can be set accurately
instead of defaulting to 200k for an "unrecognized model."

WHY THIS EXISTS
  Claude Code doesn't recognize `rits/zai-org/glm-5-2-fp8`, so /context shows
  "200k tokens (default for an unrecognized model)" and auto-compact fires at
  that guessed threshold — not GLM's real limit. This script finds the real
  number and tells you the exact env var to set:
      CLAUDE_CODE_AUTO_COMPACT_WINDOW=<N>

INDEPENDENT
  Needs only the Red Hat VPN + the GLM key at ~/.glm-52. Does NOT require the
  Side-Eye stack (praxis/metering/tunnel). It reaches the LiteLLM host
  directly through the corp HTTP proxy via CONNECT (TLS end-to-end).

STRATEGY
  1. Ask the LiteLLM proxy (authoritative + instant): GET /model/info and
     /v1/models. LiteLLM config often carries max_input_tokens / context_window
     per model. If found, print and exit.
  2. If metadata is silent, binary-search-probe POST /v1/messages with padded
     prompts of growing size, calibrating on the server's reported
     usage.input_tokens, until it rejects for context length. The largest
     accepted input_tokens is the effective context window.

SECRETS
  The GLM key is read from ~/.glm-52 (or $LITELLM_API_KEY) and sent as
  x-api-key. It is never printed or logged.

USAGE
  python3 detect_glm_context.py                  # auto: metadata, else probe
  python3 detect_glm_context.py --no-probe       # metadata only (fast, may give up)
  python3 detect_glm_context.py --probe-only     # skip metadata, force probe
  python3 detect_glm_context.py --json           # machine-readable output
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# --- Reachability ----------------------------------------------------------
TARGET_HOST = os.environ.get("GLM_TARGET_HOST", "ete-litellm.ai-models.vpc.res.ibm.com")
PROXY_HOST = os.environ.get("GLM_PROXY_HOST", "10.2.32.57")
PROXY_PORT = int(os.environ.get("GLM_PROXY_PORT", "3128"))
BASE_URL = f"https://{TARGET_HOST}"
MODEL = os.environ.get("GLM_MODEL", "rits/zai-org/glm-5-2-fp8")
KEY_FILE = Path(os.environ.get("GLM_KEY_FILE", str(Path.home() / ".glm-52")))

# Probe search bounds (tokens). GLM-5.x is documented at 128k; widen to be safe.
PROBE_LO = 4_000
PROBE_HI = 1_050_000   # above any plausible GLM window; bounded by the probe
PROBE_TOL = 2_000      # converge within this many tokens
PROBE_MAX_TOKENS = 8   # tiny output budget so input is the binding constraint


def die(msg: str, code: int = 1) -> "None":
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def get_key() -> str:
    key = os.environ.get("LITELLM_API_KEY")
    if key:
        return key.strip()
    if not KEY_FILE.exists():
        die(f"GLM key not found. Put it in {KEY_FILE} or set LITELLM_API_KEY.")
    return KEY_FILE.read_text().strip()


def make_opener():
    """urllib opener that tunnels HTTPS through the corp HTTP proxy via CONNECT."""
    proxy_url = f"http://{PROXY_HOST}:{PROXY_PORT}"
    handler = urllib.request.ProxyHandler({"https": proxy_url, "http": proxy_url})
    return urllib.request.build_opener(handler)


def http_request(opener, method: str, path: str, *, headers: dict, body: bytes | None = None,
                 timeout: float = 120) -> tuple[int, bytes, dict[str, str]]:
    """Return (status, body_bytes, headers). Raises urllib errors on network failure."""
    url = BASE_URL + path
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        resp = opener.open(req, timeout=timeout)
        return resp.status, resp.read(), {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as e:
        return e.code, e.read(), {k.lower(): v for k, v in e.headers.items()}


# --- Strategy 1: ask the proxy --------------------------------------------

def _hdr(key: str) -> dict[str, str]:
    return {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": "glm-ctx-detect/1.0",
    }


def _extract_context(obj: object) -> int | None:
    """Walk a LiteLLM /model/info or /v1/models payload looking for a context size."""
    candidates: list[int] = []
    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                kl = k.lower()
                if isinstance(v, int) and v > 1000 and (
                    "context" in kl
                    or "max_input" in kl
                    or "max_tokens" in kl
                    or "input_tokens" in kl
                    or "max_prompt" in kl
                ):
                    candidates.append(v)
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(obj)
    return max(candidates) if candidates else None


def detect_via_metadata(opener, key: str, *, want_json: bool) -> int | None:
    paths = ["/model/info", "/v1/models"]
    for path in paths:
        try:
            status, body, _ = http_request(opener, "GET", path, headers=_hdr(key), timeout=30)
        except Exception as e:
            if not want_json:
                print(f"  metadata: {path} unreachable ({e})")
            continue
        if status == 404:
            continue
        if status >= 400:
            if not want_json:
                print(f"  metadata: {path} -> HTTP {status}")
            continue
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            continue
        ctx = _extract_context(payload)
        if ctx:
            if not want_json:
                print(f"  metadata: {path} reports context window = {ctx:,} tokens")
            return ctx
        if not want_json:
            print(f"  metadata: {path} ok but no context field present")
    return None


# --- Strategy 2: binary-search probe --------------------------------------

def _probe_body(target_tokens: int, tokens_per_unit: float) -> bytes:
    """Build a /v1/messages body whose input is ~target_tokens."""
    # One unit ("x ") tokenizes to ~1 token; scale by the measured ratio.
    units = max(1, int(target_tokens / max(tokens_per_unit, 1e-6)))
    padding = "x " * units
    payload = {
        "model": MODEL,
        "max_tokens": PROBE_MAX_TOKENS,
        "messages": [{"role": "user", "content": f"{padding}\nReply with exactly: OK"}],
    }
    return json.dumps(payload).encode()


def _send_probe(opener, key: str, target_tokens: int, tokens_per_unit: float) -> tuple[bool, int | None, str]:
    """Send one probe. Return (accepted, observed_input_tokens, error_kind)."""
    body = _probe_body(target_tokens, tokens_per_unit)
    try:
        status, resp_body, _ = http_request(
            opener, "POST", "/v1/messages", headers=_hdr(key), body=body, timeout=180
        )
    except Exception as e:
        return False, None, f"network: {e}"
    if status == 200:
        try:
            usage = json.loads(resp_body).get("usage", {})
            inp = usage.get("input_tokens")
            if isinstance(inp, int):
                return True, inp, ""
        except json.JSONDecodeError:
            pass
        return True, None, ""
    # Non-200: classify. Context-too-long is the "too big" signal.
    text = resp_body.decode(errors="replace").lower()
    too_long = (
        "context" in text and ("length" in text or "window" in text or "exceed" in text or "limit" in text)
    ) or "prompt_too_long" in text or "too many tokens" in text or status in (413, 524)
    if too_long:
        return False, None, "context_length"
    return False, None, f"http_{status}: {text[:160]}"


def _calibrate(opener, key: str) -> float:
    """Measure tokens-per-'x ' unit so we can size padding to a target token count."""
    # Send a small known probe and read the server's input_tokens.
    ok, inp, _ = _send_probe(opener, key, 2_000, 1.0)
    if ok and isinstance(inp, int) and inp > 0:
        units = max(1, int(2_000 / 1.0))
        return inp / units
    # Fallback: assume ~1 token per "x " (chars/4 is pessimistic; "x " is ~1 token).
    return 1.0


def detect_via_probe(opener, key: str, *, want_json: bool) -> int:
    print(f"  probing /v1/messages (model={MODEL}); this sends several free requests...")
    tpu = _calibrate(opener, key)
    if not want_json:
        print(f"  calibrated: ~{tpu:.3f} tokens per padding unit")

    lo, hi = PROBE_LO, PROBE_HI
    best_accepted = 0       # largest input_tokens the server accepted
    best_target = 0         # largest target that was accepted
    # Track the smallest target the server rejected for context length.
    smallest_rejected = None

    while hi - lo > PROBE_TOL:
        mid = (lo + hi) // 2
        ok, inp, err = _send_probe(opener, key, mid, tpu)
        if ok:
            best_target = mid
            if isinstance(inp, int):
                best_accepted = max(best_accepted, inp)
            lo = mid + 1
            if not want_json:
                print(f"  {mid:>9,} tokens -> accepted (input_tokens={inp})")
        elif err == "context_length":
            smallest_rejected = mid if smallest_rejected is None else min(smallest_rejected, mid)
            hi = mid
            if not want_json:
                print(f"  {mid:>9,} tokens -> rejected (context length)")
        else:
            # Transient/other error: don't move the boundary, just retry once smaller.
            if not want_json:
                print(f"  {mid:>9,} tokens -> error ({err}); narrowing")
            hi = mid
        time.sleep(0.2)

    # The effective input context is the largest accepted input_tokens, or if we
    # never got a usage figure, the boundary midpoint.
    if best_accepted:
        result = best_accepted
    elif smallest_rejected is not None:
        result = smallest_rejected - PROBE_TOL
    else:
        result = (lo + hi) // 2
    return max(result, best_target)


# --- Main ------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-probe", action="store_true", help="metadata only; give up if silent")
    ap.add_argument("--probe-only", action="store_true", help="skip metadata, force probing")
    ap.add_argument("--json", action="store_true", help="emit JSON only")
    args = ap.parse_args()

    want_json = args.json
    key = get_key()
    opener = make_opener()

    if not want_json:
        print(f"GLM context-window detection")
        print(f"  target : {TARGET_HOST}")
        print(f"  model  : {MODEL}")
        print(f"  proxy  : {PROXY_HOST}:{PROXY_PORT}")

    ctx = None
    if not args.probe_only:
        if not want_json:
            print("\n[1/2] asking the LiteLLM proxy for model metadata...")
        ctx = detect_via_metadata(opener, key, want_json=want_json)

    if ctx is None and not args.no_probe:
        if not want_json:
            print("\n[2/2] metadata silent — binary-search probing the model...")
        try:
            ctx = detect_via_probe(opener, key, want_json=want_json)
        except KeyboardInterrupt:
            die("aborted by user.")

    if ctx is None:
        if want_json:
            print(json.dumps({"model": MODEL, "context_window": None, "found": False}))
        else:
            print("\nCould not determine the context window.")
        return 1

    if want_json:
        print(json.dumps({"model": MODEL, "context_window": ctx, "found": True}))
    else:
        print(f"\n=== detected context window: {ctx:,} tokens ===")
        print(f"\nSet Claude Code's auto-compact window to this value:")
        print(f"  export CLAUDE_CODE_AUTO_COMPACT_WINDOW={ctx}")
        print(f"\n(or put it in your shell profile to persist across sessions).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
