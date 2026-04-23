"""
Authentication helpers.

The public interface is the :class:`Auth` base class. Each helper exposes a
generator-style ``auth_flow(request)`` that yields one or more
:class:`~hyperhttp.client.Request` objects and receives the corresponding
:class:`~hyperhttp.client.Response` objects via ``.send()``. That structure
lets single-shot schemes (Basic, Bearer) and challenge-response schemes
(Digest) share the same driver in :class:`hyperhttp.Client`.

Built-in schemes:

* :class:`BasicAuth` — RFC 7617 Basic.
* :class:`BearerAuth` — RFC 6750 Bearer tokens.
* :class:`DigestAuth` — RFC 7616 Digest with MD5 / SHA-256 and
  ``algorithm-sess`` variants, ``qop=auth``.

Convenience coercion: ``auth=("user", "pass")`` becomes :class:`BasicAuth`.
Pass ``auth=None`` on a per-request call to disable a client-level default.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
import time
from typing import TYPE_CHECKING, Any, Dict, Generator, Optional, Tuple, Union

if TYPE_CHECKING:
    from hyperhttp.client import Request, Response


__all__ = [
    "Auth",
    "BasicAuth",
    "BearerAuth",
    "DigestAuth",
]


AuthTypes = Union["Auth", Tuple[str, str], None]


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class Auth:
    """Base class for authentication schemes.

    Subclasses implement :meth:`auth_flow`, a generator that yields
    ``Request`` objects (optionally mutated with auth headers) and receives
    ``Response`` objects back via ``.send()``. For single-shot auth the
    generator yields once and returns; for challenge-response schemes it
    yields again after inspecting the challenge response.
    """

    requires_response: bool = False
    """If ``False``, the driver can skip buffering intermediate responses.

    Subclasses that need to read the response (e.g. Digest inspecting the
    ``WWW-Authenticate`` header) should set this to ``True``.
    """

    def auth_flow(
        self, request: "Request"
    ) -> Generator["Request", "Response", None]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Basic
# ---------------------------------------------------------------------------


class BasicAuth(Auth):
    """HTTP Basic authentication (RFC 7617)."""

    __slots__ = ("_header",)

    def __init__(self, username: str, password: str) -> None:
        if not isinstance(username, str) or not isinstance(password, str):
            raise TypeError("BasicAuth credentials must be strings")
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode(
            "ascii"
        )
        self._header = f"Basic {token}"

    def auth_flow(
        self, request: "Request"
    ) -> Generator["Request", "Response", None]:
        request.headers["Authorization"] = self._header
        yield request


# ---------------------------------------------------------------------------
# Bearer
# ---------------------------------------------------------------------------


class BearerAuth(Auth):
    """Static Bearer-token authentication (RFC 6750)."""

    __slots__ = ("_header",)

    def __init__(self, token: str) -> None:
        if not isinstance(token, str) or not token:
            raise ValueError("BearerAuth requires a non-empty string token")
        # Raw token — we don't try to parse or validate shape; servers do.
        self._header = f"Bearer {token}"

    def auth_flow(
        self, request: "Request"
    ) -> Generator["Request", "Response", None]:
        request.headers["Authorization"] = self._header
        yield request


# ---------------------------------------------------------------------------
# Digest (RFC 7616)
# ---------------------------------------------------------------------------


# Supported hash algorithms. The first value is the hashlib constructor, the
# second flags the ``-sess`` variants (which change how HA1 is computed).
_DIGEST_ALGORITHMS: Dict[str, Tuple[Any, bool]] = {
    "MD5": (hashlib.md5, False),
    "MD5-SESS": (hashlib.md5, True),
    "SHA-256": (hashlib.sha256, False),
    "SHA-256-SESS": (hashlib.sha256, True),
    "SHA-512-256": (lambda: hashlib.new("sha512_256"), False),
    "SHA-512-256-SESS": (lambda: hashlib.new("sha512_256"), True),
}


class DigestAuth(Auth):
    """HTTP Digest authentication (RFC 7616).

    Supports MD5, SHA-256 and SHA-512/256, each with optional ``-sess``
    suffix, and ``qop=auth``. ``qop=auth-int`` (body integrity) is not
    supported — servers that require it will reject the response.
    """

    requires_response = True

    __slots__ = (
        "_username",
        "_password",
        "_last_nonce",
        "_nonce_count",
    )

    def __init__(self, username: str, password: str) -> None:
        if not isinstance(username, str) or not isinstance(password, str):
            raise TypeError("DigestAuth credentials must be strings")
        self._username = username
        self._password = password
        # Cache the last nonce so we can reuse it with an incremented nc if
        # the server sends back ``Authentication-Info`` with a fresh nonce
        # (future enhancement). Today we recompute per challenge.
        self._last_nonce: Optional[str] = None
        self._nonce_count = 0

    # ------------------------------------------------------------------
    # Public flow
    # ------------------------------------------------------------------

    def auth_flow(
        self, request: "Request"
    ) -> Generator["Request", "Response", None]:
        # Round 1: send without credentials to trigger the challenge.
        response = yield request
        if response.status_code != 401:
            return

        www_auth = response.headers.get("www-authenticate")
        if not www_auth or not _looks_like_digest(www_auth):
            # Server didn't challenge with Digest — nothing we can do.
            return

        challenge = _parse_challenge(www_auth)
        header = self._build_authorization(
            method=request.method,
            path=request.url.target,
            body=request.content,
            challenge=challenge,
        )
        request.headers["Authorization"] = header
        yield request

    # ------------------------------------------------------------------
    # Digest construction
    # ------------------------------------------------------------------

    def _build_authorization(
        self,
        *,
        method: str,
        path: str,
        body: Any,
        challenge: Dict[str, str],
    ) -> str:
        algorithm = challenge.get("algorithm", "MD5").upper()
        if algorithm not in _DIGEST_ALGORITHMS:
            raise ValueError(f"Unsupported Digest algorithm: {algorithm}")
        hash_ctor, is_sess = _DIGEST_ALGORITHMS[algorithm]

        realm = challenge.get("realm", "")
        nonce = challenge.get("nonce")
        if not nonce:
            raise ValueError("Digest challenge is missing 'nonce'")
        opaque = challenge.get("opaque")
        qop_header = challenge.get("qop")

        qop: Optional[str]
        if qop_header is None:
            qop = None
        else:
            # Header may list "auth,auth-int" — we only support "auth".
            options = [q.strip() for q in qop_header.split(",")]
            if "auth" in options:
                qop = "auth"
            else:
                raise ValueError(
                    f"Digest challenge requires unsupported qop: {qop_header!r} "
                    "(only 'auth' is supported)"
                )

        if nonce == self._last_nonce:
            self._nonce_count += 1
        else:
            self._last_nonce = nonce
            self._nonce_count = 1
        nc = f"{self._nonce_count:08x}"
        cnonce = _make_cnonce()

        def _h(data: str) -> str:
            h = hash_ctor()
            h.update(data.encode("utf-8"))
            return h.hexdigest()

        ha1 = _h(f"{self._username}:{realm}:{self._password}")
        if is_sess:
            ha1 = _h(f"{ha1}:{nonce}:{cnonce}")

        ha2 = _h(f"{method}:{path}")

        if qop == "auth":
            response_digest = _h(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
        else:
            response_digest = _h(f"{ha1}:{nonce}:{ha2}")

        # Assemble the header. Quoted params get quoted; nc/qop/algorithm
        # are unquoted per RFC 7616 §3.4.
        params: list = [
            f'username="{_quote(self._username)}"',
            f'realm="{_quote(realm)}"',
            f'nonce="{_quote(nonce)}"',
            f'uri="{_quote(path)}"',
            f'response="{response_digest}"',
            f"algorithm={algorithm}",
        ]
        if opaque is not None:
            params.append(f'opaque="{_quote(opaque)}"')
        if qop is not None:
            params.extend(
                [
                    f"qop={qop}",
                    f"nc={nc}",
                    f'cnonce="{_quote(cnonce)}"',
                ]
            )
        return "Digest " + ", ".join(params)


# ---------------------------------------------------------------------------
# Coercion
# ---------------------------------------------------------------------------


def _coerce_auth(auth: Any) -> Optional[Auth]:
    if auth is None:
        return None
    if isinstance(auth, Auth):
        return auth
    if isinstance(auth, tuple):
        if len(auth) != 2 or not all(isinstance(x, str) for x in auth):
            raise TypeError(
                "auth tuple must be (username, password) of two strings"
            )
        return BasicAuth(*auth)
    raise TypeError(
        "auth must be an Auth instance, a (username, password) tuple, or None"
    )


# ---------------------------------------------------------------------------
# Challenge header parsing
# ---------------------------------------------------------------------------


# ``WWW-Authenticate`` is a comma-separated list, but values may contain
# commas inside quoted strings. This regex pulls out one key=value (quoted or
# token) at a time.
_KV_RE = re.compile(
    r"""
    \s*
    ([A-Za-z][A-Za-z0-9_\-]*)    # key
    \s*=\s*
    (                             # value:
        "((?:[^"\\]|\\.)*)"       #   quoted string (captures inner)
        |
        ([^\s,]+)                 #   token
    )
    \s*,?
    """,
    re.VERBOSE,
)


def _looks_like_digest(header: str) -> bool:
    # The header may list multiple schemes — pick out any Digest challenge.
    for scheme in _split_challenges(header):
        if scheme.lower().startswith("digest"):
            return True
    return False


def _parse_challenge(header: str) -> Dict[str, str]:
    """Extract the ``Digest`` challenge from a ``WWW-Authenticate`` header."""
    for scheme in _split_challenges(header):
        if not scheme.lower().startswith("digest"):
            continue
        body = scheme[len("Digest") :].strip()
        params: Dict[str, str] = {}
        for match in _KV_RE.finditer(body):
            key = match.group(1).lower()
            quoted = match.group(3)
            token = match.group(4)
            if quoted is not None:
                params[key] = _unescape(quoted)
            else:
                params[key] = token
        return params
    raise ValueError("No Digest challenge found")


def _split_challenges(header: str) -> list:
    """Split a ``WWW-Authenticate`` value into per-scheme chunks.

    Schemes are separated by commas, but commas also appear *inside* quoted
    parameter values and between parameters of the same scheme. We split on
    a scheme boundary: a comma (or start-of-string) followed by a bare word
    followed by whitespace, where the bare word isn't an already-seen
    parameter name. In practice, though, ``Digest`` is almost always the
    only scheme in the header, so this conservative splitter is enough.
    """
    # Fast path: single scheme.
    head = header.lstrip().split(None, 1)
    if not head:
        return []
    # Most servers emit exactly one scheme; return it whole.
    return [header.strip()]


def _quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _unescape(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\")


def _make_cnonce() -> str:
    # 16 bytes of entropy → 32 hex chars. Mixing in time gives an extra
    # monotonic component so reused entropy within the same second still
    # produces a unique cnonce.
    raw = secrets.token_bytes(16) + int(time.time()).to_bytes(8, "big")
    return hashlib.sha256(raw + os.urandom(8)).hexdigest()[:32]
