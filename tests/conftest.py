import httpx
import pytest_asyncio


@pytest_asyncio.fixture
async def http_client():
    client = httpx.AsyncClient()
    try:
        yield client
    finally:
        await client.aclose()
