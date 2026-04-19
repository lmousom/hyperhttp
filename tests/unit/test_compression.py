import gzip
import zlib

import pytest

import hyperhttp
from hyperhttp._compression import (
    ChainedDecoder,
    DeflateDecoder,
    GzipDecoder,
    IdentityDecoder,
    make_decoder,
    supported_encodings,
)


def _decode_all(decoder, data: bytes) -> bytes:
    return decoder.decompress(data) + decoder.flush()


def test_identity_passthrough():
    d = IdentityDecoder()
    assert _decode_all(d, b"hello") == b"hello"


def test_gzip_round_trip_streaming():
    payload = b"hello world" * 50
    gz = gzip.compress(payload)
    d = GzipDecoder()
    # Split into two halves to exercise streaming.
    mid = len(gz) // 2
    out = d.decompress(gz[:mid]) + d.decompress(gz[mid:]) + d.flush()
    assert out == payload


def test_gzip_empty_input_returns_empty():
    d = GzipDecoder()
    assert d.decompress(b"") == b""


def test_deflate_zlib_wrapped():
    payload = b"abc" * 100
    wrapped = zlib.compress(payload)
    d = DeflateDecoder()
    assert _decode_all(d, wrapped) == payload


def test_deflate_raw_fallback():
    payload = b"abc" * 100
    co = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    raw = co.compress(payload) + co.flush()
    d = DeflateDecoder()
    assert _decode_all(d, raw) == payload


def test_deflate_empty_input_returns_empty():
    d = DeflateDecoder()
    assert d.decompress(b"") == b""


def test_make_decoder_none_is_identity():
    assert isinstance(make_decoder(None), IdentityDecoder)
    assert isinstance(make_decoder(""), IdentityDecoder)
    assert isinstance(make_decoder("identity"), IdentityDecoder)


def test_make_decoder_gzip():
    d = make_decoder("gzip")
    assert isinstance(d, GzipDecoder)


def test_make_decoder_chains_gzip_and_deflate():
    payload = b"xyz" * 50
    inner = zlib.compress(payload)
    outer = gzip.compress(inner)
    # "deflate, gzip" means: gzip applied last → decode gzip first.
    d = make_decoder("deflate, gzip")
    assert isinstance(d, ChainedDecoder)
    out = d.decompress(outer) + d.flush()
    assert out == payload


def test_make_decoder_unknown_raises():
    with pytest.raises(RuntimeError):
        make_decoder("rot13")


def test_supported_encodings_contains_gzip_and_deflate():
    enc = supported_encodings()
    assert "gzip" in enc
    assert "deflate" in enc
    assert "identity" in enc


@pytest.mark.skipif(not hyperhttp.HAS_BROTLI, reason="brotli not installed")
def test_brotli_decoder_round_trip():
    import brotli  # type: ignore

    payload = b"hello " * 100
    compressed = brotli.compress(payload)
    d = make_decoder("br")
    out = d.decompress(compressed) + d.flush()
    assert out == payload


@pytest.mark.skipif(not hyperhttp.HAS_ZSTANDARD, reason="zstandard not installed")
def test_zstd_decoder_round_trip():
    import zstandard  # type: ignore

    payload = b"hello " * 100
    compressed = zstandard.ZstdCompressor().compress(payload)
    d = make_decoder("zstd")
    out = d.decompress(compressed) + d.flush()
    assert out == payload
