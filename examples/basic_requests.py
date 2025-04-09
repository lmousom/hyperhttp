"""
Basic usage examples for HyperHTTP client.
"""

import asyncio
import json
import time

from hyperhttp import Client


async def basic_requests() -> None:
    """Demonstrate basic request types."""
    # Create client
    client = Client()
    
    try:
        # Basic GET request
        print("Making GET request...")
        start = time.time()
        response = await client.get("https://httpbin.org/get")
        elapsed = time.time() - start
        
        print(f"GET response: {response.status_code} ({elapsed:.3f}s)")
        print("Response headers:")
        for name, value in response.headers.items():
            print(f"  {name}: {value}")
            
        # Access JSON response data
        data = await response.json()
        print(f"Response data: {json.dumps(data, indent=2)}")
        
        # POST request with JSON payload
        print("\nMaking POST request with JSON...")
        post_data = {"name": "HyperHTTP", "type": "HTTP client"}
        response = await client.post(
            "https://httpbin.org/post",
            json=post_data
        )
        print(f"POST response: {response.status_code}")
        data = await response.json()
        print(f"Sent data: {json.dumps(data['json'], indent=2)}")
        
        # PUT request
        print("\nMaking PUT request...")
        response = await client.put(
            "https://httpbin.org/put",
            data="Raw data content"
        )
        print(f"PUT response: {response.status_code}")
        
        # DELETE request
        print("\nMaking DELETE request...")
        response = await client.delete("https://httpbin.org/delete")
        print(f"DELETE response: {response.status_code}")
        
    finally:
        # Close the client
        await client.close()


async def request_with_params() -> None:
    """Demonstrate request with query parameters."""
    async with Client() as client:
        # Request with query parameters
        print("\nMaking request with query parameters...")
        response = await client.get(
            "https://httpbin.org/get",
            params={"param1": "value1", "param2": "value2"}
        )
        print(f"Response: {response.status_code}")
        data = await response.json()
        print(f"Query args: {json.dumps(data['args'], indent=2)}")


async def request_with_headers() -> None:
    """Demonstrate request with custom headers."""
    async with Client() as client:
        # Request with custom headers
        print("\nMaking request with custom headers...")
        response = await client.get(
            "https://httpbin.org/headers",
            headers={
                "X-Custom-Header": "CustomValue",
                "User-Agent": "HyperHTTP/Example"
            }
        )
        print(f"Response: {response.status_code}")
        data = await response.json()
        print(f"Received headers: {json.dumps(data['headers'], indent=2)}")


async def request_with_timeout() -> None:
    """Demonstrate request with timeout."""
    async with Client() as client:
        # Request with timeout
        print("\nMaking request with timeout...")
        try:
            response = await client.get(
                "https://httpbin.org/delay/3",
                timeout=1.0
            )
            print(f"Response: {response.status_code}")
        except Exception as e:
            print(f"Request timed out as expected: {e}")


async def main() -> None:
    """Run all examples."""
    await basic_requests()
    await request_with_params()
    await request_with_headers()
    await request_with_timeout()


if __name__ == "__main__":
    asyncio.run(main())