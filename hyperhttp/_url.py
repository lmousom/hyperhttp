"""
URL parsing and request-target construction.

We use stdlib ``urllib.parse`` but wrap it in a thin value object that caches
the bits we need on the hot path (``host_port`` for pool lookup, ``target``
for the HTTP request line, etc.).
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse, urlunparse

from hyperhttp.exceptions import InvalidURL

QueryInput = Union[
    None,
    str,
    bytes,
    Mapping[str, Any],
    Sequence[Tuple[str, Any]],
]


class URL:
    __slots__ = (
        "_raw",
        "scheme",
        "host",
        "port",
        "path",
        "query",
        "fragment",
        "userinfo",
    )

    def __init__(self, raw: str) -> None:
        if not isinstance(raw, str) or not raw:
            raise InvalidURL(f"URL must be a non-empty string, got {raw!r}")
        parsed = urlparse(raw)
        if not parsed.scheme:
            raise InvalidURL(f"URL is missing scheme: {raw!r}")
        if parsed.scheme not in ("http", "https"):
            raise InvalidURL(f"Unsupported URL scheme: {parsed.scheme!r}")
        if not parsed.hostname:
            raise InvalidURL(f"URL is missing host: {raw!r}")
        self._raw = raw
        self.scheme: str = parsed.scheme
        self.host: str = parsed.hostname
        self.port: int = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.path: str = parsed.path or "/"
        self.query: str = parsed.query
        self.fragment: str = parsed.fragment
        self.userinfo: Optional[str] = None
        if parsed.username is not None:
            user = parsed.username
            pw = parsed.password
            self.userinfo = f"{user}:{pw}" if pw is not None else user

    # --- derived accessors -------------------------------------------------

    @property
    def host_port(self) -> str:
        """Host[:port] string used as a connection-pool key."""
        default = 443 if self.scheme == "https" else 80
        if self.port == default:
            return self.host
        return f"{self.host}:{self.port}"

    @property
    def authority(self) -> str:
        return self.host_port

    @property
    def target(self) -> str:
        """Path + query, as used in the HTTP request line / :path header."""
        if self.query:
            return f"{self.path}?{self.query}"
        return self.path

    @property
    def is_secure(self) -> bool:
        return self.scheme == "https"

    def with_query(self, params: QueryInput) -> "URL":
        """Return a new URL with ``params`` merged into the query string."""
        if params is None:
            return self
        merged = parse_qsl(self.query, keep_blank_values=True)
        if isinstance(params, (str, bytes)):
            new_query = params if isinstance(params, str) else params.decode("ascii")
            if merged:
                new_query = f"{self.query}&{new_query}" if new_query else self.query
        else:
            if isinstance(params, Mapping):
                items = list(params.items())
            else:
                items = list(params)
            merged.extend((str(k), str(v)) for k, v in items)
            new_query = urlencode(merged, doseq=True)
        return URL(
            urlunparse(
                (self.scheme, self._netloc(), self.path, "", new_query, self.fragment)
            )
        )

    def _netloc(self) -> str:
        host = self.host
        default = 443 if self.scheme == "https" else 80
        base = host if self.port == default else f"{host}:{self.port}"
        if self.userinfo:
            return f"{self.userinfo}@{base}"
        return base

    def join(self, ref: str) -> "URL":
        """Resolve ``ref`` against this URL (used for redirects)."""
        from urllib.parse import urljoin

        return URL(urljoin(self._raw, ref))

    # --- dunder ------------------------------------------------------------

    def __str__(self) -> str:
        return self._raw

    def __repr__(self) -> str:
        return f"URL({self._raw!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, URL):
            return self._raw == other._raw
        if isinstance(other, str):
            return self._raw == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._raw)


def encode_query(params: QueryInput) -> str:
    if params is None:
        return ""
    if isinstance(params, str):
        return params
    if isinstance(params, bytes):
        return params.decode("ascii")
    if isinstance(params, Mapping):
        items = list(params.items())
    else:
        items = list(params)
    return urlencode([(str(k), str(v)) for k, v in items], doseq=True)


__all__ = ["URL", "QueryInput", "encode_query"]
