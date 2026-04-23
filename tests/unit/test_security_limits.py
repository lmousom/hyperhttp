"""
Memory-bomb protections:

* ``max_response_size`` caps raw (still-encoded) body bytes.
* ``max_decompressed_size`` caps decoded body bytes, preventing a compressed
  response of 1 KiB that inflates to 10 GB.

Exercised through both the streaming (``aiter_bytes``, ``aiter_raw``) and the
materialising (``aread``) paths so a caller cannot route around the cap.
"""

from __future__ import annotations

import gzip

import pytest

import hyperhttp
from hyperhttp._compression import GzipDecoder, IdentityDecoder, make_decoder
from hyperhttp.exceptions import DecompressionError, ResponseTooLarge


# ---------------------------------------------------------------------------
# Decoder-level unit tests
# ---------------------------------------------------------------------------


def test_identity_decoder_no_cap_is_pass_through() -> None:
    d = IdentityDecoder()
    assert d.decompress(b"abc") == b"abc"
    assert d.flush() == b""


def test_identity_decoder_cap_blocks_over_limit() -> None:
    d = IdentityDecoder(max_output_size=4)
    assert d.decompress(b"abcd") == b"abcd"
    with pytest.raises(DecompressionError):
        d.decompress(b"x")


def test_gzip_decoder_cap_blocks_inflated_output() -> None:
    # 1 MiB of zeros compresses to ~1 KiB — a mild "bomb".
    payload = gzip.compress(b"\0" * (1 << 20))
    assert len(payload) < 2048
    d = GzipDecoder(max_output_size=4096)
    with pytest.raises(DecompressionError):
        d.decompress(payload)


def test_gzip_decoder_below_cap_passes() -> None:
    payload = gzip.compress(b"hello" * 100)
    d = GzipDecoder(max_output_size=10_000)
    out = d.decompress(payload) + d.flush()
    assert out == b"hello" * 100


def test_make_decoder_passes_through_cap() -> None:
    d = make_decoder("gzip", max_output_size=256)
    with pytest.raises(DecompressionError):
        d.decompress(gzip.compress(b"\0" * 10_000))


def test_make_decoder_identity_no_encoding() -> None:
    assert isinstance(make_decoder(None), IdentityDecoder)
    assert isinstance(make_decoder("identity"), IdentityDecoder)


# ---------------------------------------------------------------------------
# Client-level: bombs against Response
# ---------------------------------------------------------------------------


async def test_response_aread_rejects_oversize_content_length() -> None:
    """A server advertising CL > cap must be rejected before reading the body."""
    big = b"A" * 8192

    async def handler(request):
        # Use ``content=`` so MockResponse sets a Content-Length header equal
        # to the body size; this hits the CL fast-path in ``aread``.
        return hyperhttp.MockResponse(200, content=big)

    async with hyperhttp.Client(
        transport=hyperhttp.MockTransport(handler),
        max_response_size=1024,
    ) as c:
        r = await c.get("https://x/y")
        with pytest.raises(ResponseTooLarge):
            await r.aread()


async def test_response_aiter_bytes_rejects_oversize_stream() -> None:
    """When the body arrives piecewise, the cap is enforced mid-stream."""

    async def streamer():
        yield b"A" * 600
        yield b"B" * 600  # crosses 1024

    async def handler(request):
        return hyperhttp.MockResponse(200, stream=streamer())

    async with hyperhttp.Client(
        transport=hyperhttp.MockTransport(handler),
        max_response_size=1024,
    ) as c:
        r = await c.get("https://x/y")
        collected = 0
        with pytest.raises(ResponseTooLarge):
            async for chunk in r.aiter_bytes():
                collected += len(chunk)
        # We yielded the first 600-byte chunk before the cap tripped.
        assert 0 < collected <= 1024


async def test_gzip_bomb_is_caught_before_oom() -> None:
    """The classic 1 KiB → 10 MiB gzip expansion must be short-circuited."""
    bomb_plain = b"\0" * (10 * 1024 * 1024)
    bomb = gzip.compress(bomb_plain)
    assert len(bomb) < 20_000

    async def handler(request):
        return hyperhttp.MockResponse(
            200,
            content=bomb,
            headers={"Content-Encoding": "gzip"},
        )

    async with hyperhttp.Client(
        transport=hyperhttp.MockTransport(handler),
        max_decompressed_size=256 * 1024,  # 256 KiB
    ) as c:
        r = await c.get("https://x/y")
        with pytest.raises(DecompressionError):
            await r.aread()


async def test_uncapped_clients_still_work() -> None:
    """Setting both caps to ``None`` disables enforcement (opt-out)."""
    payload = b"X" * (2 * 1024 * 1024)

    async def handler(request):
        return hyperhttp.MockResponse(200, content=payload)

    async with hyperhttp.Client(
        transport=hyperhttp.MockTransport(handler),
        max_response_size=None,
        max_decompressed_size=None,
    ) as c:
        r = await c.get("https://x/y")
        assert await r.aread() == payload


def test_client_rejects_negative_limits() -> None:
    with pytest.raises(ValueError):
        hyperhttp.Client(max_response_size=0)
    with pytest.raises(ValueError):
        hyperhttp.Client(max_decompressed_size=-1)


async def test_default_decompression_cap_is_64mib() -> None:
    """Default should be sensible: 64 MiB decoded body cap."""
    async with hyperhttp.Client() as c:
        assert c._max_decompressed_size == 64 * 1024 * 1024
        assert c._max_response_size is None


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
