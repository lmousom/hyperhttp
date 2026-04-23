"""
``multipart/form-data`` encoder built for throughput.

Design goals:

- Never slurp a file into memory. Disk-backed parts stream in fixed chunks
  directly from ``file.read(n)`` to the socket, with no intermediate
  bytearray staging. A 1 GiB upload uses O(chunk) memory.
- Pre-compute ``Content-Length`` whenever every part's size is known, so the
  request goes out with Content-Length framing instead of chunked encoding
  (many servers reject or are slower on chunked uploads).
- Pre-render each part's header block exactly once at construction time;
  iteration just hands out already-computed ``bytes`` objects.
- Zero-copy yields: each ``bytes`` chunk produced by iteration is the exact
  object returned by the source (``file.read``, user ``bytes``, user
  ``memoryview``) — we never pay for a copy on the upload hot path.

Supported part sources:

- ``bytes`` / ``bytearray`` / ``memoryview`` — in-memory payload.
- ``os.PathLike`` / ``str`` path — streamed from disk, size from ``stat()``.
- Sync binary file handle (``open(path, "rb")``) — streamed in chunks.
- Async iterable of bytes — yielded as-is (size must be provided if you want
  Content-Length framing).

SOCKS / HTTPS / uvloop all work transparently; this encoder doesn't touch
sockets directly.
"""

from __future__ import annotations

import io
import mimetypes
import os
import secrets
import stat as _stat_mod
from collections.abc import Mapping as _AbcMapping
from typing import (
    Any,
    AsyncIterator,
    BinaryIO,
    Iterable,
    List,
    Optional,
    Tuple,
    Union,
)

from hyperhttp._validate import validate_multipart_param

__all__ = [
    "MultipartEncoder",
    "MultipartField",
    "MultipartFile",
]


# 256 KiB is a sweet spot for large-file uploads: large enough that the
# ``to_thread`` hop on each read is amortised across many kernel page-cache
# copies, small enough to stay well below the socket send buffer so
# ``drain()`` rarely has to block. Benchmarks show 64 KiB leaves ~35% on the
# table vs 256 KiB on 100 MiB uploads because of thread-hop overhead.
_DEFAULT_CHUNK = 1024 * 1024

_CRLF = b"\r\n"
_DASH_DASH = b"--"


# ---------------------------------------------------------------------------
# Header encoding helpers
# ---------------------------------------------------------------------------


def _is_ascii(value: str) -> bool:
    return all(ord(c) < 128 for c in value)


def _quote_header_value(value: str) -> str:
    """Quote a parameter value for a Content-Disposition header.

    Backslashes and double quotes are escaped per RFC 7230. Non-ASCII values
    are rejected here — the caller should use :func:`_format_filename` for
    filenames that may contain non-ASCII characters.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _format_filename(filename: str) -> str:
    """Render ``filename`` for ``Content-Disposition``.

    Uses the ASCII-safe ``filename="..."`` form when possible, otherwise
    emits both an ASCII fallback and an RFC 5987 ``filename*=UTF-8''...``
    form so modern servers get the correct Unicode filename.
    """
    if _is_ascii(filename):
        return f'filename="{_quote_header_value(filename)}"'
    from urllib.parse import quote

    ascii_fallback = filename.encode("ascii", "replace").decode("ascii")
    encoded = quote(filename, safe="")
    return (
        f'filename="{_quote_header_value(ascii_fallback)}"; '
        f"filename*=UTF-8''{encoded}"
    )


def _guess_content_type(filename: Optional[str]) -> Optional[str]:
    if not filename:
        return None
    ctype, _ = mimetypes.guess_type(filename)
    return ctype


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


class _Source:
    """Internal interface for anything that can yield bytes.

    Subclasses must provide ``size`` (``int`` or ``None``) and
    ``__aiter__``. Sources whose size is known allow Content-Length framing.
    """

    size: Optional[int]

    def __aiter__(self) -> AsyncIterator[bytes]:  # pragma: no cover - abstract
        raise NotImplementedError


class _BytesSource(_Source):
    __slots__ = ("_data", "size")

    def __init__(self, data: Union[bytes, bytearray, memoryview]) -> None:
        if isinstance(data, (bytearray, memoryview)):
            data = bytes(data)
        self._data = data
        self.size = len(data)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self._data:
            yield self._data


class _PathSource(_Source):
    """Stream a regular file from disk.

    The file is opened lazily inside ``__aiter__`` so the encoder can be
    reused for a retry without leaking a file descriptor.
    """

    __slots__ = ("_path", "size", "_chunk_size")

    def __init__(self, path: "os.PathLike[str] | str", chunk_size: int) -> None:
        self._path = os.fspath(path)
        self._chunk_size = chunk_size
        try:
            st = os.stat(self._path)
        except OSError as exc:
            raise ValueError(f"Cannot stat {self._path!r}: {exc}") from exc
        self.size = st.st_size

    async def __aiter__(self) -> AsyncIterator[bytes]:
        import asyncio

        fh = await asyncio.to_thread(open, self._path, "rb", buffering=0)
        try:
            _fadvise_sequential(fh)
            chunk = self._chunk_size
            while True:
                data = await asyncio.to_thread(fh.read, chunk)
                if not data:
                    break
                yield data
        finally:
            await asyncio.to_thread(fh.close)


class _FileHandleSource(_Source):
    """Stream an already-open binary file handle.

    Size is inferred from ``fstat`` on the first iteration when the handle
    has a ``fileno``; otherwise ``size`` is ``None`` (chunked framing).
    Single-use — we do not seek the handle back to 0 between iterations.
    """

    __slots__ = ("_fh", "size", "_chunk_size", "_consumed")

    def __init__(
        self,
        fh: BinaryIO,
        chunk_size: int,
        size: Optional[int] = None,
    ) -> None:
        self._fh = fh
        self._chunk_size = chunk_size
        self._consumed = False
        if size is not None:
            self.size = size
            return
        self.size = _stat_size_or_none(fh)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        import asyncio

        if self._consumed:
            raise RuntimeError(
                "MultipartEncoder: file handle source already consumed; "
                "pass a path or re-seek the handle to reuse it"
            )
        self._consumed = True
        chunk = self._chunk_size
        fh = self._fh
        while True:
            data = await asyncio.to_thread(fh.read, chunk)
            if not data:
                break
            yield data


class _AsyncIterableSource(_Source):
    """Yield bytes from a user-provided async iterable."""

    __slots__ = ("_iterable", "size", "_consumed")

    def __init__(self, iterable: Any, size: Optional[int]) -> None:
        self._iterable = iterable
        self.size = size
        self._consumed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self._consumed:
            raise RuntimeError(
                "MultipartEncoder: async iterable source already consumed"
            )
        self._consumed = True
        async for chunk in self._iterable:
            if not chunk:
                continue
            yield chunk if isinstance(chunk, bytes) else bytes(chunk)


def _stat_size_or_none(fh: Any) -> Optional[int]:
    try:
        fd = fh.fileno()
    except (AttributeError, OSError, io.UnsupportedOperation):
        pass
    else:
        try:
            st = os.fstat(fd)
            if _stat_mod.S_ISREG(st.st_mode):
                try:
                    pos = fh.tell()
                except (AttributeError, OSError, io.UnsupportedOperation):
                    pos = 0
                return max(st.st_size - pos, 0)
        except OSError:
            pass
    # BytesIO / StringIO etc.: try getbuffer().
    getbuf = getattr(fh, "getbuffer", None)
    if getbuf is not None:
        try:
            return len(getbuf()) - fh.tell()
        except Exception:
            return None
    return None


def _fadvise_sequential(fh: Any) -> None:
    """Hint the kernel for sequential read-ahead on Linux."""
    fadvise = getattr(os, "posix_fadvise", None)
    if fadvise is None:
        return
    try:
        fd = fh.fileno()
    except (AttributeError, OSError, io.UnsupportedOperation):
        return
    try:
        fadvise(fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)  # type: ignore[attr-defined]
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Field model
# ---------------------------------------------------------------------------


class MultipartFile:
    """Convenience constructor for a file-typed multipart part.

    Exactly one of ``content``, ``path``, or ``file`` must be provided.
    """

    __slots__ = ("content", "path", "file", "filename", "content_type", "size")

    def __init__(
        self,
        content: Any = None,
        *,
        path: Any = None,
        file: Any = None,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
        size: Optional[int] = None,
    ) -> None:
        given = sum(x is not None for x in (content, path, file))
        if given != 1:
            raise ValueError(
                "MultipartFile requires exactly one of content=, path=, file="
            )
        self.content = content
        self.path = path
        self.file = file
        self.filename = filename
        self.content_type = content_type
        self.size = size


class MultipartField:
    """A fully-prepared multipart part.

    Parts are self-contained: their header block is rendered exactly once
    at construction, and their ``total_size`` accounts for both the headers
    and the trailing CRLF that separates parts on the wire.
    """

    __slots__ = ("name", "filename", "content_type", "_source", "_header", "_total_size")

    def __init__(
        self,
        name: str,
        source: _Source,
        *,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
        boundary: bytes,
    ) -> None:
        # Every component that lands in the on-wire ``Content-Disposition``
        # line must be free of CR / LF / NUL — otherwise an attacker-controlled
        # field name, filename, or content-type smuggles MIME headers into the
        # body (confirmed exploitable against the pre-2.0.1 encoder).
        validate_multipart_param(name, field="field name")
        if filename is not None:
            validate_multipart_param(filename, field="filename")
        if content_type is not None:
            validate_multipart_param(content_type, field="content-type")

        self.name = name
        self.filename = filename
        self.content_type = content_type
        self._source = source

        lines: List[str] = [f"--{boundary.decode('ascii')}"]
        disposition = f'Content-Disposition: form-data; name="{_quote_header_value(name)}"'
        if filename is not None:
            disposition += f"; {_format_filename(filename)}"
        lines.append(disposition)
        if content_type is not None:
            lines.append(f"Content-Type: {content_type}")
        self._header = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")

        if source.size is None:
            self._total_size: Optional[int] = None
        else:
            # Header + content + trailing CRLF (before next boundary or terminator).
            self._total_size = len(self._header) + source.size + len(_CRLF)

    @property
    def header_bytes(self) -> bytes:
        return self._header

    @property
    def total_size(self) -> Optional[int]:
        return self._total_size

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._header
        async for chunk in self._source:
            yield chunk
        yield _CRLF


# ---------------------------------------------------------------------------
# Encoder
# ---------------------------------------------------------------------------


_FieldSpec = Union[
    str,
    bytes,
    bytearray,
    memoryview,
    "os.PathLike[str]",
    BinaryIO,
    MultipartFile,
    Tuple[Any, ...],  # (filename, content[, content_type])
]


class MultipartEncoder:
    """Async-iterable ``multipart/form-data`` body.

    The encoder yields the fully-framed body as raw ``bytes`` chunks. Pass
    it as the request body; pair it with
    ``Content-Type: multipart/form-data; boundary=<boundary>`` (available
    via :attr:`content_type`) and, if :attr:`content_length` is not
    ``None``, ``Content-Length: <n>``.

    When using ``hyperhttp.Client.post(files=..., data=...)`` the client
    sets these headers automatically.
    """

    def __init__(
        self,
        fields: Union[
            _AbcMapping,
            Iterable[Tuple[str, _FieldSpec]],
        ],
        *,
        boundary: Optional[str] = None,
        chunk_size: int = _DEFAULT_CHUNK,
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        self._chunk_size = chunk_size

        if boundary is None:
            boundary = _make_boundary()
        else:
            _validate_boundary(boundary)
        self._boundary = boundary.encode("ascii")

        field_iter: Iterable[Tuple[str, _FieldSpec]]
        if isinstance(fields, _AbcMapping):
            field_iter = list(fields.items())
        else:
            field_iter = list(fields)

        self._parts: List[MultipartField] = [
            _build_part(name, value, boundary=self._boundary, chunk_size=chunk_size)
            for name, value in field_iter
        ]

        # Terminator: "--<boundary>--\r\n".
        self._terminator = _DASH_DASH + self._boundary + _DASH_DASH + _CRLF

        # Content-Length: only if every part's size is known.
        total: Optional[int] = 0
        for part in self._parts:
            sz = part.total_size
            if sz is None:
                total = None
                break
            total += sz
        if total is not None:
            total += len(self._terminator)
        self._content_length = total

    @property
    def boundary(self) -> str:
        return self._boundary.decode("ascii")

    @property
    def content_type(self) -> str:
        return f"multipart/form-data; boundary={self.boundary}"

    @property
    def content_length(self) -> Optional[int]:
        return self._content_length

    def __len__(self) -> int:
        if self._content_length is None:
            raise TypeError(
                "MultipartEncoder has streaming parts with unknown size; "
                "Content-Length is not known ahead of time"
            )
        return self._content_length

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for part in self._parts:
            async for chunk in part:
                yield chunk
        yield self._terminator


# ---------------------------------------------------------------------------
# Field builders
# ---------------------------------------------------------------------------


def _make_boundary() -> str:
    """Return a random boundary token.

    ~22 URL-safe base64 characters = 128 bits of entropy. Collision with
    any inline bytes is astronomically unlikely; we don't scan the content.
    """
    return "----hyperhttp-" + secrets.token_urlsafe(16)


_BOUNDARY_ALLOWED = set(
    "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'()+_,-./:=?"
)


def _validate_boundary(boundary: str) -> None:
    # RFC 2046 §5.1.1: boundary is 1–70 chars from a restricted charset, no
    # trailing space.
    if not 1 <= len(boundary) <= 70:
        raise ValueError("multipart boundary must be 1–70 characters")
    if any(c not in _BOUNDARY_ALLOWED and c != " " for c in boundary):
        raise ValueError("multipart boundary contains an illegal character")
    if boundary.endswith(" "):
        raise ValueError("multipart boundary must not end with a space")


def _build_part(
    name: str,
    value: Any,
    *,
    boundary: bytes,
    chunk_size: int,
) -> MultipartField:
    if not isinstance(name, str) or not name:
        raise ValueError("multipart field name must be a non-empty string")

    # MultipartFile: explicit control.
    if isinstance(value, MultipartFile):
        source, filename, ctype = _source_from_file(value, chunk_size)
        return MultipartField(
            name, source, filename=filename, content_type=ctype, boundary=boundary
        )

    # Tuple shapes: (filename, content) or (filename, content, content_type).
    if isinstance(value, tuple):
        return _build_part_from_tuple(name, value, boundary=boundary, chunk_size=chunk_size)

    # Plain str → text field.
    if isinstance(value, str):
        source = _BytesSource(value.encode("utf-8"))
        return MultipartField(name, source, boundary=boundary)

    # Plain bytes-like → treat as anonymous part (no filename, no inferred ctype).
    if isinstance(value, (bytes, bytearray, memoryview)):
        return MultipartField(name, _BytesSource(value), boundary=boundary)

    # Path-like → streamed file, filename/content-type inferred.
    if isinstance(value, os.PathLike) or _is_probable_path(value):
        path_str = os.fspath(value) if isinstance(value, os.PathLike) else value
        source = _PathSource(path_str, chunk_size)
        filename = os.path.basename(str(path_str)) or None
        ctype = _guess_content_type(filename) or "application/octet-stream"
        return MultipartField(
            name, source, filename=filename, content_type=ctype, boundary=boundary
        )

    # File-like (has .read)?
    if hasattr(value, "read"):
        source = _FileHandleSource(value, chunk_size)
        filename = getattr(value, "name", None)
        if isinstance(filename, str):
            filename = os.path.basename(filename) or None
        else:
            filename = None
        ctype = _guess_content_type(filename) or "application/octet-stream"
        return MultipartField(
            name, source, filename=filename, content_type=ctype, boundary=boundary
        )

    # Async iterable? Treat as streaming with unknown size.
    if hasattr(value, "__aiter__"):
        return MultipartField(
            name, _AsyncIterableSource(value, None), boundary=boundary
        )

    raise TypeError(
        f"multipart field {name!r} has unsupported type {type(value).__name__}"
    )


def _build_part_from_tuple(
    name: str,
    value: Tuple[Any, ...],
    *,
    boundary: bytes,
    chunk_size: int,
) -> MultipartField:
    if len(value) not in (2, 3):
        raise ValueError(
            f"multipart field {name!r}: tuple must be "
            "(filename, content) or (filename, content, content_type)"
        )
    filename, content = value[0], value[1]
    ctype = value[2] if len(value) == 3 else None

    if filename is not None and not isinstance(filename, str):
        raise TypeError(
            f"multipart field {name!r}: filename must be str or None"
        )

    # Resolve the content into a source.
    if isinstance(content, (bytes, bytearray, memoryview)):
        source: _Source = _BytesSource(content)
    elif isinstance(content, str):
        source = _BytesSource(content.encode("utf-8"))
    elif isinstance(content, os.PathLike) or _is_probable_path(content):
        path_str = os.fspath(content) if isinstance(content, os.PathLike) else content
        source = _PathSource(path_str, chunk_size)
    elif hasattr(content, "read"):
        source = _FileHandleSource(content, chunk_size)
    elif hasattr(content, "__aiter__"):
        source = _AsyncIterableSource(content, None)
    else:
        raise TypeError(
            f"multipart field {name!r}: unsupported content type "
            f"{type(content).__name__}"
        )

    if ctype is None and filename is not None:
        ctype = _guess_content_type(filename) or "application/octet-stream"

    return MultipartField(
        name, source, filename=filename, content_type=ctype, boundary=boundary
    )


def _source_from_file(
    mf: MultipartFile, chunk_size: int
) -> Tuple[_Source, Optional[str], Optional[str]]:
    filename = mf.filename
    ctype = mf.content_type

    if mf.content is not None:
        data = mf.content
        if isinstance(data, str):
            data = data.encode("utf-8")
        if isinstance(data, (bytes, bytearray, memoryview)):
            source: _Source = _BytesSource(data)
        elif hasattr(data, "__aiter__"):
            source = _AsyncIterableSource(data, mf.size)
        else:
            raise TypeError(
                "MultipartFile.content must be bytes-like, str, or an async iterable"
            )
    elif mf.path is not None:
        source = _PathSource(mf.path, chunk_size)
        if filename is None:
            filename = os.path.basename(os.fspath(mf.path)) or None
    else:
        source = _FileHandleSource(mf.file, chunk_size, size=mf.size)
        if filename is None:
            name = getattr(mf.file, "name", None)
            if isinstance(name, str):
                filename = os.path.basename(name) or None

    if ctype is None:
        ctype = _guess_content_type(filename) or "application/octet-stream"
    return source, filename, ctype


def _is_probable_path(value: Any) -> bool:
    # A bare ``str`` is ambiguous: it might be a filename OR a text field.
    # We choose the less surprising behaviour: plain ``str`` is treated as a
    # text field. Callers who want path semantics should use ``pathlib.Path``
    # or the 2-/3-tuple form.
    return False
