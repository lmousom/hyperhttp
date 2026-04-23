"""Unit tests for the multipart/form-data encoder."""

from __future__ import annotations

import io
import os
from email.parser import BytesParser
from email.policy import default as _email_default

import pytest

from hyperhttp import MultipartEncoder, MultipartFile


async def _collect(encoder: MultipartEncoder) -> bytes:
    chunks = []
    async for chunk in encoder:
        chunks.append(chunk)
    return b"".join(chunks)


def _parse(body: bytes, content_type: str) -> list:
    """Parse a multipart body into a list of (name, filename, ctype, content)."""
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
    msg = BytesParser(policy=_email_default).parsebytes(header + body)
    out = []
    for part in msg.iter_parts():
        disp = part.get("Content-Disposition", "")
        name = _param(disp, "name")
        filename = _param(disp, "filename")
        out.append((name, filename, part.get_content_type(), part.get_payload(decode=True)))
    return out


def _param(header: str, key: str):
    # Very small param extractor; strict enough for the expected inputs.
    for part in header.split(";"):
        part = part.strip()
        if part.startswith(f"{key}="):
            val = part[len(key) + 1 :]
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            return val
    return None


class TestBasicEncoding:
    @pytest.mark.asyncio
    async def test_string_field(self) -> None:
        enc = MultipartEncoder([("name", "alice")], boundary="BOUND")
        body = await _collect(enc)
        assert body.endswith(b"--BOUND--\r\n")
        assert b'Content-Disposition: form-data; name="name"' in body
        assert b"\r\nalice\r\n" in body
        # No Content-Type header should be emitted for text fields.
        assert b"Content-Type:" not in body

    @pytest.mark.asyncio
    async def test_content_length_matches_actual_bytes(self) -> None:
        enc = MultipartEncoder(
            [
                ("name", "alice"),
                ("file", ("hello.txt", b"hello world", "text/plain")),
            ],
            boundary="BOUND",
        )
        body = await _collect(enc)
        assert enc.content_length == len(body)

    @pytest.mark.asyncio
    async def test_auto_content_type_for_known_extension(self) -> None:
        enc = MultipartEncoder(
            [("photo", ("me.png", b"\x89PNG\r\n\x1a\n"))], boundary="BOUND"
        )
        body = await _collect(enc)
        assert b"Content-Type: image/png" in body

    @pytest.mark.asyncio
    async def test_bytes_field_no_filename(self) -> None:
        enc = MultipartEncoder([("payload", b"\x00\x01\x02")], boundary="BOUND")
        body = await _collect(enc)
        assert b'name="payload"' in body
        assert b"filename=" not in body
        assert b"\x00\x01\x02" in body

    @pytest.mark.asyncio
    async def test_dict_input_preserves_order(self) -> None:
        enc = MultipartEncoder(
            {"first": "1", "second": "2", "third": "3"}, boundary="B"
        )
        body = await _collect(enc)
        assert body.index(b'name="first"') < body.index(b'name="second"') < body.index(b'name="third"')


class TestBoundary:
    def test_default_boundary_is_random_and_safe(self) -> None:
        enc = MultipartEncoder([("x", "y")])
        assert enc.boundary.startswith("----hyperhttp-")
        # Entropy: two fresh encoders should almost certainly differ.
        other = MultipartEncoder([("x", "y")])
        assert enc.boundary != other.boundary

    def test_explicit_boundary(self) -> None:
        enc = MultipartEncoder([("x", "y")], boundary="custom-123")
        assert enc.content_type == "multipart/form-data; boundary=custom-123"

    def test_rejects_invalid_boundary(self) -> None:
        with pytest.raises(ValueError):
            MultipartEncoder([("x", "y")], boundary="")
        with pytest.raises(ValueError):
            MultipartEncoder([("x", "y")], boundary="a" * 71)
        with pytest.raises(ValueError):
            MultipartEncoder([("x", "y")], boundary="has\nnewline")
        with pytest.raises(ValueError):
            MultipartEncoder([("x", "y")], boundary="trailing ")


class TestFilenameEncoding:
    @pytest.mark.asyncio
    async def test_quoted_filename_escapes(self) -> None:
        enc = MultipartEncoder(
            [("file", ('weird "name".txt', b"data"))],
            boundary="BOUND",
        )
        body = await _collect(enc)
        assert b'filename="weird \\"name\\".txt"' in body

    @pytest.mark.asyncio
    async def test_non_ascii_filename_uses_rfc5987(self) -> None:
        enc = MultipartEncoder(
            [("file", ("résumé.pdf", b"%PDF-"))], boundary="BOUND"
        )
        body = await _collect(enc)
        assert b"filename*=UTF-8''" in body
        # ASCII fallback also present for old clients.
        assert b'filename="' in body


class TestFromPath:
    @pytest.mark.asyncio
    async def test_file_from_pathlib(self, tmp_path) -> None:
        import pathlib

        p = tmp_path / "blob.bin"
        payload = b"\x00" * (128 * 1024 + 17)
        p.write_bytes(payload)
        enc = MultipartEncoder([("blob", pathlib.Path(p))], boundary="B")
        body = await _collect(enc)
        assert enc.content_length == len(body)
        # Content is embedded intact.
        assert payload in body
        # Filename + default ctype.
        assert b'filename="blob.bin"' in body
        assert b"Content-Type: application/octet-stream" in body

    @pytest.mark.asyncio
    async def test_multipartfile_path_keeps_known_size(self, tmp_path) -> None:
        p = tmp_path / "data.txt"
        p.write_bytes(b"hello\n" * 500)
        mf = MultipartFile(path=p, content_type="text/plain")
        enc = MultipartEncoder([("f", mf)], boundary="B")
        body = await _collect(enc)
        assert enc.content_length == len(body)
        assert b"Content-Type: text/plain" in body

    @pytest.mark.asyncio
    async def test_path_reopens_for_reiteration(self, tmp_path) -> None:
        p = tmp_path / "reused.bin"
        p.write_bytes(b"1234567890")
        enc = MultipartEncoder([("f", p)], boundary="B")
        body1 = await _collect(enc)
        body2 = await _collect(enc)
        assert body1 == body2
        assert enc.content_length == len(body1) == len(body2)


class TestFromFileHandle:
    @pytest.mark.asyncio
    async def test_bytesio_size_detected(self) -> None:
        buf = io.BytesIO(b"hello bytes io")
        enc = MultipartEncoder([("f", ("in.txt", buf))], boundary="B")
        body = await _collect(enc)
        assert enc.content_length == len(body)
        assert b"hello bytes io" in body

    @pytest.mark.asyncio
    async def test_real_file_handle(self, tmp_path) -> None:
        p = tmp_path / "src.txt"
        p.write_bytes(b"abc" * 1000)
        with open(p, "rb") as fh:
            enc = MultipartEncoder([("f", fh)], boundary="B")
            body = await _collect(enc)
        assert enc.content_length == len(body)
        assert b"abc" * 1000 in body

    @pytest.mark.asyncio
    async def test_file_handle_single_use(self) -> None:
        buf = io.BytesIO(b"once")
        enc = MultipartEncoder([("f", ("x", buf))], boundary="B")
        await _collect(enc)
        with pytest.raises(RuntimeError):
            await _collect(enc)


class TestStreamingUnknownSize:
    @pytest.mark.asyncio
    async def test_async_iterable_forces_unknown_length(self) -> None:
        async def gen():
            yield b"one"
            yield b"two"

        enc = MultipartEncoder(
            [("stream", MultipartFile(content=gen(), filename="s.bin"))],
            boundary="B",
        )
        assert enc.content_length is None
        body = await _collect(enc)
        assert b"onetwo" in body


class TestFieldConstruction:
    def test_name_must_be_non_empty_string(self) -> None:
        with pytest.raises(ValueError):
            MultipartEncoder([("", "value")])

    def test_unsupported_type_raises(self) -> None:
        with pytest.raises(TypeError):
            MultipartEncoder([("x", object())])

    def test_tuple_wrong_arity_raises(self) -> None:
        with pytest.raises(ValueError):
            MultipartEncoder([("x", ("a", b"b", "c/t", "extra"))])

    def test_multipartfile_requires_exactly_one_source(self) -> None:
        with pytest.raises(ValueError):
            MultipartFile()  # type: ignore[call-arg]
        with pytest.raises(ValueError):
            MultipartFile(content=b"x", path="/tmp/y")


class TestRoundTripParse:
    @pytest.mark.asyncio
    async def test_body_parses_back_with_stdlib(self, tmp_path) -> None:
        p = tmp_path / "attachment.bin"
        payload = os.urandom(4096)
        p.write_bytes(payload)
        enc = MultipartEncoder(
            [
                ("user", "alice"),
                ("greeting", "héllo"),
                ("photo", ("me.png", b"\x89PNG\r\n\x1a\n", "image/png")),
                ("file", p),
            ],
            boundary="BOUND-123",
        )
        body = await _collect(enc)
        parts = _parse(body, enc.content_type)
        names = [p[0] for p in parts]
        assert names == ["user", "greeting", "photo", "file"]
        assert parts[0][3] == b"alice"
        assert parts[1][3].decode("utf-8") == "héllo"
        assert parts[2][1] == "me.png"
        assert parts[2][2] == "image/png"
        assert parts[3][1] == "attachment.bin"
        assert parts[3][3] == payload
