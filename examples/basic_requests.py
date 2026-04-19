"""Basic usage examples for hyperhttp."""

from __future__ import annotations

import asyncio

import hyperhttp


async def basic() -> None:
    async with hyperhttp.Client() as client:
        response = await client.get("https://httpbin.org/get")
        await response.aread()
        print(response.status_code, response.http_version)
        print(response.json())


async def with_params() -> None:
    async with hyperhttp.Client() as client:
        response = await client.get(
            "https://httpbin.org/get",
            params={"hello": "world"},
        )
        await response.aread()
        print(response.json()["args"])


async def post_json() -> None:
    async with hyperhttp.Client() as client:
        response = await client.post(
            "https://httpbin.org/post",
            json={"name": "hyperhttp", "v": 2},
        )
        await response.aread()
        print(response.json()["json"])


async def streaming_download() -> None:
    async with hyperhttp.Client() as client:
        async with await client.get("https://httpbin.org/stream/10") as response:
            async for line in response.aiter_lines():
                print(line.rstrip())


async def main() -> None:
    await basic()
    await with_params()
    await post_json()
    await streaming_download()


if __name__ == "__main__":
    hyperhttp.install_uvloop()
    asyncio.run(main())
