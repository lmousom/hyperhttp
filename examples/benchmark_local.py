"""
Local benchmark harness for hyperhttp.

Runs an in-process aiohttp server on loopback and drives it with:
  - hyperhttp (this repo)
  - httpx (h1 and, if available, h2)
  - aiohttp

For each (client, body-size), we record:
  - requests/sec (total / wall)
  - P50 / P95 / P99 latency (ms)
  - peak RSS delta (MiB) over the run

Usage:
    python examples/benchmark_local.py
    python examples/benchmark_local.py --requests 5000 --concurrency 128
    python examples/benchmark_local.py --sizes 200,10240 --json results.json

This replaces the WAN-bound httpbin.org benchmark. The only thing measured here
is client CPU / memory behavior, which is what matters for picking an HTTP
client.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import resource
import socket
import ssl
import statistics
import sys
import time
import tracemalloc
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, asdict
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

# Make the source tree importable when run from a checkout.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

try:
    from aiohttp import web as aioweb
except ImportError:  # pragma: no cover - aiohttp is required for benchmarking
    print("aiohttp is required to run the benchmark. Install with `pip install aiohttp`.")
    sys.exit(2)


def _make_payloads(sizes: List[int]) -> Dict[int, bytes]:
    return {s: (b"x" * s) for s in sizes}


async def _make_app(payloads: Dict[int, bytes]) -> aioweb.Application:
    app = aioweb.Application()

    async def handler(request: aioweb.Request) -> aioweb.Response:
        try:
            size = int(request.match_info["size"])
        except (KeyError, ValueError):
            size = 200
        body = payloads.get(size)
        if body is None:
            body = b"x" * size
        return aioweb.Response(body=body, content_type="application/octet-stream")

    app.router.add_get("/bytes/{size}", handler)
    app.router.add_get("/", lambda r: aioweb.Response(text="ok"))
    return app


@asynccontextmanager
async def run_server(payloads: Dict[int, bytes]):
    app = await _make_app(payloads)
    runner = aioweb.AppRunner(app, access_log=None)
    await runner.setup()
    # Bind to an ephemeral port on loopback
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    site = aioweb.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


# ---------------------------------------------------------------------------
# Client drivers
# ---------------------------------------------------------------------------

ClientDriver = Callable[[str, int, int, int], Awaitable["Result"]]


@dataclass
class Result:
    name: str
    size: int
    total_requests: int
    elapsed_s: float
    latencies_ms: List[float] = field(default_factory=list)
    errors: int = 0
    rss_peak_mib: float = 0.0

    @property
    def rps(self) -> float:
        return self.total_requests / self.elapsed_s if self.elapsed_s > 0 else 0.0

    def pct(self, p: float) -> float:
        if not self.latencies_ms:
            return 0.0
        s = sorted(self.latencies_ms)
        k = max(0, min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1)))))
        return s[k]

    def summary(self) -> Dict[str, Any]:
        return {
            "client": self.name,
            "body_size": self.size,
            "requests": self.total_requests,
            "errors": self.errors,
            "elapsed_s": round(self.elapsed_s, 4),
            "rps": round(self.rps, 2),
            "p50_ms": round(self.pct(50), 3),
            "p95_ms": round(self.pct(95), 3),
            "p99_ms": round(self.pct(99), 3),
            "rss_peak_mib": round(self.rss_peak_mib, 2),
        }


def _rss_mib() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB; macOS reports bytes.
    if sys.platform == "darwin":
        return usage / (1024 * 1024)
    return usage / 1024


async def _drive(
    name: str,
    size: int,
    total: int,
    concurrency: int,
    worker: Callable[[int], Awaitable[float]],
) -> Result:
    gc.collect()
    rss_start = _rss_mib()
    tracemalloc.start()
    latencies: List[float] = []
    errors = 0

    sem = asyncio.Semaphore(concurrency)

    async def one(i: int) -> None:
        nonlocal errors
        async with sem:
            try:
                t = await worker(i)
                latencies.append(t)
            except Exception:
                errors += 1

    t0 = time.perf_counter()
    await asyncio.gather(*(one(i) for i in range(total)))
    elapsed = time.perf_counter() - t0

    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_end = _rss_mib()
    rss_delta = max(rss_end - rss_start, peak / (1024 * 1024))

    return Result(
        name=name,
        size=size,
        total_requests=total,
        elapsed_s=elapsed,
        latencies_ms=latencies,
        errors=errors,
        rss_peak_mib=rss_delta,
    )


# ----- hyperhttp -----------------------------------------------------------


async def hyperhttp_driver(url: str, size: int, total: int, concurrency: int) -> Result:
    from hyperhttp import Client

    async with Client(
        max_connections=concurrency * 2,
        max_keepalive_connections=concurrency * 2,
        http2=False,  # benchmark server is plain HTTP/1; no ALPN.
        accept_compressed=False,
    ) as client:
        try:
            r = await client.get(f"{url}/bytes/{size}")
            await r.aread()
        except Exception:
            pass

        async def worker(i: int) -> float:
            t0 = time.perf_counter()
            r = await client.get(f"{url}/bytes/{size}")
            await r.aread()
            return (time.perf_counter() - t0) * 1000

        return await _drive("hyperhttp", size, total, concurrency, worker)


# ----- httpx ---------------------------------------------------------------


async def httpx_driver(
    url: str, size: int, total: int, concurrency: int, http2: bool = False
) -> Result:
    import httpx

    limits = httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency * 2)
    async with httpx.AsyncClient(http2=http2, limits=limits) as client:
        try:
            await client.get(f"{url}/bytes/{size}")
        except Exception:
            pass

        async def worker(i: int) -> float:
            t0 = time.perf_counter()
            r = await client.get(f"{url}/bytes/{size}")
            _ = r.content
            return (time.perf_counter() - t0) * 1000

        name = "httpx[h2]" if http2 else "httpx"
        return await _drive(name, size, total, concurrency, worker)


# ----- aiohttp -------------------------------------------------------------


async def aiohttp_driver(url: str, size: int, total: int, concurrency: int) -> Result:
    import aiohttp as _aiohttp

    conn = _aiohttp.TCPConnector(limit=concurrency * 2, limit_per_host=concurrency * 2)
    timeout = _aiohttp.ClientTimeout(total=60)
    async with _aiohttp.ClientSession(connector=conn, timeout=timeout) as session:
        try:
            async with session.get(f"{url}/bytes/{size}") as r:
                await r.read()
        except Exception:
            pass

        async def worker(i: int) -> float:
            t0 = time.perf_counter()
            async with session.get(f"{url}/bytes/{size}") as r:
                await r.read()
            return (time.perf_counter() - t0) * 1000

        return await _drive("aiohttp", size, total, concurrency, worker)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _print_table(results: List[Result]) -> None:
    header = ("client", "size", "req", "err", "rps", "p50", "p95", "p99", "rss_mib")
    print()
    print("{:<12} {:>7} {:>6} {:>5} {:>10} {:>8} {:>8} {:>8} {:>9}".format(*header))
    print("-" * 82)
    for r in results:
        s = r.summary()
        print(
            "{:<12} {:>7} {:>6} {:>5} {:>10.1f} {:>8.2f} {:>8.2f} {:>8.2f} {:>9.2f}".format(
                s["client"],
                s["body_size"],
                s["requests"],
                s["errors"],
                s["rps"],
                s["p50_ms"],
                s["p95_ms"],
                s["p99_ms"],
                s["rss_peak_mib"],
            )
        )


async def _bench_all(
    url: str,
    sizes: List[int],
    requests: int,
    concurrency: int,
    clients: List[str],
) -> List[Result]:
    results: List[Result] = []
    for size in sizes:
        for client in clients:
            if client == "hyperhttp":
                r = await hyperhttp_driver(url, size, requests, concurrency)
            elif client == "httpx":
                r = await httpx_driver(url, size, requests, concurrency, http2=False)
            elif client == "httpx-h2":
                r = await httpx_driver(url, size, requests, concurrency, http2=True)
            elif client == "aiohttp":
                r = await aiohttp_driver(url, size, requests, concurrency)
            else:
                continue
            results.append(r)
            print(f"  done: {r.summary()}")
    return results


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--requests", type=int, default=2000)
    p.add_argument("--concurrency", type=int, default=64)
    p.add_argument(
        "--sizes",
        type=lambda s: [int(x) for x in s.split(",")],
        default=[200, 10_240, 1_048_576],
        help="Comma-separated body sizes in bytes",
    )
    p.add_argument(
        "--clients",
        type=lambda s: [x.strip() for x in s.split(",")],
        default=["hyperhttp", "httpx", "aiohttp"],
        help="Comma-separated client names (hyperhttp, httpx, httpx-h2, aiohttp)",
    )
    p.add_argument("--json", type=str, default=None, help="Write results JSON to path")
    p.add_argument("--uvloop", action="store_true", help="Install uvloop if available")
    return p.parse_args()


async def main() -> int:
    args = parse_args()
    if args.uvloop:
        try:
            import uvloop  # type: ignore

            uvloop.install()
        except ImportError:
            print("uvloop not installed, continuing with stdlib asyncio")

    payloads = _make_payloads(args.sizes)
    async with run_server(payloads) as url:
        print(f"local server: {url}")
        print(
            f"config: requests={args.requests} concurrency={args.concurrency} "
            f"sizes={args.sizes} clients={args.clients}"
        )
        results = await _bench_all(
            url, args.sizes, args.requests, args.concurrency, args.clients
        )

    _print_table(results)

    if args.json:
        with open(args.json, "w") as fh:
            json.dump([r.summary() for r in results], fh, indent=2)
        print(f"results written to {args.json}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
