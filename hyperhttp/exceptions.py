"""
Exception hierarchy for hyperhttp.

All exceptions raised by public hyperhttp APIs inherit from ``HyperHTTPError``.
Network- and protocol-level errors subclass ``TransportError``; retryable
behavior is decided on a per-subclass basis by ``errors.classifier``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from hyperhttp.client import Response


class HyperHTTPError(Exception):
    """Base class for every hyperhttp-raised exception."""


# --- Transport / connection errors -----------------------------------------


class TransportError(HyperHTTPError):
    """Low-level transport failure (socket, TLS, framing)."""


class ConnectError(TransportError):
    """Could not establish a TCP connection to the remote host."""


class TLSError(TransportError):
    """TLS handshake or certificate validation failed."""


class ProtocolError(TransportError):
    """Malformed response or framing violation."""


class RemoteProtocolError(ProtocolError):
    """Remote peer sent something that violates the protocol we speak."""


class LocalProtocolError(ProtocolError):
    """We produced something invalid on the wire (bug in hyperhttp)."""


class ReadError(TransportError):
    """Socket read failed or returned unexpected EOF."""


class WriteError(TransportError):
    """Socket write failed."""


class DNSError(TransportError):
    """DNS resolution failed."""


class ProxyError(TransportError):
    """Failed to establish or use an HTTP proxy tunnel."""


# --- Timeouts --------------------------------------------------------------


class TimeoutException(HyperHTTPError):
    """Base for timeout variants."""


class ConnectTimeout(TimeoutException, ConnectError):
    """Timed out while connecting to the remote host."""


class ReadTimeout(TimeoutException, ReadError):
    """Timed out while reading a response."""


class WriteTimeout(TimeoutException, WriteError):
    """Timed out while writing a request."""


class PoolTimeout(TimeoutException):
    """Timed out waiting for a connection from the pool."""


# --- HTTP response errors --------------------------------------------------


class HTTPStatusError(HyperHTTPError):
    """Raised by ``Response.raise_for_status()`` for 4xx/5xx responses."""

    def __init__(self, message: str, *, request: "object", response: "Response") -> None:
        super().__init__(message)
        self.request = request
        self.response = response


# --- Redirects & URL-related ----------------------------------------------


class InvalidURL(HyperHTTPError):
    """URL could not be parsed or is unsuitable for a request."""


class TooManyRedirects(HyperHTTPError):
    """Exceeded the configured redirect limit."""


# --- Pool / circuit breaker -----------------------------------------------


class PoolClosed(HyperHTTPError):
    """Connection pool was closed while a request was pending."""


class CircuitBreakerOpen(HyperHTTPError):
    """Circuit breaker is open for the target host."""

    def __init__(self, host: str, remaining: float) -> None:
        super().__init__(f"Circuit breaker is OPEN for host {host} for {remaining:.1f}s more")
        self.host = host
        self.remaining = remaining


# --- Streaming state ------------------------------------------------------


class StreamError(HyperHTTPError):
    """Operation on a closed or already-consumed response stream."""


class StreamConsumed(StreamError):
    """Attempted to re-read a stream that has already been consumed."""


class ResponseClosed(StreamError):
    """Operation attempted on a closed response."""


# --- Resource limits ------------------------------------------------------


class ResponseTooLarge(HyperHTTPError):
    """Response body (decoded or raw) exceeded the configured size limit.

    Raised by the response stream when either the raw body or the decoded
    body crosses ``max_response_size`` / ``max_decompressed_size``. Treated
    as a protocol-layer error to short-circuit further reads; the caller
    can catch it to abort cleanly without OOMing.
    """


class DecompressionError(HyperHTTPError):
    """A decoder failed or was fed data that exceeded its configured cap.

    Sub-case of decompression-bomb protection: ``max_decompressed_size``
    was exceeded before the decoder finished producing output.
    """


__all__ = [
    "HyperHTTPError",
    "TransportError",
    "ConnectError",
    "TLSError",
    "ProtocolError",
    "RemoteProtocolError",
    "LocalProtocolError",
    "ReadError",
    "WriteError",
    "DNSError",
    "ProxyError",
    "TimeoutException",
    "ConnectTimeout",
    "ReadTimeout",
    "WriteTimeout",
    "PoolTimeout",
    "HTTPStatusError",
    "InvalidURL",
    "TooManyRedirects",
    "PoolClosed",
    "CircuitBreakerOpen",
    "StreamError",
    "StreamConsumed",
    "ResponseClosed",
    "ResponseTooLarge",
    "DecompressionError",
]
