"""
Optional-dependency shims.

Every fast dependency here is strictly optional; a pure-Python fallback is
always available. Callers should use the module-level flags/functions rather
than importing the third-party modules directly, so the fast path is a single
``if HAS_FOO`` branch.
"""

from __future__ import annotations

import json as _stdlib_json
from typing import Any, Callable, Optional

__all__ = [
    "HAS_ORJSON",
    "HAS_UVLOOP",
    "HAS_H11",
    "HAS_BROTLI",
    "HAS_ZSTANDARD",
    "json_dumps",
    "json_loads",
    "install_uvloop",
    "brotli_decompress",
    "zstd_decompress",
    "h11",
]

# ---------------------------------------------------------------------------
# orjson (JSON)
# ---------------------------------------------------------------------------

try:  # pragma: no cover - trivial branch
    import orjson as _orjson  # type: ignore

    HAS_ORJSON = True
except ImportError:  # pragma: no cover
    _orjson = None
    HAS_ORJSON = False


def json_dumps(obj: Any) -> bytes:
    """Serialize ``obj`` to UTF-8 bytes using the fastest available JSON encoder."""
    if HAS_ORJSON:
        return _orjson.dumps(obj)  # type: ignore[union-attr]
    return _stdlib_json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def json_loads(data: Any) -> Any:
    """Deserialize JSON from ``bytes``/``bytearray``/``str``."""
    if HAS_ORJSON:
        if isinstance(data, str):
            return _orjson.loads(data.encode("utf-8"))  # type: ignore[union-attr]
        return _orjson.loads(data)  # type: ignore[union-attr]
    if isinstance(data, (bytes, bytearray, memoryview)):
        data = bytes(data).decode("utf-8")
    return _stdlib_json.loads(data)


# ---------------------------------------------------------------------------
# uvloop
# ---------------------------------------------------------------------------

try:  # pragma: no cover
    import uvloop as _uvloop  # type: ignore

    HAS_UVLOOP = True
except ImportError:  # pragma: no cover
    _uvloop = None
    HAS_UVLOOP = False


def install_uvloop() -> bool:
    """Install uvloop as the default asyncio event loop policy.

    Returns True if uvloop is installed, False otherwise. Safe to call from
    user code at module import; a no-op if uvloop is not available.
    """
    if not HAS_UVLOOP:
        return False
    _uvloop.install()  # type: ignore[union-attr]
    return True


# ---------------------------------------------------------------------------
# h11 (HTTP/1.1 parser)
# ---------------------------------------------------------------------------

try:  # pragma: no cover
    import h11 as _h11  # type: ignore

    HAS_H11 = True
    h11: Optional[Any] = _h11
except ImportError:  # pragma: no cover
    h11 = None
    HAS_H11 = False


# ---------------------------------------------------------------------------
# brotli / brotlicffi
# ---------------------------------------------------------------------------

_brotli_decompress: Optional[Callable[[bytes], bytes]] = None

try:  # pragma: no cover
    import brotli as _brotli  # type: ignore

    _brotli_decompress = _brotli.decompress
    HAS_BROTLI = True
except ImportError:  # pragma: no cover
    try:
        import brotlicffi as _brotli  # type: ignore

        _brotli_decompress = _brotli.decompress
        HAS_BROTLI = True
    except ImportError:
        HAS_BROTLI = False


def brotli_decompress(data: bytes) -> bytes:
    if not HAS_BROTLI:
        raise RuntimeError(
            "Brotli support requires the `brotli` or `brotlicffi` package. "
            "Install hyperhttp[speed]."
        )
    return _brotli_decompress(data)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# zstandard
# ---------------------------------------------------------------------------

try:  # pragma: no cover
    import zstandard as _zstd  # type: ignore

    HAS_ZSTANDARD = True
    _zstd_dctx = _zstd.ZstdDecompressor()
except ImportError:  # pragma: no cover
    _zstd = None
    _zstd_dctx = None
    HAS_ZSTANDARD = False


def zstd_decompress(data: bytes) -> bytes:
    if not HAS_ZSTANDARD:
        raise RuntimeError(
            "zstd support requires the `zstandard` package. Install hyperhttp[speed]."
        )
    return _zstd_dctx.decompress(data)  # type: ignore[union-attr]


def accept_encoding() -> str:
    """Build an Accept-Encoding header for what we can actually decode."""
    parts = ["gzip", "deflate"]
    if HAS_BROTLI:
        parts.append("br")
    if HAS_ZSTANDARD:
        parts.append("zstd")
    return ", ".join(parts)
