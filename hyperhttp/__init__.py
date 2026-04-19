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
from hyperhttp._url import URL
from hyperhttp.client import Client, Request, Response, Timeout, __version__
from hyperhttp.cookies import Cookies
from hyperhttp.exceptions import (
    CircuitBreakerOpen,
    ConnectError,
    ConnectTimeout,
    DNSError,
    HTTPStatusError,
    HyperHTTPError,
    InvalidURL,
    PoolClosed,
    PoolTimeout,
    ProtocolError,
    ReadError,
    ReadTimeout,
    RemoteProtocolError,
    ResponseClosed,
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
]
