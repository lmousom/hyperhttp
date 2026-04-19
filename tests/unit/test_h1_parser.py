"""
Unit-level tests for the HTTP/1.1 response parser.
"""

import pytest

from hyperhttp.exceptions import RemoteProtocolError
from hyperhttp.protocol.h1 import (
    ResponseHead,
    _PyParser,
    build_request_head,
    make_parser,
)


def _run(parser, data: bytes):
    return list(parser.feed(data))


def test_pure_py_parser_selected_by_default():
    # Regardless of whether h11 is installed, the default should be _PyParser
    # unless HYPERHTTP_USE_H11=1.
    p = make_parser()
    assert isinstance(p, _PyParser)


def test_build_request_head_adds_host_and_content_length():
    from hyperhttp._headers import Headers

    out = build_request_head("GET", "/", "example.com", Headers(), content_length=5)
    assert out.startswith(b"GET / HTTP/1.1\r\n")
    assert b"Host: example.com\r\n" in out
    assert b"Content-Length: 5\r\n" in out
    assert out.endswith(b"\r\n\r\n")


def test_build_request_head_chunked_transfer_encoding():
    from hyperhttp._headers import Headers

    out = build_request_head("POST", "/", "x", Headers(), chunked=True)
    assert b"Transfer-Encoding: chunked\r\n" in out
    assert b"Content-Length" not in out


def test_build_request_head_preserves_user_host_header():
    from hyperhttp._headers import Headers

    h = Headers({"Host": "override"})
    out = build_request_head("GET", "/", "example.com", h)
    assert out.count(b"Host: ") == 1
    assert b"Host: override\r\n" in out


def test_parse_content_length_response():
    p = _PyParser()
    events = _run(p, b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello")
    assert isinstance(events[0], ResponseHead)
    assert events[0].status_code == 200
    assert events[0].http_version == "HTTP/1.1"
    assert events[1] == b"hello"
    assert events[2] is None
    assert p.keep_alive


def test_parse_chunked_with_trailers():
    p = _PyParser()
    data = (
        b"HTTP/1.1 200 OK\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n"
        b"5\r\nhello\r\n"
        b"6\r\n world\r\n"
        b"0\r\n"
        b"X-Trailer: yes\r\n\r\n"
    )
    events = _run(p, data)
    assert isinstance(events[0], ResponseHead)
    chunks = [e for e in events if isinstance(e, bytes)]
    assert b"".join(chunks) == b"hello world"
    assert events[-1] is None


def test_parse_chunked_without_trailers():
    p = _PyParser()
    data = (
        b"HTTP/1.1 200 OK\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n"
        b"3\r\nabc\r\n"
        b"0\r\n\r\n"
    )
    events = _run(p, data)
    chunks = [e for e in events if isinstance(e, bytes)]
    assert b"".join(chunks) == b"abc"


def test_parse_chunked_with_extension_is_stripped():
    p = _PyParser()
    data = (
        b"HTTP/1.1 200 OK\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n"
        b"3;name=foo\r\nabc\r\n"
        b"0\r\n\r\n"
    )
    events = _run(p, data)
    chunks = [e for e in events if isinstance(e, bytes)]
    assert b"".join(chunks) == b"abc"


def test_missing_crlf_after_chunk_rejected():
    p = _PyParser()
    bad = (
        b"HTTP/1.1 200 OK\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n"
        b"3\r\nabcXX"
    )
    with pytest.raises(RemoteProtocolError):
        list(p.feed(bad))


def test_negative_chunk_size_rejected():
    p = _PyParser()
    bad = (
        b"HTTP/1.1 200 OK\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n"
        b"-1\r\n"
    )
    with pytest.raises(RemoteProtocolError):
        list(p.feed(bad))


def test_reject_cl_and_te_conflict():
    p = _PyParser()
    bad = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Length: 5\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n"
    )
    with pytest.raises(RemoteProtocolError):
        list(p.feed(bad))


def test_reject_multiple_differing_cl():
    p = _PyParser()
    bad = (
        b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nContent-Length: 6\r\n\r\n"
    )
    with pytest.raises(RemoteProtocolError):
        list(p.feed(bad))


def test_accept_duplicate_identical_cl():
    p = _PyParser()
    ok = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nContent-Length: 5\r\n\r\nhello"
    events = list(p.feed(ok))
    assert b"hello" in events


def test_negative_cl_rejected():
    p = _PyParser()
    with pytest.raises(RemoteProtocolError):
        list(p.feed(b"HTTP/1.1 200 OK\r\nContent-Length: -1\r\n\r\n"))


def test_invalid_cl_value_rejected():
    p = _PyParser()
    with pytest.raises(RemoteProtocolError):
        list(p.feed(b"HTTP/1.1 200 OK\r\nContent-Length: abc\r\n\r\n"))


def test_204_has_no_body_and_stays_keep_alive():
    p = _PyParser()
    events = list(p.feed(b"HTTP/1.1 204 No Content\r\n\r\n"))
    assert isinstance(events[0], ResponseHead)
    assert events[0].status_code == 204
    assert events[1] is None
    assert p.keep_alive


def test_304_has_no_body():
    p = _PyParser()
    events = list(p.feed(b"HTTP/1.1 304 Not Modified\r\nETag: abc\r\n\r\n"))
    assert events[0].status_code == 304
    assert events[-1] is None


def test_informational_1xx_has_no_body():
    p = _PyParser()
    events = list(p.feed(b"HTTP/1.1 103 Early Hints\r\n\r\n"))
    assert events[0].status_code == 103
    assert events[-1] is None


def test_connection_close_disables_keep_alive():
    p = _PyParser()
    list(p.feed(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"))
    assert not p.keep_alive


def test_body_read_to_close_when_no_framing():
    p = _PyParser()
    events = list(p.feed(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\npart1"))
    # No CL + no TE → read to EOF. "part1" should appear as data.
    assert any(isinstance(e, bytes) and e == b"part1" for e in events)
    # Feed EOF to close out.
    tail = list(p.feed_eof())
    assert tail[-1] is None


def test_malformed_status_line():
    p = _PyParser()
    with pytest.raises(RemoteProtocolError):
        list(p.feed(b"NOTHTTP garbage\r\n\r\n"))


def test_malformed_header():
    p = _PyParser()
    with pytest.raises(RemoteProtocolError):
        list(p.feed(b"HTTP/1.1 200 OK\r\nbadheader\r\n\r\n"))


def test_obsolete_line_folding_rejected():
    p = _PyParser()
    bad = (
        b"HTTP/1.1 200 OK\r\n"
        b"X-Thing: one\r\n"
        b" two\r\n"
        b"\r\n"
    )
    with pytest.raises(RemoteProtocolError):
        list(p.feed(bad))


def test_eof_before_any_bytes_errors():
    p = _PyParser()
    with pytest.raises(RemoteProtocolError):
        list(p.feed_eof())


def test_eof_mid_body_errors():
    p = _PyParser()
    list(p.feed(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nabc"))
    with pytest.raises(RemoteProtocolError):
        list(p.feed_eof())


def test_mark_no_body_for_head_requests():
    p = _PyParser()
    p.mark_no_body()
    # After mark_no_body, no further data should be parsed.
    events = list(p.feed(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello"))
    assert events == []


def test_feed_split_across_boundaries():
    p = _PyParser()
    # Head arrives in two pieces, body in two more.
    list(p.feed(b"HTTP/1.1 200 OK\r\nContent-Length:"))
    list(p.feed(b" 5\r\n\r\nhe"))
    rest = list(p.feed(b"llo"))
    chunks = [e for e in rest if isinstance(e, bytes)]
    assert b"".join(chunks).endswith(b"llo")
