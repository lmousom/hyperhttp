"""
HTTP/1.1 wire protocol.

Two parser implementations share a common interface (``H1Parser``):

- ``_H11Parser`` (default if ``h11`` is installed) wraps the h11 state
  machine, which is the most rigorously tested HTTP/1 parser in Python.
- ``_PyParser`` (fallback) is a pure-Python state machine using
  ``bytearray``/``memoryview`` with no regex. It is strict about framing
  (CL+TE conflict, negative CL, multiple differing CL) and preserves
  duplicate headers via ``Headers``.

Both parsers produce the same outputs: a ``ResponseHead`` object followed by
body ``bytes`` chunks and finally ``b""`` to signal end-of-body.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Tuple, Union

import os

from hyperhttp._compat import HAS_H11, h11
from hyperhttp._headers import Headers
from hyperhttp._validate import (
    validate_header_name,
    validate_header_value,
    validate_method,
    validate_target,
)
from hyperhttp.exceptions import LocalProtocolError, RemoteProtocolError

# The pure-Python parser is ~2x faster than h11 on the client hot path and
# is a strict HTTP/1.1 response parser (CL/TE conflict rejection, multiple-CL
# rejection, chunk trailers, etc). h11 remains available for users who want
# its extensively-fuzzed state machine — opt in via HYPERHTTP_USE_H11=1.
_USE_H11 = os.environ.get("HYPERHTTP_USE_H11") == "1"

__all__ = [
    "H1Parser",
    "ResponseHead",
    "build_request_head",
    "make_parser",
]


@dataclass
class ResponseHead:
    http_version: str
    status_code: int
    reason: str
    headers: Headers = field(default_factory=Headers)


# ---------------------------------------------------------------------------
# Request framing
# ---------------------------------------------------------------------------


def build_request_head(
    method: str,
    target: str,
    host: str,
    headers: Headers,
    *,
    content_length: Optional[int] = None,
    chunked: bool = False,
) -> bytes:
    """Build the HTTP/1.1 request head (request line + headers + CRLF).

    Every field written to the wire is validated here as a defence-in-depth
    check: ``Headers`` already validates at insertion, but the method, target,
    and Host value flow in from the URL/caller directly. Rejecting CR/LF/NUL
    makes HTTP request smuggling / header injection impossible regardless of
    what the caller does with user-supplied strings.
    """
    validate_method(method)
    validate_target(target)
    validate_header_value(host)

    parts: List[str] = [f"{method} {target} HTTP/1.1\r\n"]

    have_host = False
    have_cl = False
    have_te = False
    for name, value in headers.items():
        # Header names/values were validated at insertion (see Headers._append),
        # but revalidate on the way out so a ``copy()`` that bypassed _append
        # cannot smuggle CRLF back in.
        validate_header_name(name)
        validate_header_value(value)
        lname = name.lower()
        if lname == "host":
            have_host = True
        elif lname == "content-length":
            have_cl = True
        elif lname == "transfer-encoding":
            have_te = True
        parts.append(f"{name}: {value}\r\n")

    if not have_host:
        parts.append(f"Host: {host}\r\n")
    if chunked and not have_te:
        parts.append("Transfer-Encoding: chunked\r\n")
    elif content_length is not None and not have_cl and not chunked:
        parts.append(f"Content-Length: {content_length}\r\n")

    parts.append("\r\n")
    return "".join(parts).encode("latin-1")


# ---------------------------------------------------------------------------
# Parser interface
# ---------------------------------------------------------------------------


ParserEvent = Union[ResponseHead, bytes, None]
# A parser emits:
#   - exactly one ResponseHead
#   - zero or more bytes (body chunks; may be empty if between chunks)
#   - a final ``None`` sentinel meaning "end of response"
#   - an empty ``b""`` bytes object is NOT used as the sentinel; we use None.


class H1Parser:
    """Base interface for HTTP/1 parsers."""

    def feed(self, data: bytes) -> Iterator[ParserEvent]:  # pragma: no cover - iface
        raise NotImplementedError

    def feed_eof(self) -> Iterator[ParserEvent]:  # pragma: no cover - iface
        raise NotImplementedError

    @property
    def keep_alive(self) -> bool:
        raise NotImplementedError

    @property
    def their_state(self) -> str:
        raise NotImplementedError


def make_parser() -> H1Parser:
    if _USE_H11 and HAS_H11:
        return _H11Parser()
    return _PyParser()


# ---------------------------------------------------------------------------
# h11 adapter
# ---------------------------------------------------------------------------


class _H11Parser(H1Parser):
    def __init__(self) -> None:
        assert h11 is not None
        # ``our_role`` is CLIENT; h11 then parses SERVER → CLIENT responses.
        self._conn = h11.Connection(our_role=h11.CLIENT)
        self._saw_head = False
        self._done = False

    def feed(self, data: bytes) -> Iterator[ParserEvent]:
        if self._done:
            return
        self._conn.receive_data(data)
        yield from self._drain()

    def feed_eof(self) -> Iterator[ParserEvent]:
        if self._done:
            return
        self._conn.receive_data(b"")
        yield from self._drain()

    def _drain(self) -> Iterator[ParserEvent]:
        assert h11 is not None
        while True:
            try:
                event = self._conn.next_event()
            except h11.RemoteProtocolError as exc:
                raise RemoteProtocolError(str(exc)) from exc
            except h11.LocalProtocolError as exc:
                raise LocalProtocolError(str(exc)) from exc

            if event is h11.NEED_DATA:
                return
            if event is h11.PAUSED:
                return
            if isinstance(event, h11.Response):
                hdrs = Headers([(k.decode("ascii"), v.decode("latin-1")) for k, v in event.headers])
                http_version = "HTTP/" + event.http_version.decode("ascii")
                reason = event.reason.decode("latin-1") if event.reason else ""
                yield ResponseHead(
                    http_version=http_version,
                    status_code=event.status_code,
                    reason=reason,
                    headers=hdrs,
                )
            elif isinstance(event, h11.InformationalResponse):
                # 1xx — ignore; the real response will follow.
                continue
            elif isinstance(event, h11.Data):
                # h11 already returns ``bytes``; don't copy.
                yield event.data
            elif isinstance(event, h11.EndOfMessage):
                self._done = True
                yield None
                return
            elif isinstance(event, h11.ConnectionClosed):
                self._done = True
                yield None
                return

    @property
    def keep_alive(self) -> bool:
        assert h11 is not None
        if not self._done:
            return False
        # We frame the request ourselves rather than driving h11's send side,
        # so ``our_state`` stays at IDLE. Trust ``their_state`` for keep-alive.
        return self._conn.their_state is h11.DONE

    @property
    def their_state(self) -> str:
        assert h11 is not None
        return str(self._conn.their_state)


# ---------------------------------------------------------------------------
# Pure-Python fallback parser
# ---------------------------------------------------------------------------


class _PyParser(H1Parser):
    """Strict, bytearray-backed HTTP/1 response parser."""

    # State constants
    _S_STATUS = 0
    _S_HEADERS = 1
    _S_BODY_CL = 2
    _S_BODY_CHUNK_SIZE = 3
    _S_BODY_CHUNK_DATA = 4
    _S_BODY_CHUNK_TRAILER = 5
    _S_BODY_EOF = 6
    _S_DONE = 7

    def __init__(self) -> None:
        self._buf = bytearray()
        self._state = self._S_STATUS
        self._head: Optional[ResponseHead] = None
        self._keep_alive = True
        self._content_length = 0
        self._cl_remaining = 0
        self._chunk_remaining = 0
        self._is_head_response = False  # set externally if needed
        self._done = False

    # We need an external signal for HEAD / 204 / 304 to zero out the body.
    def mark_no_body(self) -> None:
        self._state = self._S_DONE
        self._done = True

    def feed(self, data: bytes) -> Iterator[ParserEvent]:
        if data:
            # Fast path: already in the CL body state with nothing pending
            # means we can hand the incoming bytes out directly, avoiding a
            # bytearray.extend + slice copy cycle.
            if self._state == self._S_BODY_CL and not self._buf:
                dlen = len(data)
                remaining = self._cl_remaining
                if dlen <= remaining:
                    self._cl_remaining = remaining - dlen
                    yield data
                    if self._cl_remaining == 0:
                        self._state = self._S_DONE
                        self._done = True
                        yield None
                    return
                # data has body tail + possibly pipelined next-response bytes;
                # fall through to the generic pump.
                self._buf.extend(data)
                yield from self._pump()
                return
            self._buf.extend(data)
        yield from self._pump()

    def feed_eof(self) -> Iterator[ParserEvent]:
        if self._state == self._S_BODY_EOF:
            # EOF marks end of body when framing is connection-close.
            self._state = self._S_DONE
            self._done = True
            yield None
            return
        if self._state == self._S_DONE:
            return
        if self._state == self._S_STATUS and not self._buf:
            # Server closed before sending anything.
            raise RemoteProtocolError("Connection closed before any response was received")
        raise RemoteProtocolError("Connection closed before response was complete")

    # -- inner machinery ----------------------------------------------------

    def _pump(self) -> Iterator[ParserEvent]:
        while True:
            if self._state == self._S_STATUS:
                advanced = self._parse_status_line()
                if not advanced:
                    return
            elif self._state == self._S_HEADERS:
                ev = self._parse_headers()
                if ev is None:
                    return
                yield ev
                # No-body responses transition straight to DONE; emit the
                # end-of-message sentinel so the caller notices.
                if self._state == self._S_DONE:
                    yield None
                    return
            elif self._state == self._S_BODY_CL:
                if self._cl_remaining == 0:
                    self._state = self._S_DONE
                    self._done = True
                    yield None
                    return
                buf_len = len(self._buf)
                if not buf_len:
                    return
                if buf_len <= self._cl_remaining:
                    # Hand the whole pending buffer off without a copy.
                    chunk = bytes(self._buf)
                    self._buf.clear()
                    self._cl_remaining -= buf_len
                    yield chunk
                else:
                    take = self._cl_remaining
                    chunk = bytes(self._buf[:take])
                    del self._buf[:take]
                    self._cl_remaining = 0
                    yield chunk
            elif self._state == self._S_BODY_CHUNK_SIZE:
                line_end = self._buf.find(b"\r\n")
                if line_end < 0:
                    return
                line = bytes(self._buf[:line_end])
                del self._buf[: line_end + 2]
                # strip chunk extensions
                semi = line.find(b";")
                raw = line[:semi] if semi >= 0 else line
                try:
                    size = int(raw.strip(), 16)
                except ValueError as exc:
                    raise RemoteProtocolError(f"Malformed chunk size: {raw!r}") from exc
                if size < 0:
                    raise RemoteProtocolError("Negative chunk size")
                self._chunk_remaining = size
                if size == 0:
                    self._state = self._S_BODY_CHUNK_TRAILER
                else:
                    self._state = self._S_BODY_CHUNK_DATA
            elif self._state == self._S_BODY_CHUNK_DATA:
                need = self._chunk_remaining
                if len(self._buf) < need + 2:
                    # Wait until we have the whole chunk + trailing CRLF.
                    return
                chunk = bytes(self._buf[:need])
                # Validate trailing CRLF
                if self._buf[need : need + 2] != b"\r\n":
                    raise RemoteProtocolError("Missing CRLF after chunk")
                del self._buf[: need + 2]
                self._chunk_remaining = 0
                self._state = self._S_BODY_CHUNK_SIZE
                yield chunk
            elif self._state == self._S_BODY_CHUNK_TRAILER:
                # Optional trailers: skip until blank line.
                idx = self._buf.find(b"\r\n\r\n")
                if idx < 0:
                    # Maybe we have just CRLF (no trailers)?
                    if self._buf.startswith(b"\r\n"):
                        del self._buf[:2]
                        self._state = self._S_DONE
                        self._done = True
                        yield None
                        return
                    return
                del self._buf[: idx + 4]
                self._state = self._S_DONE
                self._done = True
                yield None
                return
            elif self._state == self._S_BODY_EOF:
                if not self._buf:
                    return
                chunk = bytes(self._buf)
                self._buf.clear()
                yield chunk
            elif self._state == self._S_DONE:
                return

    def _parse_status_line(self) -> bool:
        idx = self._buf.find(b"\r\n")
        if idx < 0:
            return False
        line = bytes(self._buf[:idx])
        del self._buf[: idx + 2]
        # Handle leading empty lines, as RFC allows before the status line.
        if not line:
            return True
        try:
            version, rest = line.split(b" ", 1)
            status_str, _, reason_b = rest.partition(b" ")
            status_code = int(status_str)
        except (ValueError, IndexError) as exc:
            raise RemoteProtocolError(f"Malformed status line: {line!r}") from exc
        if not version.startswith(b"HTTP/"):
            raise RemoteProtocolError(f"Malformed HTTP version: {version!r}")
        http_version = version.decode("ascii")
        reason = reason_b.decode("latin-1") if reason_b else ""
        self._head = ResponseHead(
            http_version=http_version,
            status_code=status_code,
            reason=reason,
        )
        self._state = self._S_HEADERS
        return True

    def _parse_headers(self) -> Optional[ResponseHead]:
        # We need the full header block, terminated by a blank line.
        end = self._buf.find(b"\r\n\r\n")
        if end < 0:
            # Maybe empty header block?
            if self._buf.startswith(b"\r\n"):
                del self._buf[:2]
                assert self._head is not None
                return self._finalize_head()
            return None
        header_block = bytes(self._buf[:end])
        del self._buf[: end + 4]

        assert self._head is not None
        headers = self._head.headers

        for raw_line in header_block.split(b"\r\n"):
            if not raw_line:
                continue
            if raw_line[0:1] in (b" ", b"\t"):
                # Obsolete header line folding — reject per RFC 7230.
                raise RemoteProtocolError("Obsolete header line folding is not supported")
            colon = raw_line.find(b":")
            if colon <= 0:
                raise RemoteProtocolError(f"Malformed header: {raw_line!r}")
            name = raw_line[:colon].decode("ascii")
            value = raw_line[colon + 1 :].strip().decode("latin-1")
            if not name or any(c in name for c in " \t"):
                raise RemoteProtocolError(f"Invalid header name: {name!r}")
            headers.add(name, value)

        return self._finalize_head()

    def _finalize_head(self) -> ResponseHead:
        assert self._head is not None
        headers = self._head.headers
        status = self._head.status_code

        # 1xx / 204 / 304 / HEAD responses carry no body. Caller can also
        # mark the parser externally via ``mark_no_body`` (e.g. for HEAD
        # requests) before feeding data.
        is_no_body_status = status == 204 or status == 304 or (100 <= status < 200)

        cl_values = headers.get_list("content-length")
        te_value = headers.get("transfer-encoding", "")
        te = te_value.lower() if te_value else ""
        has_chunked = "chunked" in te

        # Framing conflict: CL + TE:chunked must be rejected.
        if cl_values and has_chunked:
            raise RemoteProtocolError(
                "Conflicting Content-Length and Transfer-Encoding: chunked"
            )

        # Multiple differing CLs are a framing attack.
        cl_int: Optional[int] = None
        if cl_values:
            normalized = {v.strip() for v in cl_values}
            if len(normalized) > 1:
                raise RemoteProtocolError("Multiple differing Content-Length headers")
            try:
                cl_int = int(next(iter(normalized)))
            except ValueError as exc:
                raise RemoteProtocolError("Invalid Content-Length header") from exc
            if cl_int < 0:
                raise RemoteProtocolError("Negative Content-Length")

        conn_tokens = {
            tok.strip().lower()
            for tok in headers.get("connection", "").split(",")
            if tok.strip()
        }
        # HTTP/1.1 keep-alive by default unless Connection: close.
        self._keep_alive = "close" not in conn_tokens

        head = self._head
        # Decide body framing.
        if is_no_body_status:
            self._state = self._S_DONE
            self._done = True
        elif has_chunked:
            self._state = self._S_BODY_CHUNK_SIZE
        elif cl_int is not None:
            self._content_length = cl_int
            self._cl_remaining = cl_int
            self._state = self._S_BODY_CL
        else:
            # No framing info → read until connection close.
            self._state = self._S_BODY_EOF
            self._keep_alive = False
        return head

    @property
    def keep_alive(self) -> bool:
        return self._keep_alive and self._done

    @property
    def their_state(self) -> str:
        return {
            self._S_STATUS: "STATUS",
            self._S_HEADERS: "HEADERS",
            self._S_BODY_CL: "BODY_CL",
            self._S_BODY_CHUNK_SIZE: "BODY_CHUNK_SIZE",
            self._S_BODY_CHUNK_DATA: "BODY_CHUNK_DATA",
            self._S_BODY_CHUNK_TRAILER: "BODY_TRAILER",
            self._S_BODY_EOF: "BODY_EOF",
            self._S_DONE: "DONE",
        }[self._state]
