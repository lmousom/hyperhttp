"""
Tests for HTTP/1.1 response framing edge cases.

We run a raw TCP server so we can inject hand-crafted, sometimes malformed
responses (CL+TE conflict, multiple differing Content-Length headers, ...).
These must be rejected by the parser — otherwise we're vulnerable to request
smuggling.
"""

from __future__ import annotations

import asyncio
from typing import Tuple

import pytest

import hyperhttp
from hyperhttp.exceptions import RemoteProtocolError


async def _respond(writer: asyncio.StreamWriter, reader: asyncio.StreamReader, raw: bytes) -> None:
    # Drain the request line + headers so the client doesn't error on reset.
    try:
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=1.0)
            if line in (b"\r\n", b""):
                break
    except asyncio.TimeoutError:
        pass
    writer.write(raw)
    await writer.drain()


async def test_rejects_cl_te_conflict(raw_tcp_server: Tuple[str, asyncio.Queue]) -> None:
    url, queue = raw_tcp_server

    async def serve() -> None:
        reader, writer = await queue.get()
        bad = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: 5\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            b"0\r\n\r\n"
        )
        await _respond(writer, reader, bad)
        writer.close()

    task = asyncio.create_task(serve())
    try:
        async with hyperhttp.Client(http2=False) as c:
            with pytest.raises(RemoteProtocolError):
                r = await c.get(f"{url}/")
                await r.aread()
    finally:
        await task


async def test_rejects_multiple_differing_content_length(
    raw_tcp_server: Tuple[str, asyncio.Queue],
) -> None:
    url, queue = raw_tcp_server

    async def serve() -> None:
        reader, writer = await queue.get()
        bad = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: 5\r\n"
            b"Content-Length: 6\r\n"
            b"\r\n"
            b"hello"
        )
        await _respond(writer, reader, bad)
        writer.close()

    task = asyncio.create_task(serve())
    try:
        async with hyperhttp.Client(http2=False) as c:
            with pytest.raises(RemoteProtocolError):
                r = await c.get(f"{url}/")
                await r.aread()
    finally:
        await task


async def test_accepts_identical_duplicate_content_length(
    raw_tcp_server: Tuple[str, asyncio.Queue],
) -> None:
    """Same CL twice is legal (deduped)."""
    url, queue = raw_tcp_server

    async def serve() -> None:
        reader, writer = await queue.get()
        ok = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: 5\r\n"
            b"Content-Length: 5\r\n"
            b"\r\n"
            b"hello"
        )
        await _respond(writer, reader, ok)
        writer.close()

    task = asyncio.create_task(serve())
    try:
        async with hyperhttp.Client(http2=False) as c:
            r = await c.get(f"{url}/")
            assert await r.aread() == b"hello"
    finally:
        await task
