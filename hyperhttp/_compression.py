"""
Transparent Content-Encoding decoding.

We stream-decode gzip, deflate, brotli, and zstd. Each decoder is a tiny
object with ``decompress(chunk)`` and ``flush()``. Brotli and zstd require
optional dependencies; missing them raises a clear error when an encoded
response actually arrives.
"""

from __future__ import annotations

import zlib
from typing import List, Optional

from hyperhttp._compat import (
    HAS_BROTLI,
    HAS_ZSTANDARD,
    brotli_decompress,
)

__all__ = ["make_decoder", "Decoder", "supported_encodings"]


class Decoder:
    """Base class for streaming decoders. Subclasses override ``decompress``."""

    def decompress(self, data: bytes) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def flush(self) -> bytes:
        return b""


class IdentityDecoder(Decoder):
    def decompress(self, data: bytes) -> bytes:
        return data


class DeflateDecoder(Decoder):
    """deflate / zlib; tolerant of raw deflate streams."""

    def __init__(self) -> None:
        self._decompressor = zlib.decompressobj()
        self._first = True

    def decompress(self, data: bytes) -> bytes:
        if not data:
            return b""
        if self._first:
            self._first = False
            try:
                return self._decompressor.decompress(data)
            except zlib.error:
                # Raw deflate (no zlib wrapper)
                self._decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
                return self._decompressor.decompress(data)
        return self._decompressor.decompress(data)

    def flush(self) -> bytes:
        return self._decompressor.flush()


class GzipDecoder(Decoder):
    def __init__(self) -> None:
        self._decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16)

    def decompress(self, data: bytes) -> bytes:
        if not data:
            return b""
        return self._decompressor.decompress(data)

    def flush(self) -> bytes:
        return self._decompressor.flush()


class BrotliDecoder(Decoder):
    """Brotli isn't streamable via the plain `brotli.decompress`; we buffer
    until ``flush`` for simplicity. Response bodies using brotli are small
    enough that this is not a real problem in practice."""

    def __init__(self) -> None:
        if not HAS_BROTLI:
            from hyperhttp._compat import HAS_BROTLI as _h

            if not _h:
                raise RuntimeError(
                    "Received brotli-encoded response but brotli is not installed. "
                    "Install hyperhttp[speed]."
                )
        self._buffer = bytearray()

    def decompress(self, data: bytes) -> bytes:
        self._buffer.extend(data)
        return b""

    def flush(self) -> bytes:
        if not self._buffer:
            return b""
        out = brotli_decompress(bytes(self._buffer))
        self._buffer = bytearray()
        return out


class ZstdDecoder(Decoder):
    """zstandard with a streaming decompressor."""

    def __init__(self) -> None:
        if not HAS_ZSTANDARD:
            raise RuntimeError(
                "Received zstd-encoded response but zstandard is not installed. "
                "Install hyperhttp[speed]."
            )
        import zstandard  # type: ignore

        self._dctx = zstandard.ZstdDecompressor()
        self._dobj = self._dctx.decompressobj()

    def decompress(self, data: bytes) -> bytes:
        if not data:
            return b""
        return self._dobj.decompress(data)

    def flush(self) -> bytes:
        return self._dobj.flush()


class ChainedDecoder(Decoder):
    """Apply multiple decoders in order (for responses chaining encodings)."""

    def __init__(self, decoders: List[Decoder]) -> None:
        self._decoders = decoders

    def decompress(self, data: bytes) -> bytes:
        for dec in self._decoders:
            data = dec.decompress(data)
        return data

    def flush(self) -> bytes:
        out = b""
        for dec in self._decoders:
            tail = dec.flush()
            if out:
                tail = dec.decompress(out) + tail  # type: ignore[assignment]
            out = tail
        return out


def supported_encodings() -> List[str]:
    enc = ["gzip", "deflate"]
    if HAS_BROTLI:
        enc.append("br")
    if HAS_ZSTANDARD:
        enc.append("zstd")
    enc.append("identity")
    return enc


def make_decoder(content_encoding: Optional[str]) -> Decoder:
    """Build a decoder for a Content-Encoding header value (comma-separated)."""
    if not content_encoding:
        return IdentityDecoder()
    encodings = [e.strip().lower() for e in content_encoding.split(",") if e.strip()]
    if not encodings or encodings == ["identity"]:
        return IdentityDecoder()

    decoders: List[Decoder] = []
    # Content-Encoding applies in reverse order on receive: the last-listed
    # encoding was applied last, so we must decode it first.
    for enc in reversed(encodings):
        if enc == "gzip" or enc == "x-gzip":
            decoders.append(GzipDecoder())
        elif enc == "deflate":
            decoders.append(DeflateDecoder())
        elif enc == "br":
            decoders.append(BrotliDecoder())
        elif enc == "zstd":
            decoders.append(ZstdDecoder())
        elif enc == "identity":
            continue
        else:
            raise RuntimeError(f"Unsupported Content-Encoding: {enc!r}")

    if len(decoders) == 1:
        return decoders[0]
    return ChainedDecoder(decoders)
