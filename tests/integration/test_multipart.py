"""End-to-end multipart upload tests: Client.post(files=...) → aiohttp server."""

from __future__ import annotations

import os
import socket
from typing import AsyncIterator

import pytest
import pytest_asyncio
from aiohttp import web as aioweb

import hyperhttp
from hyperhttp import MultipartEncoder, MultipartFile


def _pick_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest_asyncio.fixture
async def upload_server() -> AsyncIterator[str]:
    """Accepts multipart uploads, echoes back a JSON summary of the parts."""

    async def upload(request: aioweb.Request) -> aioweb.Response:
        summary = []
        reader = await request.multipart()
        while True:
            part = await reader.next()
            if part is None:
                break
            body = await part.read(decode=False)
            summary.append(
                {
                    "name": part.name,
                    "filename": part.filename,
                    "content_type": part.headers.get("Content-Type"),
                    "size": len(body),
                    "sha_prefix": body[:32].hex(),
                }
            )
        return aioweb.json_response(
            {
                "content_type": request.headers.get("Content-Type"),
                "content_length": request.headers.get("Content-Length"),
                "transfer_encoding": request.headers.get("Transfer-Encoding"),
                "parts": summary,
            }
        )

    app = aioweb.Application(client_max_size=256 * 1024 * 1024)
    app.router.add_post("/upload", upload)
    runner = aioweb.AppRunner(app, access_log=None)
    await runner.setup()
    port = _pick_port()
    site = aioweb.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_simple_files_post_sets_content_length(upload_server) -> None:
    async with hyperhttp.Client(trust_env=False, retry=False) as client:
        r = await client.post(
            f"{upload_server}/upload",
            data={"user": "alice", "role": "admin"},
            files={"photo": ("me.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        )
        assert r.status_code == 200
        await r.aread()
        result = r.json()
    # Server saw a content-length-framed upload (no chunked).
    assert result["transfer_encoding"] is None
    assert result["content_length"] is not None
    assert result["content_type"].startswith("multipart/form-data; boundary=")

    names = [p["name"] for p in result["parts"]]
    assert names == ["user", "role", "photo"]
    photo = result["parts"][-1]
    assert photo["filename"] == "me.png"
    assert photo["content_type"] == "image/png"
    assert photo["size"] == len(b"\x89PNG\r\n\x1a\n")


@pytest.mark.asyncio
async def test_upload_large_file_from_disk(tmp_path, upload_server) -> None:
    path = tmp_path / "blob.bin"
    payload = os.urandom(5 * 1024 * 1024 + 123)  # 5 MiB + a few bytes
    path.write_bytes(payload)

    async with hyperhttp.Client(trust_env=False, retry=False) as client:
        r = await client.post(
            f"{upload_server}/upload",
            files={"file": path},
        )
        assert r.status_code == 200
        await r.aread()
        result = r.json()

    assert result["transfer_encoding"] is None  # Content-Length framed.
    assert int(result["content_length"]) > len(payload)  # + headers/boundary
    part = result["parts"][0]
    assert part["name"] == "file"
    assert part["filename"] == "blob.bin"
    assert part["size"] == len(payload)
    assert part["sha_prefix"] == payload[:32].hex()


@pytest.mark.asyncio
async def test_upload_async_iterable_uses_chunked(upload_server) -> None:
    async def stream():
        yield b"abc" * 1024
        yield b"def" * 2048

    async with hyperhttp.Client(trust_env=False, retry=False) as client:
        r = await client.post(
            f"{upload_server}/upload",
            files={"stream": MultipartFile(content=stream(), filename="s.bin")},
        )
        assert r.status_code == 200
        await r.aread()
        result = r.json()

    # Size unknown → chunked framing.
    assert result["transfer_encoding"] == "chunked"
    assert result["content_length"] is None
    assert result["parts"][0]["size"] == len(b"abc" * 1024) + len(b"def" * 2048)


@pytest.mark.asyncio
async def test_prebuilt_encoder_as_content(upload_server) -> None:
    encoder = MultipartEncoder(
        [
            ("user", "bob"),
            ("file", ("doc.txt", b"some text", "text/plain")),
        ],
        boundary="preset-boundary-123",
    )
    async with hyperhttp.Client(trust_env=False, retry=False) as client:
        r = await client.post(f"{upload_server}/upload", content=encoder)
        assert r.status_code == 200
        await r.aread()
        result = r.json()
    assert "preset-boundary-123" in result["content_type"]
    assert result["transfer_encoding"] is None


@pytest.mark.asyncio
async def test_body_size_exactly_matches_declared(upload_server) -> None:
    """If Content-Length is wrong the transport raises; this asserts it's right."""
    payload = os.urandom(128 * 1024)
    async with hyperhttp.Client(trust_env=False, retry=False) as client:
        r = await client.post(
            f"{upload_server}/upload",
            files={"f": ("x.bin", payload, "application/octet-stream")},
        )
        await r.aread()
        assert r.status_code == 200
        assert r.json()["parts"][0]["size"] == len(payload)


@pytest.mark.asyncio
async def test_non_ascii_filename_round_trip(upload_server) -> None:
    async with hyperhttp.Client(trust_env=False, retry=False) as client:
        r = await client.post(
            f"{upload_server}/upload",
            files={"file": ("résumé.pdf", b"%PDF-1.4\n")},
        )
        await r.aread()
        result = r.json()
    assert result["parts"][0]["filename"] == "résumé.pdf"
