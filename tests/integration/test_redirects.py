"""
307/308 must preserve method and body (RFC 7231 §6.4.7 / RFC 7538 §3).
301/302/303 become GET per widespread convention.
"""

from __future__ import annotations

import json

import hyperhttp


async def test_307_preserves_method_and_body(http_server: str) -> None:
    async with hyperhttp.Client(http2=False, follow_redirects=True) as c:
        r = await c.post(
            f"{http_server}/redirect/307",
            content=b"hello",
            headers={"Content-Type": "text/plain"},
        )
        assert r.status_code == 200
        await r.aread()
        echoed = json.loads(r.text)
        assert echoed["method"] == "POST"
        assert echoed["body"] == "hello"


async def test_308_preserves_method_and_body(http_server: str) -> None:
    async with hyperhttp.Client(http2=False, follow_redirects=True) as c:
        r = await c.put(f"{http_server}/redirect/308", content=b"world")
        assert r.status_code == 200
        await r.aread()
        echoed = json.loads(r.text)
        assert echoed["method"] == "PUT"
        assert echoed["body"] == "world"


async def test_303_converts_to_get(http_server: str) -> None:
    async with hyperhttp.Client(http2=False, follow_redirects=True) as c:
        r = await c.post(f"{http_server}/redirect/303", content=b"discarded")
        assert r.status_code == 200
        await r.aread()
        echoed = json.loads(r.text)
        assert echoed["method"] == "GET"
        assert echoed["body"] == ""
