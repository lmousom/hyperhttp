"""
Transparent Content-Encoding decoding.

We stream-decode gzip, deflate, brotli, and zstd. Each decoder is a tiny
object with ``decompress(chunk)`` and ``flush()``. Brotli and zstd require
optional dependencies; missing them raises a clear error when an encoded
response actually arrives.

Security: every decoder tracks the number of bytes it has produced and
raises :class:`DecompressionError` once a per-response ``max_output_size``
is crossed. Without this, a 1 KiB compressed payload that inflates to 10 GB
(a "zip bomb") would silently OOM the process. The limit is enforced at
:meth:`Decoder.decompress` and :meth:`Decoder.flush` exits and — critically —
for brotli (which is non-streaming) is also checked against the accumulated
*compressed* buffer so we never expand an oversized input at all.
"""

from __future__ import annotations

import zlib
from typing import List, Optional

from hyperhttp._compat import (
    HAS_BROTLI,
    HAS_ZSTANDARD,
    brotli_decompress,
)
from hyperhttp.exceptions import DecompressionError

__all__ = ["make_decoder", "Decoder", "supported_encodings"]


class Decoder:
    """Base class for streaming decoders.

    ``max_output_size`` (bytes) is the per-response cap on decompressed
    output; ``None`` disables the cap (not recommended except for trusted
    peers). Subclasses must call :meth:`_track` on every chunk they return
    so the accountant stays accurate.
    """

    __slots__ = ("_max_output", "_produced")

    def __init__(self, *, max_output_size: Optional[int] = None) -> None:
        self._max_output = max_output_size
        self._produced = 0

    def decompress(self, data: bytes) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def flush(self) -> bytes:
        return b""

    def _track(self, chunk: bytes) -> bytes:
        if not chunk:
            return chunk
        if self._max_output is not None:
            self._produced += len(chunk)
            if self._produced > self._max_output:
                raise DecompressionError(
                    "Decompressed response exceeded max_decompressed_size "
                    f"({self._max_output} bytes) — possible decompression bomb"
                )
        return chunk


class IdentityDecoder(Decoder):
    __slots__ = ()

    def decompress(self, data: bytes) -> bytes:
        return self._track(data)


class DeflateDecoder(Decoder):
    """deflate / zlib; tolerant of raw deflate streams."""

    __slots__ = ("_decompressor", "_first")

    def __init__(self, *, max_output_size: Optional[int] = None) -> None:
        super().__init__(max_output_size=max_output_size)
        self._decompressor = zlib.decompressobj()
        self._first = True

    def decompress(self, data: bytes) -> bytes:
        if not data:
            return b""
        if self._first:
            self._first = False
            try:
                return self._track(self._decompressor.decompress(data))
            except zlib.error:
                self._decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
                return self._track(self._decompressor.decompress(data))
        return self._track(self._decompressor.decompress(data))

    def flush(self) -> bytes:
        return self._track(self._decompressor.flush())


class GzipDecoder(Decoder):
    __slots__ = ("_decompressor",)

    def __init__(self, *, max_output_size: Optional[int] = None) -> None:
        super().__init__(max_output_size=max_output_size)
        self._decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16)

    def decompress(self, data: bytes) -> bytes:
        if not data:
            return b""
        return self._track(self._decompressor.decompress(data))

    def flush(self) -> bytes:
        return self._track(self._decompressor.flush())


class BrotliDecoder(Decoder):
    """Brotli isn't streamable via the plain ``brotli.decompress``.

    We buffer compressed bytes until ``flush`` and then decompress once. To
    keep this bounded, we also cap the *compressed* input at
    ``max_output_size`` (same value — a compressed input already larger than
    the decoded cap cannot possibly decode to something smaller). That stops
    an attacker from feeding us gigabytes of brotli-compressed junk that we'd
    buffer before even trying to inflate.
    """

    __slots__ = ("_buffer",)

    def __init__(self, *, max_output_size: Optional[int] = None) -> None:
        super().__init__(max_output_size=max_output_size)
        if not HAS_BROTLI:
            raise RuntimeError(
                "Received brotli-encoded response but brotli is not installed. "
                "Install hyperhttp[speed]."
            )
        self._buffer = bytearray()

    def decompress(self, data: bytes) -> bytes:
        if not data:
            return b""
        if self._max_output is not None and (
            len(self._buffer) + len(data) > self._max_output
        ):
            raise DecompressionError(
                "Brotli input exceeded max_decompressed_size "
                f"({self._max_output} bytes) before decoding — possible bomb"
            )
        self._buffer.extend(data)
        return b""

    def flush(self) -> bytes:
        if not self._buffer:
            return b""
        out = brotli_decompress(bytes(self._buffer))
        self._buffer = bytearray()
        return self._track(out)


class ZstdDecoder(Decoder):
    """zstandard with a streaming decompressor."""

    __slots__ = ("_dctx", "_dobj")

    def __init__(self, *, max_output_size: Optional[int] = None) -> None:
        super().__init__(max_output_size=max_output_size)
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
        return self._track(self._dobj.decompress(data))

    def flush(self) -> bytes:
        return self._track(self._dobj.flush())


class ChainedDecoder(Decoder):
    """Apply multiple decoders in order (for responses chaining encodings)."""

    __slots__ = ("_decoders",)

    def __init__(
        self,
        decoders: List[Decoder],
        *,
        max_output_size: Optional[int] = None,
    ) -> None:
        super().__init__(max_output_size=max_output_size)
        self._decoders = decoders

    def decompress(self, data: bytes) -> bytes:
        for dec in self._decoders:
            data = dec.decompress(data)
        return self._track(data)

    def flush(self) -> bytes:
        out = b""
        for dec in self._decoders:
            tail = dec.flush()
            if out:
                tail = dec.decompress(out) + tail
            out = tail
        return self._track(out)


def supported_encodings() -> List[str]:
    enc = ["gzip", "deflate"]
    if HAS_BROTLI:
        enc.append("br")
    if HAS_ZSTANDARD:
        enc.append("zstd")
    enc.append("identity")
    return enc


def make_decoder(
    content_encoding: Optional[str],
    *,
    max_output_size: Optional[int] = None,
) -> Decoder:
    """Build a decoder for a Content-Encoding header value (comma-separated).

    ``max_output_size`` is the per-response decompressed-byte cap (applied
    after every chunk). ``None`` disables the cap; prefer a finite value for
    anything talking to untrusted peers.
    """
    if not content_encoding:
        return IdentityDecoder(max_output_size=max_output_size)
    encodings = [e.strip().lower() for e in content_encoding.split(",") if e.strip()]
    if not encodings or encodings == ["identity"]:
        return IdentityDecoder(max_output_size=max_output_size)

    decoders: List[Decoder] = []
    # Content-Encoding applies in reverse order on receive: the last-listed
    # encoding was applied last, so we must decode it first.
    for enc in reversed(encodings):
        if enc == "gzip" or enc == "x-gzip":
            decoders.append(GzipDecoder(max_output_size=max_output_size))
        elif enc == "deflate":
            decoders.append(DeflateDecoder(max_output_size=max_output_size))
        elif enc == "br":
            decoders.append(BrotliDecoder(max_output_size=max_output_size))
        elif enc == "zstd":
            decoders.append(ZstdDecoder(max_output_size=max_output_size))
        elif enc == "identity":
            continue
        else:
            raise RuntimeError(f"Unsupported Content-Encoding: {enc!r}")

    if len(decoders) == 1:
        return decoders[0]
    return ChainedDecoder(decoders, max_output_size=max_output_size)
