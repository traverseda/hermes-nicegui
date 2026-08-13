#!/usr/bin/env python3
"""Reverse proxy exposing a multiplexed Hermes profile as its own port.

The hermes gateway's api_server is a single port-binding platform: it can
only listen on ONE port, owned by the default profile. With
`gateway.multiplex_profiles` enabled, named profiles are served by that same
listener under a URL prefix (e.g. the `ha` profile at `/p/ha/` on
http://127.0.0.1:8443). A secondary profile cannot bind its own api_server
port, so to give Home Assistant a plain OpenAI-compatible endpoint on a
dedicated port we reverse-proxy it:

    :8444  ──►  http://127.0.0.1:8443/p/ha/<path>

Every method, path, query, header and body is forwarded; the response body is
streamed chunk-by-chunk so SSE event streams (`/v1/.../events`, `chat/stream`)
are preserved in both directions.

Environment variables (all optional):
  HA_PROFILE_PROXY_BIND      listen address  (default 127.0.0.1)
  HA_PROFILE_PROXY_PORT      listen port     (default 8444)
  HA_PROFILE_PROXY_UPSTREAM  upstream base   (default http://127.0.0.1:8443)
  HA_PROFILE_PROXY_PREFIX    profile prefix  (default /p/ha)
"""

import os

import aiohttp
from aiohttp import web

BIND = os.getenv("HA_PROFILE_PROXY_BIND", "127.0.0.1")
PORT = int(os.getenv("HA_PROFILE_PROXY_PORT", "8444"))
UPSTREAM = os.getenv("HA_PROFILE_PROXY_UPSTREAM", "http://127.0.0.1:8443").rstrip("/")
PREFIX = os.getenv("HA_PROFILE_PROXY_PREFIX", "/p/ha").rstrip("/")

# Hop-by-hop headers must not be forwarded verbatim (aiohttp sets its own and
# Content-Length is recomputed because we stream).
_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


async def handler(request: web.Request) -> web.StreamResponse:
    target = f"{UPSTREAM}{PREFIX}{request.path}"
    if request.query_string:
        target = f"{target}?{request.query_string}"

    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS
    }

    async with aiohttp.ClientSession() as session:
        async with session.request(
            request.method,
            target,
            headers=headers,
            data=request.content,
            allow_redirects=False,
        ) as upstream:
            response = web.StreamResponse(
                status=upstream.status,
                headers={
                    k: v
                    for k, v in upstream.headers.items()
                    if k.lower() not in _HOP_HEADERS
                },
            )
            await response.prepare(request)
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
            await response.write_eof()
            return response


def main() -> None:
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    web.run_app(app, host=BIND, port=PORT)


if __name__ == "__main__":
    main()
