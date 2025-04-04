"""
Benchmarking tool for comparing HyperHTTP with other HTTP clients.

This benchmark measures performance characteristics for different HTTP clients
including requests/second, memory usage, and latency distribution.
"""

import argparse
import asyncio
import statistics
import time
import gc
import tracemalloc
from typing import Dict, Any, List, Callable, Awaitable, Optional, Tuple

# Import HyperHTTP client
from hyperhttp import Client

# Optional imports for other clients
try:
    import httpx
    HAVE_HTTPX = True
except ImportError:
    HAVE_HTTPX = False

try:
    import aiohttp
    HAVE_AIOHTTP = True
except ImportError:
    HAVE_AIOHTTP = False

try:
    import requests
    HAVE_REQUESTS = True
except ImportError:
    HAVE_REQUESTS = False


# Type for client factories
ClientFactory = Callable[[], Any]

# Type for request functions
RequestFunc = Callable[[Any, str], Awaitable[Any]]


async def hyperhttp_request(client: Client, url: str) -> None:
    """Perform a request with HyperHTTP."""
    response = await client.get(url)
    await response.body()  # Ensure body is fully read


async def httpx_request(client: httpx.AsyncClient, url: str) -> None:
    """Perform a request with HTTPX."""
    response = await client.get(url)
    response.read()  # Ensure body is fully read


async def aiohttp_request(client: aiohttp.ClientSession, url: str) -> None:
    """Perform a request with AIOHTTP."""
    async with client.get(url) as response:
        await response.read()  # Ensure body is fully read


def make_hyperhttp_client() -> Client:
    """Create a HyperHTTP client."""
    return Client()


def make_httpx_client() -> httpx.AsyncClient:
    """Create an HTTPX client."""
    return httpx.AsyncClient(http2=True)


def make_aiohttp_client() -> aiohttp.ClientSession:
    """Create an AIOHTTP client."""
    return aiohttp.ClientSession()


class Benchmark:
    """Benchmark runner for HTTP clients."""
    
    def __init__(
        self,
        name: str,
        client_factory: ClientFactory,
        request_func: RequestFunc,
        concurrency: int = 10,
        requests_per_client: int = 100,
        warmup_requests: int = 10,
    ):
        self.name = name
        self.client_factory = client_factory
        self.request_func = request_func
        self.concurrency = concurrency
        self.requests_per_client = requests_per_client
        self.warmup_requests = warmup_requests
        self.latencies: List[float] = []
        self.errors = 0
        self.peak_memory = 0
        
    async def run_client(self, client: Any, url: str) -> None:
        """Run benchmark for a single client."""
        for _ in range(self.requests_per_client):
            start_time = time.time()
            try:
                await self.request_func(client, url)
                self.latencies.append((time.time() - start_time) * 1000)  # ms
            except Exception as e:
                self.errors += 1
                print(f"Error in {self.name}: {e}")
                
    async def run(self, url: str) -> Dict[str, Any]:
        """Run the complete benchmark."""
        print(f"Running benchmark for {self.name}...")
        
        # Reset state
        self.latencies = []
        self.errors = 0
        
        # Create clients
        clients = [self.client_factory() for _ in range(self.concurrency)]
        
        # Warmup
        print("  Warming up...")
        warmup_tasks = []
        for i in range(min(self.warmup_requests, self.concurrency)):
            task = asyncio.create_task(self.request_func(clients[i], url))
            warmup_tasks.append(task)
        await asyncio.gather(*warmup_tasks)
        
        # Run gc to start with a clean slate
        gc.collect()
        
        # Start memory tracking
        tracemalloc.start()
        start_time = time.time()
        
        # Run benchmark
        print(f"  Running {self.concurrency} clients with {self.requests_per_client} requests each...")
        tasks = []
        for i in range(self.concurrency):
            task = asyncio.create_task(self.run_client(clients[i], url))
            tasks.append(task)
        await asyncio.gather(*tasks)
        
        # Calculate results
        duration = time.time() - start_time
        current, peak = tracemalloc.get_traced_memory()
        self.peak_memory = peak / (1024 * 1024)  # MB
        tracemalloc.stop()
        
        # Close clients
        for client in clients:
            if hasattr(client, 'close'):
                if asyncio.iscoroutinefunction(client.close):
                    await client.close()
                else:
                    client.close()
            elif hasattr(client, 'aclose'):
                await client.aclose()
        
        # Calculate statistics
        total_requests = self.concurrency * self.requests_per_client - self.errors
        requests_per_second = total_requests / duration
        
        if self.latencies:
            latency_stats = {
                'min': min(self.latencies),
                'max': max(self.latencies),
                'mean': statistics.mean(self.latencies),
                'median': statistics.median(self.latencies),
                'p95': percentile(self.latencies, 95),
                'p99': percentile(self.latencies, 99),
            }
        else:
            latency_stats = {
                'min': 0,
                'max': 0,
                'mean': 0,
                'median': 0,
                'p95': 0,
                'p99': 0,
            }
        
        return {
            'name': self.name,
            'requests': total_requests,
            'errors': self.errors,
            'duration': duration,
            'requests_per_second': requests_per_second,
            'latency': latency_stats,
            'memory_mb': self.peak_memory,
        }


def percentile(data: List[float], percentile: float) -> float:
    """Calculate a percentile from a list of values."""
    if not data:
        return 0
    sorted_data = sorted(data)
    idx = int(len(sorted_data) * (percentile / 100))
    return sorted_data[idx]


def print_results(results: List[Dict[str, Any]]) -> None:
    """Print benchmark results in a table."""
    print("\n=== Benchmark Results ===\n")
    
    # Print header
    headers = ["Client", "Req/sec", "Memory (MB)", "p50 (ms)", "p95 (ms)", "p99 (ms)"]
    widths = [max(len(h), max(len(r['name']) for r in results)) for h in headers]
    widths[1] = max(10, widths[1])
    
    # Format header
    header_line = " | ".join(h.ljust(w) for h, w in zip(headers, widths))
    separator = "-+-".join("-" * w for w in widths)
    print(header_line)
    print(separator)
    
    # Print results
    for result in results:
        row = [
            result['name'].ljust(widths[0]),
            f"{result['requests_per_second']:.2f}".ljust(widths[1]),
            f"{result['memory_mb']:.2f}".ljust(widths[2]),
            f"{result['latency']['median']:.2f}".ljust(widths[3]),
            f"{result['latency']['p95']:.2f}".ljust(widths[4]),
            f"{result['latency']['p99']:.2f}".ljust(widths[5]),
        ]
        print(" | ".join(row))


async def run_benchmarks(
    url: str,
    concurrency: int,
    requests: int,
    clients: List[str],
) -> None:
    """Run benchmarks for the specified clients."""
    results = []
    
    # Setup benchmarks based on available clients
    benchmarks = []
    
    if "hyperhttp" in clients:
        benchmarks.append(Benchmark(
            name="hyperhttp",
            client_factory=make_hyperhttp_client,
            request_func=hyperhttp_request,
            concurrency=concurrency,
            requests_per_client=requests,
        ))
    
    if "httpx" in clients and HAVE_HTTPX:
        benchmarks.append(Benchmark(
            name="httpx",
            client_factory=make_httpx_client,
            request_func=httpx_request,
            concurrency=concurrency,
            requests_per_client=requests,
        ))
    
    if "aiohttp" in clients and HAVE_AIOHTTP:
        benchmarks.append(Benchmark(
            name="aiohttp",
            client_factory=make_aiohttp_client,
            request_func=aiohttp_request,
            concurrency=concurrency,
            requests_per_client=requests,
        ))
    
    # Run benchmarks
    for benchmark in benchmarks:
        result = await benchmark.run(url)
        results.append(result)
    
    # Print results
    print_results(results)


def main() -> None:
    """Parse arguments and run benchmarks."""
    parser = argparse.ArgumentParser(description="HTTP client benchmark tool")
    parser.add_argument(
        "--url", "-u",
        default="https://httpbin.org/get",
        help="URL to benchmark against (default: https://httpbin.org/get)"
    )
    parser.add_argument(
        "--concurrency", "-c",
        type=int, default=10,
        help="Number of concurrent clients (default: 10)"
    )
    parser.add_argument(
        "--requests", "-r",
        type=int, default=100,
        help="Requests per client (default: 100)"
    )
    parser.add_argument(
        "--clients",
        nargs="+",
        default=["hyperhttp", "httpx", "aiohttp"],
        help="Clients to benchmark (default: all available)"
    )
    
    args = parser.parse_args()
    
    # Check if requested clients are available
    available_clients = ["hyperhttp"]
    if HAVE_HTTPX:
        available_clients.append("httpx")
    if HAVE_AIOHTTP:
        available_clients.append("aiohttp")
    
    requested_clients = [c for c in args.clients if c in available_clients]
    if not requested_clients:
        print(f"No requested clients are available. Available clients: {available_clients}")
        return
    
    # Print benchmark configuration
    print(f"Benchmark configuration:")
    print(f"  URL: {args.url}")
    print(f"  Concurrency: {args.concurrency}")
    print(f"  Requests per client: {args.requests}")
    print(f"  Clients: {requested_clients}")
    
    # Run benchmarks
    asyncio.run(run_benchmarks(
        url=args.url,
        concurrency=args.concurrency,
        requests=args.requests,
        clients=requested_clients,
    ))


if __name__ == "__main__":
    main()