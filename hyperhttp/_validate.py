"""
Hot-path validators that reject wire-level smuggling vectors.

Anything that lands verbatim in the HTTP/1 request line or header block must
be free of CR / LF / NUL — otherwise an attacker-controlled substring of a
header value, a filename, or a URL target becomes header / request injection.
We also enforce RFC 7230 token grammar for header names and the request
method.

These checks are intentionally small: ``str.translate`` with a precomputed
table is the fastest way to detect forbidden characters in a Python string,
and the token validators short-circuit to an all-ASCII fast path.
"""

from __future__ import annotations

from hyperhttp.exceptions import LocalProtocolError

__all__ = [
    "validate_header_name",
    "validate_header_value",
    "validate_method",
    "validate_target",
    "validate_multipart_param",
]


# ---------------------------------------------------------------------------
# Header names (RFC 7230 §3.2.6 "token")
# ---------------------------------------------------------------------------
#
# token = 1*tchar
# tchar = "!" / "#" / "$" / "%" / "&" / "'" / "*" / "+" / "-" / "." /
#         "^" / "_" / "`" / "|" / "~" / DIGIT / ALPHA

_TCHAR = frozenset(
    "!#$%&'*+-.^_`|~0123456789"
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
)


def validate_header_name(name: str) -> None:
    """Raise :class:`LocalProtocolError` if ``name`` is not an RFC 7230 token."""
    if not isinstance(name, str) or not name:
        raise LocalProtocolError(f"Invalid header name: {name!r}")
    # Fast path: reject any char outside the token set.
    for ch in name:
        if ch not in _TCHAR:
            raise LocalProtocolError(
                f"Invalid character in header name: {name!r}"
            )


# ---------------------------------------------------------------------------
# Header values — block CR / LF / NUL
# ---------------------------------------------------------------------------
#
# RFC 7230 allows VCHAR / obs-text / SP / HTAB. We take a tighter view here:
# anything that would let an attacker escape the header line (CR, LF, NUL) is
# rejected. Everything else passes through; the transport encodes values with
# latin-1 so any high-bit bytes round-trip unchanged.

_HEADER_VALUE_FORBIDDEN = "\r\n\0"


def validate_header_value(value: str) -> None:
    if not isinstance(value, str):
        raise LocalProtocolError(
            f"Header value must be a string, got {type(value).__name__}"
        )
    # ``str.translate`` with a tiny deletion table is faster than a loop for
    # typical header sizes and has no overhead on the happy path.
    for ch in _HEADER_VALUE_FORBIDDEN:
        if ch in value:
            raise LocalProtocolError(
                "Header value contains forbidden character "
                f"(CR/LF/NUL) — possible header injection: {value!r}"
            )


# ---------------------------------------------------------------------------
# Request method — RFC 7230 token
# ---------------------------------------------------------------------------


def validate_method(method: str) -> None:
    if not isinstance(method, str) or not method:
        raise LocalProtocolError(f"Invalid HTTP method: {method!r}")
    for ch in method:
        if ch not in _TCHAR:
            raise LocalProtocolError(
                f"Invalid character in HTTP method: {method!r}"
            )


# ---------------------------------------------------------------------------
# Request target — no CR / LF / NUL / SP (space would split the request line)
# ---------------------------------------------------------------------------

_TARGET_FORBIDDEN = "\r\n\0 "


def validate_target(target: str) -> None:
    if not isinstance(target, str) or not target:
        raise LocalProtocolError(f"Invalid request target: {target!r}")
    for ch in _TARGET_FORBIDDEN:
        if ch in target:
            raise LocalProtocolError(
                "Request target contains forbidden character "
                f"(CR/LF/NUL/SP): {target!r}"
            )


# ---------------------------------------------------------------------------
# Multipart parameter values (field name, filename)
# ---------------------------------------------------------------------------
#
# The multipart encoder formats these inside a ``Content-Disposition`` header
# that lives in the body. They share the header-value CRLF rule: any raw CR/LF
# here would corrupt the MIME structure and potentially smuggle a new part.


def validate_multipart_param(value: str, *, field: str) -> None:
    if not isinstance(value, str):
        raise LocalProtocolError(
            f"multipart {field} must be a string, got {type(value).__name__}"
        )
    for ch in _HEADER_VALUE_FORBIDDEN:
        if ch in value:
            raise LocalProtocolError(
                f"multipart {field} contains forbidden character "
                f"(CR/LF/NUL): {value!r}"
            )
