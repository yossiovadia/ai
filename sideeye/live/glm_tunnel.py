#!/usr/bin/env python3
"""Temporary VPN glue: a CONNECT tunnel exposing the internal GLM LiteLLM host
as a local TCP port, so praxis (which has no forward-proxy egress) can reach it.

GLM is only reachable through the Red Hat corp HTTP proxy over VPN. This listens
on 127.0.0.1:<local-port>, and for each connection opens a CONNECT tunnel through
the proxy to `ete-litellm.ai-models.vpc.res.ibm.com:443`, then splices bytes
raw. TLS passes through end-to-end: praxis (and curl) do the TLS handshake with
SNI = the real host and validate the real cert. No TLS is terminated here.

THIS IS TEMPORARY GLUE. Production capture is gateway-side; this exists only so
the laptop POC can reach an internal host over VPN. No key or payload is ever
read or logged here — it only moves bytes.

  python -m sideeye.live.glm_tunnel            # listen on 127.0.0.1:8443
  GLM_TUNNEL_PORT=9443 python -m sideeye.live.glm_tunnel
"""
from __future__ import annotations

import asyncio
import os

PROXY_HOST = os.environ.get("GLM_PROXY_HOST", "10.2.32.57")
PROXY_PORT = int(os.environ.get("GLM_PROXY_PORT", "3128"))
TARGET_HOST = os.environ.get("GLM_TARGET_HOST", "ete-litellm.ai-models.vpc.res.ibm.com")
TARGET_PORT = int(os.environ.get("GLM_TARGET_PORT", "443"))
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("GLM_TUNNEL_PORT", "8443"))


async def _pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def _handle(client_reader, client_writer):
    try:
        up_reader, up_writer = await asyncio.open_connection(PROXY_HOST, PROXY_PORT)
    except OSError as e:
        client_writer.close()
        print(f"  proxy connect failed: {e}")
        return

    # Establish the CONNECT tunnel through the corp proxy.
    connect = (
        f"CONNECT {TARGET_HOST}:{TARGET_PORT} HTTP/1.1\r\n"
        f"Host: {TARGET_HOST}:{TARGET_PORT}\r\n\r\n"
    )
    up_writer.write(connect.encode())
    await up_writer.drain()

    # Read the proxy's response headers (up to the blank line).
    header = b""
    while b"\r\n\r\n" not in header:
        chunk = await up_reader.read(1024)
        if not chunk:
            break
        header += chunk
    status_line = header.split(b"\r\n", 1)[0].decode(errors="replace")
    if " 200 " not in status_line:
        print(f"  CONNECT refused: {status_line}")
        up_writer.close()
        client_writer.close()
        return

    # Splice raw bytes both directions until either side closes.
    await asyncio.gather(
        _pipe(client_reader, up_writer),
        _pipe(up_reader, client_writer),
    )


async def main():
    server = await asyncio.start_server(_handle, LISTEN_HOST, LISTEN_PORT)
    print(f"GLM tunnel: {LISTEN_HOST}:{LISTEN_PORT} -> [CONNECT via "
          f"{PROXY_HOST}:{PROXY_PORT}] -> {TARGET_HOST}:{TARGET_PORT}")
    print("(temporary VPN glue; Ctrl-C to stop)")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
