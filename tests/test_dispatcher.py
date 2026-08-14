import re
import httpx
import pytest
import respx
from app.config import SinkConfig
from app.dispatcher import SinkDispatcher, SinkResult


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_many_detailed_error_propagation(http_client):
    sinks = [
        SinkConfig(
            id="matrix-sink",
            type="matrix",
            config={
                "room_id": "!room:example.com",
                "access_token": "token",
                "homeserver_url": "https://matrix.example.com",
            },
        ),
        SinkConfig(
            id="webhook-sink",
            type="webhook",
            config={
                "url": "https://hooks.example.com/alert",
            },
        ),
    ]
    dispatcher = SinkDispatcher(sinks, http_client)

    # Mock matrix sink to fail with 500 error
    respx.put(re.compile(r"https://matrix\.example\.com/.*")).mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    # Mock webhook sink to fail with timeout
    respx.post("https://hooks.example.com/alert").mock(
        side_effect=httpx.TimeoutException("Connection timed out")
    )

    results = await dispatcher.dispatch_many(
        ["matrix-sink", "webhook-sink", "missing-sink"],
        "Critical Alert",
    )

    assert len(results) == 3

    # Check matrix sink result
    res_matrix = next(r for r in results if r.sink_id == "matrix-sink")
    assert res_matrix.success is False
    assert res_matrix.error is not None
    assert "500 Internal Server Error" in res_matrix.error

    # Check webhook sink result
    res_webhook = next(r for r in results if r.sink_id == "webhook-sink")
    assert res_webhook.success is False
    assert res_webhook.error is not None
    assert "Connection timed out" in res_webhook.error

    # Check missing sink result
    res_missing = next(r for r in results if r.sink_id == "missing-sink")
    assert res_missing.success is False
    assert res_missing.error is not None
    assert "Sink 'missing-sink' not found in configuration" in res_missing.error
