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
import json
import csv
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Callable, Awaitable, Optional, Tuple, Union
from dataclasses import dataclass
from tqdm import tqdm
import numpy as np

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


# Client factory functions
def make_hyperhttp_client() -> Client:
    """Create a HyperHTTP client."""
    return Client()


def make_httpx_client() -> httpx.AsyncClient:
    """Create an HTTPX client."""
    return httpx.AsyncClient(http2=True)


def make_aiohttp_client() -> aiohttp.ClientSession:
    """Create an AIOHTTP client."""
    return aiohttp.ClientSession()


# Request functions
async def hyperhttp_request(client: Client, url: str, **kwargs) -> Any:
    """Perform a request with HyperHTTP."""
    if kwargs.get('json'):
        response = await client.post(url, json=kwargs['json'])
    else:
        response = await client.get(url)
    body = await response.body()  # HyperHTTP uses body() method
    return body


async def httpx_request(client: httpx.AsyncClient, url: str, **kwargs) -> Any:
    """Perform a request with HTTPX."""
    if kwargs.get('json'):
        response = await client.post(url, json=kwargs['json'])
    else:
        response = await client.get(url)
    body = response.content  # HTTPX uses content property
    return body


async def aiohttp_request(client: aiohttp.ClientSession, url: str, **kwargs) -> Any:
    """Perform a request with AIOHTTP."""
    if kwargs.get('json'):
        async with client.post(url, json=kwargs['json']) as response:
            body = await response.read()  # AIOHTTP uses read() method
            return body
    else:
        async with client.get(url) as response:
            body = await response.read()  # AIOHTTP uses read() method
            return body


@dataclass
class BenchmarkConfig:
    """Configuration for benchmark runs."""
    url: str
    method: str = "GET"
    payload: Optional[Dict[str, Any]] = None
    headers: Optional[Dict[str, str]] = None
    concurrency: int = 10
    requests_per_client: int = 100
    warmup_requests: int = 10
    cooldown_seconds: int = 5
    timeout_seconds: int = 30
    output_format: str = "table"  # table, json, csv
    output_file: Optional[str] = None
    clients: List[str] = None


@dataclass
class BenchmarkResult:
    """Results from a benchmark run."""
    name: str
    total_requests: int
    successful_requests: int
    failed_requests: int
    duration: float
    requests_per_second: float
    latency_stats: Dict[str, float]
    memory_stats: Dict[str, float]
    error_rate: float
    throughput_mbps: float
    timestamp: str


class Benchmark:
    """Benchmark runner for HTTP clients."""
    
    def __init__(self, config: BenchmarkConfig):
        self.config = config
        self.latencies: List[float] = []
        self.errors = 0
        self.peak_memory = 0
        self.start_time: float = 0
        self.end_time: float = 0
        self.total_bytes = 0
        
    async def run_client(self, client: Any, url: str, progress_bar: tqdm) -> None:
        """Run benchmark for a single client."""
        for _ in range(self.config.requests_per_client):
            start_time = time.time()
            try:
                if self.config.method == "GET":
                    body = await self.request_func(client, url)
                else:
                    body = await self.request_func(client, url, json=self.config.payload)
                
                # Track response size
                self.total_bytes += len(body)
                
                latency = (time.time() - start_time) * 1000  # ms
                self.latencies.append(latency)
                progress_bar.update(1)
                
            except Exception as e:
                self.errors += 1
                progress_bar.update(1)
                print(f"\nError in {self.name}: {e}")
                
    async def run(self, name: str, client_factory: Callable, request_func: Callable) -> BenchmarkResult:
        """Run the complete benchmark."""
        self.name = name
        self.client_factory = client_factory
        self.request_func = request_func
        
        print(f"\nRunning benchmark for {name}...")
        
        # Reset state
        self.latencies = []
        self.errors = 0
        self.total_bytes = 0
        
        # Create clients
        clients = [self.client_factory() for _ in range(self.config.concurrency)]
        
        # Warmup phase
        print("  Warming up...")
        warmup_tasks = []
        for i in range(min(self.config.warmup_requests, self.config.concurrency)):
            task = asyncio.create_task(self.request_func(clients[i], self.config.url))
            warmup_tasks.append(task)
        await asyncio.gather(*warmup_tasks)
        
        # Cooldown
        await asyncio.sleep(self.config.cooldown_seconds)
        
        # Run gc to start with a clean slate
        gc.collect()
        
        # Start memory tracking
        tracemalloc.start()
        self.start_time = time.time()
        
        # Run benchmark
        total_requests = self.config.concurrency * self.config.requests_per_client
        with tqdm(total=total_requests, desc=f"  {name}") as progress_bar:
            tasks = []
            for i in range(self.config.concurrency):
                task = asyncio.create_task(
                    self.run_client(clients[i], self.config.url, progress_bar)
                )
                tasks.append(task)
            await asyncio.gather(*tasks)
        
        self.end_time = time.time()
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
        duration = self.end_time - self.start_time
        successful_requests = total_requests - self.errors
        requests_per_second = successful_requests / duration
        throughput_mbps = (self.total_bytes * 8) / (duration * 1024 * 1024)
        
        if self.latencies:
            latency_stats = {
                'min': min(self.latencies),
                'max': max(self.latencies),
                'mean': statistics.mean(self.latencies),
                'median': statistics.median(self.latencies),
                'p90': percentile(self.latencies, 90),
                'p95': percentile(self.latencies, 95),
                'p99': percentile(self.latencies, 99),
                'stddev': statistics.stdev(self.latencies) if len(self.latencies) > 1 else 0,
            }
        else:
            latency_stats = {k: 0 for k in ['min', 'max', 'mean', 'median', 'p90', 'p95', 'p99', 'stddev']}
        
        memory_stats = {
            'peak_mb': self.peak_memory,
            'current_mb': current / (1024 * 1024),
        }
        
        return BenchmarkResult(
            name=name,
            total_requests=total_requests,
            successful_requests=successful_requests,
            failed_requests=self.errors,
            duration=duration,
            requests_per_second=requests_per_second,
            latency_stats=latency_stats,
            memory_stats=memory_stats,
            error_rate=(self.errors / total_requests) * 100 if total_requests > 0 else 0,
            throughput_mbps=throughput_mbps,
            timestamp=datetime.now().isoformat()
        )


def percentile(data: List[float], percentile: float) -> float:
    """Calculate a percentile from a list of values."""
    if not data:
        return 0
    return np.percentile(data, percentile)


def print_results(results: List[BenchmarkResult], format: str = "table", output_file: Optional[str] = None) -> None:
    """Print benchmark results in the specified format."""
    if format == "table":
        print("\n=== Benchmark Results ===\n")
        
        # Print header
        headers = ["Client", "Req/sec", "Throughput", "Memory (MB)", "p50 (ms)", "p95 (ms)", "p99 (ms)", "Error %"]
        widths = [max(len(h), max(len(r.name) for r in results)) for h in headers]
        widths[1] = max(10, widths[1])
        
        # Format header
        header_line = " | ".join(h.ljust(w) for h, w in zip(headers, widths))
        separator = "-+-".join("-" * w for w in widths)
        print(header_line)
        print(separator)
        
        # Print results
        for result in results:
            row = [
                result.name.ljust(widths[0]),
                f"{result.requests_per_second:.2f}".ljust(widths[1]),
                f"{result.throughput_mbps:.2f} Mbps".ljust(widths[2]),
                f"{result.memory_stats['peak_mb']:.2f}".ljust(widths[3]),
                f"{result.latency_stats['median']:.2f}".ljust(widths[4]),
                f"{result.latency_stats['p95']:.2f}".ljust(widths[5]),
                f"{result.latency_stats['p99']:.2f}".ljust(widths[6]),
                f"{result.error_rate:.2f}".ljust(widths[7]),
            ]
            print(" | ".join(row))
    
    elif format in ["json", "csv"]:
        data = [vars(result) for result in results]
        
        if output_file:
            if format == "json":
                with open(output_file, 'w') as f:
                    json.dump(data, f, indent=2)
            else:  # csv
                with open(output_file, 'w', newline='') as f:
                    writer = csv.DictWriter(f, fieldnames=data[0].keys())
                    writer.writeheader()
                    writer.writerows(data)
        else:
            if format == "json":
                print(json.dumps(data, indent=2))
            else:  # csv
                import sys
                writer = csv.DictWriter(sys.stdout, fieldnames=data[0].keys())
                writer.writeheader()
                writer.writerows(data)


async def run_benchmarks(config: BenchmarkConfig) -> List[BenchmarkResult]:
    """Run benchmarks for the specified clients."""
    results = []
    benchmark = Benchmark(config)
    
    # Setup benchmarks based on available clients
    if "hyperhttp" in config.clients:
        results.append(await benchmark.run(
            name="hyperhttp",
            client_factory=make_hyperhttp_client,
            request_func=hyperhttp_request
        ))
    
    if "httpx" in config.clients and HAVE_HTTPX:
        results.append(await benchmark.run(
            name="httpx",
            client_factory=make_httpx_client,
            request_func=httpx_request
        ))
    
    if "aiohttp" in config.clients and HAVE_AIOHTTP:
        results.append(await benchmark.run(
            name="aiohttp",
            client_factory=make_aiohttp_client,
            request_func=aiohttp_request
        ))
    
    return results


def parse_args() -> BenchmarkConfig:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="HTTP Client Benchmark Tool")
    parser.add_argument("url", help="URL to benchmark against")
    parser.add_argument("--method", default="GET", choices=["GET", "POST", "PUT", "DELETE"],
                      help="HTTP method to use")
    parser.add_argument("--payload", type=json.loads, help="JSON payload for POST/PUT requests")
    parser.add_argument("--headers", type=json.loads, help="JSON headers to include")
    parser.add_argument("--concurrency", type=int, default=10,
                      help="Number of concurrent clients")
    parser.add_argument("--requests", type=int, default=100,
                      help="Number of requests per client")
    parser.add_argument("--warmup", type=int, default=10,
                      help="Number of warmup requests")
    parser.add_argument("--cooldown", type=int, default=5,
                      help="Cooldown period in seconds")
    parser.add_argument("--timeout", type=int, default=30,
                      help="Request timeout in seconds")
    parser.add_argument("--format", choices=["table", "json", "csv"], default="table",
                      help="Output format")
    parser.add_argument("--output", help="Output file path")
    parser.add_argument("--clients", nargs="+", default=["hyperhttp", "httpx", "aiohttp"],
                      choices=["hyperhttp", "httpx", "aiohttp"],
                      help="Clients to benchmark")
    
    args = parser.parse_args()
    
    return BenchmarkConfig(
        url=args.url,
        method=args.method,
        payload=args.payload,
        headers=args.headers,
        concurrency=args.concurrency,
        requests_per_client=args.requests,
        warmup_requests=args.warmup,
        cooldown_seconds=args.cooldown,
        timeout_seconds=args.timeout,
        output_format=args.format,
        output_file=args.output,
        clients=args.clients
    )


async def main() -> None:
    """Main entry point."""
    config = parse_args()
    results = await run_benchmarks(config)
    print_results(results, config.output_format, config.output_file)


if __name__ == "__main__":
    asyncio.run(main())