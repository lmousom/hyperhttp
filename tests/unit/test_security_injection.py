"""
CRLF / NUL / token-grammar validation at every construction point that writes
to the wire. These tests lock in the fix for hyperhttp's pre-2.0.1 request
smuggling / header injection vector.
"""

from __future__ import annotations

import asyncio

import pytest

import hyperhttp
from hyperhttp._headers import Headers
from hyperhttp._multipart import MultipartEncoder, MultipartField, MultipartFile
from hyperhttp._url import URL
from hyperhttp._validate import (
    validate_header_name,
    validate_header_value,
    validate_method,
    validate_target,
)
from hyperhttp.exceptions import InvalidURL, LocalProtocolError
from hyperhttp.protocol.h1 import build_request_head


# ---------------------------------------------------------------------------
# Pure validator unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["X-Foo", "accept", "Content-Length", "x"])
def test_validate_header_name_accepts_tokens(name: str) -> None:
    validate_header_name(name)  # no raise


@pytest.mark.parametrize(
    "name",
    [
        "",
        "X Foo",  # space
        "X:Foo",  # colon
        "X\r\nY",  # CRLF
        "X\tY",  # tab
        "Héllo",  # non-ASCII
        "X\0Y",  # NUL
    ],
)
def test_validate_header_name_rejects_non_tokens(name: str) -> None:
    with pytest.raises(LocalProtocolError):
        validate_header_name(name)


@pytest.mark.parametrize("value", ["bar", "multi word value", "encoded=%20"])
def test_validate_header_value_accepts_clean(value: str) -> None:
    validate_header_value(value)


@pytest.mark.parametrize(
    "value", ["bad\rheader", "bad\nheader", "bad\r\nheader", "nul\0byte"]
)
def test_validate_header_value_rejects_framing_chars(value: str) -> None:
    with pytest.raises(LocalProtocolError):
        validate_header_value(value)


@pytest.mark.parametrize("method", ["GET", "POST", "PURGE", "M-SEARCH"])
def test_validate_method_accepts_tokens(method: str) -> None:
    validate_method(method)


@pytest.mark.parametrize("method", ["", "GET\r\n", "GE T", "GET/1"])
def test_validate_method_rejects_bad(method: str) -> None:
    with pytest.raises(LocalProtocolError):
        validate_method(method)


@pytest.mark.parametrize("target", ["/", "/a/b?c=d", "*", "/x%20y"])
def test_validate_target_accepts(target: str) -> None:
    validate_target(target)


@pytest.mark.parametrize(
    "target", ["", "/a b", "/x\r\ny", "/x\ny", "/\0"]
)
def test_validate_target_rejects(target: str) -> None:
    with pytest.raises(LocalProtocolError):
        validate_target(target)


# ---------------------------------------------------------------------------
# Headers: insertion validation
# ---------------------------------------------------------------------------


def test_headers_reject_crlf_in_value() -> None:
    with pytest.raises(LocalProtocolError):
        Headers([("X-Foo", "bar\r\nInjected: 1")])


def test_headers_reject_invalid_name() -> None:
    with pytest.raises(LocalProtocolError):
        Headers([("X Bad", "ok")])


def test_headers_set_validates() -> None:
    h = Headers()
    with pytest.raises(LocalProtocolError):
        h["X-Foo"] = "bad\nheader"


def test_headers_update_validates() -> None:
    h = Headers()
    with pytest.raises(LocalProtocolError):
        h.update({"X-Foo": "bad\rheader"})


# ---------------------------------------------------------------------------
# build_request_head: catch everything at the wire boundary
# ---------------------------------------------------------------------------


def test_build_request_head_rejects_crlf_method() -> None:
    with pytest.raises(LocalProtocolError):
        build_request_head("GET\r\nEvil", "/", "example.com", Headers())


def test_build_request_head_rejects_crlf_target() -> None:
    with pytest.raises(LocalProtocolError):
        build_request_head("GET", "/x\r\nEvil: 1", "example.com", Headers())


def test_build_request_head_rejects_crlf_host() -> None:
    with pytest.raises(LocalProtocolError):
        build_request_head("GET", "/", "example.com\r\nEvil: 1", Headers())


def test_build_request_head_happy_path() -> None:
    out = build_request_head(
        "GET",
        "/hello?a=1",
        "example.com",
        Headers([("Accept", "*/*")]),
    )
    assert out.startswith(b"GET /hello?a=1 HTTP/1.1\r\n")
    assert b"Host: example.com\r\n" in out


# ---------------------------------------------------------------------------
# URL: control-char rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "http://example.com/\r\nGET /evil HTTP/1.1",
        "http://example.com\r\nX-Smuggle:1/",
        "http://example.com/\n",
        "http://example.com/\t",
        "http://example.com/\0",
    ],
)
def test_url_rejects_control_chars(raw: str) -> None:
    with pytest.raises(InvalidURL):
        URL(raw)


# ---------------------------------------------------------------------------
# Multipart: CRLF in field name / filename / content-type
# ---------------------------------------------------------------------------


def test_multipart_rejects_crlf_in_field_name() -> None:
    with pytest.raises(LocalProtocolError):
        MultipartEncoder([("evil\r\nInjected: hdr", b"data")])


def test_multipart_rejects_crlf_in_filename() -> None:
    with pytest.raises(LocalProtocolError):
        MultipartEncoder(
            [("f", MultipartFile(content=b"data", filename="x\r\ny.txt"))]
        )


def test_multipart_rejects_crlf_in_content_type() -> None:
    with pytest.raises(LocalProtocolError):
        MultipartEncoder(
            [
                (
                    "f",
                    MultipartFile(
                        content=b"data",
                        filename="x.txt",
                        content_type="text/plain\r\nX: 1",
                    ),
                )
            ]
        )


def test_multipart_field_ctor_validates_eagerly() -> None:
    from hyperhttp._multipart import _BytesSource

    with pytest.raises(LocalProtocolError):
        MultipartField(
            name="bad\r\nname",
            source=_BytesSource(b"x"),
            boundary=b"b",
        )


# ---------------------------------------------------------------------------
# End-to-end: Client.request() can't be used to smuggle headers
# ---------------------------------------------------------------------------


async def test_client_rejects_crlf_header_at_request_time() -> None:
    async def handler(request):
        return hyperhttp.MockResponse(200)

    async with hyperhttp.Client(
        transport=hyperhttp.MockTransport(handler)
    ) as c:
        with pytest.raises(LocalProtocolError):
            await c.get(
                "https://x/y",
                headers={"X-Foo": "bar\r\nEvil: injected"},
            )


async def test_client_rejects_crlf_in_url() -> None:
    async def handler(request):
        return hyperhttp.MockResponse(200)

    async with hyperhttp.Client(
        transport=hyperhttp.MockTransport(handler)
    ) as c:
        with pytest.raises(InvalidURL):
            await c.get("https://x/path\r\nEvil")


async def test_client_multipart_rejects_crlf_field_name() -> None:
    async def handler(request):
        return hyperhttp.MockResponse(200)

    async with hyperhttp.Client(
        transport=hyperhttp.MockTransport(handler)
    ) as c:
        with pytest.raises(LocalProtocolError):
            await c.post(
                "https://x/y",
                files={"evil\r\nInjected: header": b"data"},
            )


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(
        test_client_rejects_crlf_header_at_request_time()  # type: ignore[arg-type]
    )
