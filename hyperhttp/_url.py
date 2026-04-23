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
        # Reject wire-control characters before parsing. ``urlparse`` happily
        # preserves CR/LF in hostnames/paths (CVE-2019-9740 territory) which,
        # combined with our latin-1 encoding on the wire, would be header
        # injection. Belt-and-suspenders: the transport also validates.
        for ch in ("\r", "\n", "\0", "\t"):
            if ch in raw:
                raise InvalidURL(
                    f"URL contains forbidden control character: {raw!r}"
                )
        parsed = urlparse(raw)
        if not parsed.scheme:
            raise InvalidURL(f"URL is missing scheme: {raw!r}")
        if parsed.scheme not in ("http", "https"):
            raise InvalidURL(f"Unsupported URL scheme: {parsed.scheme!r}")
        if not parsed.hostname:
            raise InvalidURL(f"URL is missing host: {raw!r}")
        # Defensive: make sure urlparse didn't round-trip any control bytes
        # into the host component either.
        if any(ch in parsed.hostname for ch in ("\r", "\n", "\0", " ", "\t")):
            raise InvalidURL(f"URL host contains forbidden character: {raw!r}")
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
        """Resolve ``ref`` against this URL (used for redirects).

        ``urljoin`` accepts any URI reference, including scheme-only
        references like ``javascript:alert(1)``. ``URL.__init__`` will
        reject those (only ``http``/``https`` pass), but we double-check
        here so a misbehaving server can't hand us something that bypasses
        the ultimate scheme filter via some urljoin edge case.
        """
        if not isinstance(ref, str) or not ref:
            raise InvalidURL(f"Redirect target must be a non-empty string, got {ref!r}")
        from urllib.parse import urljoin

        # Reject control chars in the redirect reference itself before
        # letting urljoin compose anything.
        for ch in ("\r", "\n", "\0", "\t"):
            if ch in ref:
                raise InvalidURL(
                    f"Redirect target contains forbidden character: {ref!r}"
                )
        return URL(urljoin(self._raw, ref))

    def sanitized(self) -> str:
        """URL safe to include in logs — strips userinfo and query string.

        Query strings frequently embed API keys, session tokens, or signed
        credentials (``?api_key=...``, ``?token=...``). Userinfo in the URL
        itself is also a credential. Both are replaced with redaction
        markers so ``logger.info("retrying %s", url.sanitized())`` cannot
        accidentally end up in a log aggregator with secrets attached.
        """
        base = f"{self.scheme}://{self.host}"
        default = 443 if self.scheme == "https" else 80
        if self.port != default:
            base += f":{self.port}"
        base += self.path
        if self.query:
            base += "?<redacted>"
        return base

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
