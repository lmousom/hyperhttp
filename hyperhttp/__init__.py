"""
HyperHTTP — high-performance async HTTP/1.1 + HTTP/2 client.

Quick start:

    import asyncio
    import hyperhttp

    async def main():
        async with hyperhttp.Client() as client:
            response = await client.get("https://example.com")
            print(await response.aread())

    asyncio.run(main())

For maximum throughput, install the optional speed extras
(``pip install hyperhttp[speed]``) and call ``hyperhttp.install_uvloop()``
at the top of your program.
"""

from hyperhttp._compat import (
    HAS_BROTLI,
    HAS_H11,
    HAS_ORJSON,
    HAS_UVLOOP,
    HAS_ZSTANDARD,
    install_uvloop,
)
from hyperhttp._headers import Headers
from hyperhttp._multipart import MultipartEncoder, MultipartField, MultipartFile
from hyperhttp._proxy import ProxyURL
from hyperhttp._url import URL
from hyperhttp.auth import Auth, BasicAuth, BearerAuth, DigestAuth
from hyperhttp.client import Client, Request, Response, Timeout, __version__
from hyperhttp.connection.tls import InsecureRequestWarning
from hyperhttp.cookies import Cookies
from hyperhttp.mock import MockResponse, MockTransport, Router
from hyperhttp.exceptions import (
    CircuitBreakerOpen,
    ConnectError,
    ConnectTimeout,
    DecompressionError,
    DNSError,
    HTTPStatusError,
    HyperHTTPError,
    InvalidURL,
    LocalProtocolError,
    PoolClosed,
    PoolTimeout,
    ProtocolError,
    ProxyError,
    ReadError,
    ReadTimeout,
    RemoteProtocolError,
    ResponseClosed,
    ResponseTooLarge,
    StreamConsumed,
    StreamError,
    TimeoutException,
    TLSError,
    TooManyRedirects,
    TransportError,
    WriteError,
    WriteTimeout,
)

__all__ = [
    "__version__",
    "Client",
    "Request",
    "Response",
    "Timeout",
    "Headers",
    "URL",
    "ProxyURL",
    "MultipartEncoder",
    "MultipartField",
    "MultipartFile",
    "Auth",
    "BasicAuth",
    "BearerAuth",
    "DigestAuth",
    "MockResponse",
    "MockTransport",
    "Router",
    "Cookies",
    "install_uvloop",
    "HAS_ORJSON",
    "HAS_UVLOOP",
    "HAS_H11",
    "HAS_BROTLI",
    "HAS_ZSTANDARD",
    # Exceptions
    "HyperHTTPError",
    "TransportError",
    "ConnectError",
    "TLSError",
    "ProtocolError",
    "RemoteProtocolError",
    "ReadError",
    "WriteError",
    "DNSError",
    "ProxyError",
    "TimeoutException",
    "ConnectTimeout",
    "ReadTimeout",
    "WriteTimeout",
    "PoolTimeout",
    "PoolClosed",
    "HTTPStatusError",
    "InvalidURL",
    "TooManyRedirects",
    "CircuitBreakerOpen",
    "StreamError",
    "StreamConsumed",
    "ResponseClosed",
    "ResponseTooLarge",
    "DecompressionError",
    "LocalProtocolError",
    "InsecureRequestWarning",
]
