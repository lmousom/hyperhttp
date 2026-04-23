"""
Proxy configuration parsing and resolution.

Supports:

- Explicit ``proxies=`` passed to ``Client``, either as a single URL string or
  a per-scheme mapping (``{"http": "...", "https": "...", "all": "..."}``).
- Environment-variable pickup via ``trust_env=True``: ``HTTP_PROXY``,
  ``HTTPS_PROXY``, ``ALL_PROXY`` (upper- and lowercase), and ``NO_PROXY`` for
  exclusion patterns.
- Basic authentication via ``http://user:pass@proxy:3128``; credentials are
  stripped from the network-facing URL and converted into a
  ``Proxy-Authorization: Basic ...`` header at send time.

SOCKS proxies are **not** supported.
"""

from __future__ import annotations

import base64
import ipaddress
import os
from collections.abc import Mapping as _AbcMapping
from typing import Dict, List, Mapping, Optional, Union
from urllib.parse import unquote, urlparse

from hyperhttp._url import URL
from hyperhttp.exceptions import InvalidURL

__all__ = [
    "ProxyURL",
    "ProxyConfig",
    "ProxiesInput",
    "parse_proxy_url",
]


ProxiesInput = Union[None, str, "ProxyURL", Mapping[str, Union[str, "ProxyURL", None]]]


class ProxyURL:
    """A resolved proxy endpoint."""

    __slots__ = ("scheme", "host", "port", "username", "password", "_raw")

    def __init__(
        self,
        scheme: str,
        host: str,
        port: int,
        username: Optional[str] = None,
        password: Optional[str] = None,
        raw: Optional[str] = None,
    ) -> None:
        if scheme not in ("http", "https"):
            raise InvalidURL(f"Unsupported proxy scheme: {scheme!r}")
        self.scheme = scheme
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self._raw = raw or self._format()

    def _format(self) -> str:
        auth = ""
        if self.username is not None:
            if self.password is not None:
                auth = f"{self.username}:***@"
            else:
                auth = f"{self.username}@"
        return f"{self.scheme}://{auth}{self.host}:{self.port}"

    @property
    def authority(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def is_secure(self) -> bool:
        return self.scheme == "https"

    @property
    def has_auth(self) -> bool:
        return self.username is not None

    def basic_auth_header(self) -> Optional[str]:
        """Return the ``Basic <base64>`` value for ``Proxy-Authorization``."""
        if self.username is None:
            return None
        user = self.username
        pw = self.password or ""
        token = base64.b64encode(f"{user}:{pw}".encode("utf-8")).decode("ascii")
        return f"Basic {token}"

    def pool_key(self) -> str:
        """Stable identifier used to partition the connection pool."""
        auth = ""
        if self.username is not None:
            auth = f"{self.username}:{self.password or ''}@"
        return f"{self.scheme}://{auth}{self.host}:{self.port}"

    def __repr__(self) -> str:
        return f"ProxyURL({self._raw!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ProxyURL):
            return self.pool_key() == other.pool_key()
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.pool_key())


def parse_proxy_url(raw: Union[str, "ProxyURL"]) -> "ProxyURL":
    """Parse a proxy URL string."""
    if isinstance(raw, ProxyURL):
        return raw
    if not isinstance(raw, str) or not raw:
        raise InvalidURL(f"Proxy URL must be a non-empty string, got {raw!r}")
    # ``urlparse`` requires a scheme. Reject "host:port" style shorthand to
    # avoid silently picking the wrong scheme — be explicit.
    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        if scheme in ("socks4", "socks5", "socks5h"):
            raise InvalidURL(f"SOCKS proxies are not supported: {raw!r}")
        raise InvalidURL(f"Proxy URL needs http:// or https:// scheme: {raw!r}")
    if not parsed.hostname:
        raise InvalidURL(f"Proxy URL is missing host: {raw!r}")
    port = parsed.port or (443 if scheme == "https" else 80)
    username = unquote(parsed.username) if parsed.username else None
    password = unquote(parsed.password) if parsed.password else None
    return ProxyURL(
        scheme=scheme,
        host=parsed.hostname,
        port=port,
        username=username,
        password=password,
        raw=raw,
    )


# ---------------------------------------------------------------------------
# NO_PROXY matching
# ---------------------------------------------------------------------------


def _parse_no_proxy(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    out: List[str] = []
    for piece in raw.split(","):
        piece = piece.strip().lower()
        if not piece:
            continue
        # Strip leading "." and "*." for suffix matches.
        if piece.startswith("*."):
            piece = piece[2:]
        if piece.startswith("."):
            piece = piece[1:]
        out.append(piece)
    return out


def _host_matches_no_proxy(host: str, port: int, patterns: List[str]) -> bool:
    if not patterns:
        return False
    host = host.lower()
    # A literal ``*`` disables all proxying.
    if "*" in patterns:
        return True

    # IP literal? Match as exact address or, if the pattern is a CIDR, as a
    # range.
    ip: Optional[ipaddress._BaseAddress]
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None

    for pat in patterns:
        if ip is not None:
            if pat == host:
                return True
            try:
                net = ipaddress.ip_network(pat, strict=False)
            except ValueError:
                continue
            if ip in net:  # type: ignore[operator]
                return True
            continue

        # Host pattern.  "example.com:8080" restricts to a specific port.
        pat_host = pat
        pat_port: Optional[int] = None
        if ":" in pat and pat.count(":") == 1:
            pat_host, _, p = pat.partition(":")
            try:
                pat_port = int(p)
            except ValueError:
                continue
        if pat_port is not None and pat_port != port:
            continue
        if host == pat_host or host.endswith("." + pat_host):
            return True
    return False


# ---------------------------------------------------------------------------
# ProxyConfig — assembled once per Client
# ---------------------------------------------------------------------------


class ProxyConfig:
    """Resolved proxy configuration for a ``Client``."""

    __slots__ = ("_explicit", "_env", "_no_proxy_env", "_trust_env")

    def __init__(
        self,
        proxies: ProxiesInput = None,
        *,
        trust_env: bool = True,
    ) -> None:
        self._trust_env = trust_env
        self._explicit: Dict[str, Optional[ProxyURL]] = _normalise_proxies(proxies)

        # Environment-sourced proxies are resolved lazily on first use so that
        # env changes between Client construction and first request still take
        # effect (matches requests/httpx behaviour).
        self._env: Optional[Dict[str, Optional[ProxyURL]]] = None
        self._no_proxy_env: Optional[List[str]] = None

    def _env_proxies(self) -> Dict[str, Optional[ProxyURL]]:
        if not self._trust_env:
            return {}
        if self._env is not None:
            return self._env
        env: Dict[str, Optional[ProxyURL]] = {}
        for scheme in ("http", "https", "all"):
            key_upper = f"{scheme.upper()}_PROXY"
            key_lower = f"{scheme.lower()}_proxy"
            val = os.environ.get(key_upper) or os.environ.get(key_lower)
            if val:
                try:
                    env[scheme] = parse_proxy_url(val)
                except InvalidURL:
                    env[scheme] = None
        self._env = env
        return env

    def _no_proxy_patterns(self) -> List[str]:
        if not self._trust_env:
            return []
        if self._no_proxy_env is not None:
            return self._no_proxy_env
        raw = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")
        self._no_proxy_env = _parse_no_proxy(raw)
        return self._no_proxy_env

    def for_url(self, url: URL) -> Optional[ProxyURL]:
        """Return the proxy to use for ``url``, or ``None`` for a direct connect."""
        # Explicit config wins over environment.
        explicit = self._explicit
        env = self._env_proxies()

        # NO_PROXY only applies to environment-sourced proxies; explicit
        # ``proxies=`` is treated as authoritative intent.
        def pick(scheme: str) -> Optional[ProxyURL]:
            if scheme in explicit:
                return explicit[scheme]
            if "all" in explicit:
                return explicit["all"]
            if scheme in env and not _host_matches_no_proxy(
                url.host, url.port, self._no_proxy_patterns()
            ):
                return env[scheme]
            if "all" in env and not _host_matches_no_proxy(
                url.host, url.port, self._no_proxy_patterns()
            ):
                return env["all"]
            return None

        return pick(url.scheme)

    def has_any(self) -> bool:
        """Cheap check used to skip the resolve path entirely when nothing is set."""
        if any(v for v in self._explicit.values()):
            return True
        if self._trust_env:
            # Any env proxy var set?
            for scheme in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
                if os.environ.get(scheme):
                    return True
        return False


def _normalise_proxies(proxies: ProxiesInput) -> Dict[str, Optional[ProxyURL]]:
    if proxies is None:
        return {}
    if isinstance(proxies, (str, ProxyURL)):
        parsed = parse_proxy_url(proxies)
        return {"all": parsed}
    if isinstance(proxies, _AbcMapping):
        out: Dict[str, Optional[ProxyURL]] = {}
        for key, value in proxies.items():
            key_norm = key.lower()
            if key_norm not in ("http", "https", "all"):
                raise InvalidURL(
                    f"proxies key must be 'http', 'https', or 'all', got {key!r}"
                )
            if value is None:
                # Explicit None means "do not proxy this scheme".
                out[key_norm] = None
            else:
                out[key_norm] = parse_proxy_url(value)
        return out
    raise TypeError(
        f"proxies must be None, a str/ProxyURL, or a dict; got {type(proxies).__name__}"
    )
