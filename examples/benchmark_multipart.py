"""
Multipart upload benchmark: hyperhttp vs aiohttp vs httpx.

For each client and file size we measure:

- **Throughput** (MiB/s) from wall-clock of the whole upload loop.
- **Peak RSS delta** over the run — sanity check that we're not buffering.

The server drains the body into a counter and returns ``204``. That isolates
the measurement to *upload-side* work (body encoding + socket writes), which
is what we want when the question is "which client uploads fastest".

Usage::

    python examples/benchmark_multipart.py
    python examples/benchmark_multipart.py --sizes 1048576,33554432,104857600
    python examples/benchmark_multipart.py --iters 5 --concurrency 4
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import os
import resource
import socket
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Callable, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from aiohttp import web as aioweb


def _pick_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _start_server() -> Tuple[str, aioweb.AppRunner]:
    async def upload(request: aioweb.Request) -> aioweb.Response:
        # Drain the body efficiently.
        total = 0
        while True:
            chunk = await request.content.readany()
            if not chunk:
                break
            total += len(chunk)
        return aioweb.Response(status=204)

    app = aioweb.Application(client_max_size=2 * 1024 * 1024 * 1024)
    app.router.add_post("/upload", upload)
    runner = aioweb.AppRunner(app, access_log=None)
    await runner.setup()
    port = _pick_port()
    site = aioweb.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return f"http://127.0.0.1:{port}", runner


@dataclass
class Result:
    client: str
    size_bytes: int
    iters: int
    concurrency: int
    wall_seconds: float
    rss_delta_mib: float

    @property
    def throughput_mib_s(self) -> float:
        total = self.iters * self.size_bytes
        return total / (1024 * 1024) / self.wall_seconds


def _rss_mib() -> float:
    ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes, Linux reports KiB.
    if sys.platform == "darwin":
        return ru / (1024 * 1024)
    return ru / 1024


async def _bench(
    name: str,
    driver: Callable,
    url: str,
    path: str,
    iters: int,
    concurrency: int,
    size: int,
) -> Result:
    gc.collect()
    rss_before = _rss_mib()
    start = time.perf_counter()
    await driver(url, path, iters, concurrency)
    wall = time.perf_counter() - start
    rss_after = _rss_mib()
    return Result(
        client=name,
        size_bytes=size,
        iters=iters,
        concurrency=concurrency,
        wall_seconds=wall,
        rss_delta_mib=max(0.0, rss_after - rss_before),
    )


# ---------------------------------------------------------------------------
# Drivers
# ---------------------------------------------------------------------------


async def _drive_hyperhttp(url: str, path: str, iters: int, concurrency: int) -> None:
    import hyperhttp

    async with hyperhttp.Client(trust_env=False, retry=False, http2=False) as client:
        async def one() -> None:
            r = await client.post(f"{url}/upload", files={"f": path})
            await r.aread()

        await _run_concurrent(one, iters, concurrency)


async def _drive_aiohttp(url: str, path: str, iters: int, concurrency: int) -> None:
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async def one() -> None:
            with open(path, "rb") as fh:
                data = aiohttp.FormData()
                data.add_field("f", fh, filename=os.path.basename(path))
                async with session.post(f"{url}/upload", data=data) as r:
                    await r.read()

        await _run_concurrent(one, iters, concurrency)


async def _drive_httpx(url: str, path: str, iters: int, concurrency: int) -> None:
    import httpx

    async with httpx.AsyncClient(http2=False, trust_env=False) as client:
        async def one() -> None:
            with open(path, "rb") as fh:
                r = await client.post(f"{url}/upload", files={"f": fh})
                await r.aread()

        await _run_concurrent(one, iters, concurrency)


async def _run_concurrent(fn, iters: int, concurrency: int) -> None:
    sem = asyncio.Semaphore(concurrency)

    async def guarded() -> None:
        async with sem:
            await fn()

    await asyncio.gather(*(guarded() for _ in range(iters)))


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def _amain(sizes: List[int], iters: int, concurrency: int) -> None:
    url, runner = await _start_server()
    try:
        with tempfile.TemporaryDirectory(prefix="hyperhttp-mp-bench-") as tmp:
            headers = ["client", "size", "iters", "wall_s", "MiB/s", "ΔRSS_MiB"]
            rows: List[Result] = []
            for size in sizes:
                path = os.path.join(tmp, f"blob-{size}.bin")
                with open(path, "wb") as f:
                    f.write(os.urandom(size))
                # Warm up each client once to discount import/connect cost.
                await _drive_hyperhttp(url, path, 1, 1)
                await _drive_aiohttp(url, path, 1, 1)
                try:
                    await _drive_httpx(url, path, 1, 1)
                    have_httpx = True
                except ImportError:
                    have_httpx = False

                for name, driver in [
                    ("hyperhttp", _drive_hyperhttp),
                    ("aiohttp", _drive_aiohttp),
                ] + ([("httpx", _drive_httpx)] if have_httpx else []):
                    result = await _bench(
                        name, driver, url, path, iters, concurrency, size
                    )
                    rows.append(result)

            _print_table(rows)
    finally:
        await runner.cleanup()


def _print_table(rows: List[Result]) -> None:
    headers = ("client", "size", "iters", "wall_s", "MiB/s", "ΔRSS_MiB")
    width = [12, 12, 6, 8, 10, 10]
    print("  ".join(h.ljust(w) for h, w in zip(headers, width)))
    print("  ".join("-" * w for w in width))
    for r in rows:
        size_label = _human_size(r.size_bytes)
        print(
            "  ".join(
                [
                    r.client.ljust(width[0]),
                    size_label.ljust(width[1]),
                    str(r.iters).ljust(width[2]),
                    f"{r.wall_seconds:.3f}".ljust(width[3]),
                    f"{r.throughput_mib_s:.1f}".ljust(width[4]),
                    f"{r.rss_delta_mib:.1f}".ljust(width[5]),
                ]
            )
        )


def _human_size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:g} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:g} TiB"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sizes",
        default="1048576,10485760,104857600",
        help="Comma-separated upload sizes in bytes (default: 1 MiB, 10 MiB, 100 MiB)",
    )
    parser.add_argument(
        "--iters", type=int, default=5, help="Uploads per (client, size) (default: 5)"
    )
    parser.add_argument(
        "--concurrency", type=int, default=1, help="In-flight uploads (default: 1)"
    )
    args = parser.parse_args()
    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    asyncio.run(_amain(sizes, args.iters, args.concurrency))


if __name__ == "__main__":
    main()
