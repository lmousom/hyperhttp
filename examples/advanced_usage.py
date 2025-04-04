"""
Advanced usage examples for HyperHTTP client.

This demonstrates more complex features like connection pooling,
parallel requests, retry policies, and error handling.
"""

import asyncio
import json
import time
from typing import Dict, Any, List

from hyperhttp import Client
from hyperhttp.errors.retry import RetryPolicy
from hyperhttp.utils.backoff import ExponentialBackoff, DecorrelatedJitterBackoff


async def parallel_requests() -> None:
    """Demonstrate parallel request execution."""
    print("=== Parallel Requests ===")
    async with Client() as client:
        # Create a list of URLs to request in parallel
        urls = [
            "https://httpbin.org/get",
            "https://httpbin.org/ip",
            "https://httpbin.org/user-agent",
            "https://httpbin.org/headers",
        ]
        
        print(f"Making {len(urls)} parallel requests...")
        start_time = time.time()
        
        # Create tasks for parallel execution
        tasks = [client.get(url) for url in urls]
        responses = await asyncio.gather(*tasks)
        
        # Process all responses
        for i, response in enumerate(responses):
            print(f"Response {i+1}: {response.status_code} - {urls[i]}")
        
        elapsed = time.time() - start_time
        print(f"Completed in {elapsed:.3f} seconds")


async def custom_retry_policy() -> None:
    """Demonstrate custom retry policy."""
    print("\n=== Custom Retry Policy ===")
    
    # Create a client with custom retry policy
    retry_policy = RetryPolicy(
        max_retries=5,
        retry_categories=['TRANSIENT', 'TIMEOUT', 'SERVER'],
        status_force_list=[429, 500, 502, 503, 504],
        backoff_strategy=DecorrelatedJitterBackoff(
            base=0.1,
            max_backoff=10.0,
        ),
        respect_retry_after=True,
    )
    
    async with Client(retry_policy=retry_policy) as client:
        print("Making request to endpoint that might fail...")
        
        try:
            # This endpoint will randomly return 500 errors
            response = await client.get("https://httpbin.org/status/200,500")
            print(f"Request succeeded: {response.status_code}")
        except Exception as e:
            print(f"Request failed after retries: {e}")


async def streaming_response() -> None:
    """Demonstrate handling a streaming response."""
    print("\n=== Streaming Response ===")
    
    async with Client() as client:
        print("Making request for streaming response...")
        
        # Request a stream of 10 JSON objects
        response = await client.get("https://httpbin.org/stream/5")
        
        # Process the response line by line
        print("Received data:")
        body = await response.text()
        for line in body.splitlines():
            if line.strip():
                try:
                    data = json.loads(line)
                    print(f"  Received item {data.get('id', 'unknown')}")
                except json.JSONDecodeError:
                    print(f"  Invalid JSON: {line}")


async def connection_pooling() -> None:
    """Demonstrate connection pooling behavior."""
    print("\n=== Connection Pooling ===")
    
    # Create client with smaller connection pool for demonstration
    async with Client(max_connections=5) as client:
        print("Making 20 sequential requests to same host...")
        
        start_time = time.time()
        
        # Sequential requests to same host should reuse connections
        for i in range(20):
            response = await client.get("https://httpbin.org/get", 
                                       params={"request_id": i})
            print(f"Request {i+1}: {response.status_code}")
        
        elapsed = time.time() - start_time
        print(f"Completed in {elapsed:.3f} seconds")
        
        # Now make parallel requests
        print("\nMaking 20 parallel requests to same host...")
        
        start_time = time.time()
        tasks = [client.get("https://httpbin.org/get", params={"request_id": i}) 
                for i in range(20)]
        
        # Execute all requests in parallel
        responses = await asyncio.gather(*tasks)
        
        elapsed = time.time() - start_time
        print(f"Completed in {elapsed:.3f} seconds")
        print(f"All responses successful: {all(r.status_code == 200 for r in responses)}")


async def error_handling() -> None:
    """Demonstrate error handling."""
    print("\n=== Error Handling ===")
    
    async with Client() as client:
        # 404 Not Found
        try:
            print("Making request to non-existent endpoint...")
            response = await client.get("https://httpbin.org/status/404")
            response.raise_for_status()  # This will raise an exception
        except Exception as e:
            print(f"Caught exception as expected: {e}")
        
        # Connection error
        try:
            print("\nMaking request to non-existent host...")
            response = await client.get("https://non-existent-host.example", 
                                       timeout=2.0)
        except Exception as e:
            print(f"Caught exception as expected: {e}")
        
        # Timeout error
        try:
            print("\nMaking request that will timeout...")
            response = await client.get("https://httpbin.org/delay/3", 
                                       timeout=1.0)
        except Exception as e:
            print(f"Caught exception as expected: {e}")


async def main() -> None:
    """Run all advanced examples."""
    await parallel_requests()
    await custom_retry_policy()
    await streaming_response()
    await connection_pooling()
    await error_handling()


if __name__ == "__main__":
    asyncio.run(main())