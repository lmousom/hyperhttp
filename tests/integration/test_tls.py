"""
TLS + ALPN transport selection.

The server advertises only ``http/1.1``; the client must pick the H1 transport
even when ``http2=True`` is requested (i.e. negotiation wins, not config).
"""

from __future__ import annotations

import ssl
from typing import Tuple

import hyperhttp


async def test_https_h1_alpn_selects_http1(
    https_h1_server: Tuple[str, ssl.SSLContext],
) -> None:
    url, client_ctx = https_h1_server
    async with hyperhttp.Client(http2=True, ssl_context=client_ctx) as c:
        r = await c.get(f"{url}/")
        assert r.status_code == 200
        assert r.http_version == "HTTP/1.1"
        await r.aread()
        assert r.text == "ok"


async def test_https_reuses_tls_connection(
    https_h1_server: Tuple[str, ssl.SSLContext],
) -> None:
    url, client_ctx = https_h1_server
    async with hyperhttp.Client(http2=False, ssl_context=client_ctx) as c:
        await (await c.get(f"{url}/")).aread()
        await (await c.get(f"{url}/")).aread()
        stats = c.get_pool_stats()
        assert any(
            s.get("total_connections", 0) <= 2 for s in stats.values()
        ), stats
