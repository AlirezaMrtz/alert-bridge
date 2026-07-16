import json
import re
import httpx
import pytest
import respx
from app.sinks.matrix import MatrixSink


@pytest.mark.asyncio
@respx.mock
async def test_matrix_sink_success():
    sink_config = {
        "room_id": "!abc123:matrix.example.com",
        "access_token": "secret-token",
        "homeserver_url": "https://matrix.example.com"
    }
    sink = MatrixSink(sink_id="test-matrix", config=sink_config)

    route = respx.put(
        url=re.compile(r"https://matrix\.example\.com/_matrix/client/v3/rooms/.*")
    ).mock(return_value=httpx.Response(200, json={"event_id": "$12345"}))

    success = await sink.send("Hello World!")
    assert success is True
    assert route.called
    
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer secret-token"
    
    data = json.loads(request.content)
    assert data["msgtype"] == "m.text"
    assert data["body"] == "Hello World!"


@pytest.mark.asyncio
@respx.mock
async def test_matrix_sink_timeout(caplog):
    sink_config = {
        "room_id": "!abc123:matrix.example.com",
        "access_token": "secret-token",
        "homeserver_url": "https://matrix.example.com"
    }
    sink = MatrixSink(sink_id="test-matrix", config=sink_config)

    respx.put(
        url=re.compile(r"https://matrix\.example\.com/_matrix/client/v3/rooms/.*")
    ).mock(side_effect=httpx.TimeoutException("Connection timed out"))

    success = await sink.send("Hello World!")
    assert success is False
    assert any("failed to send" in record.message for record in caplog.records)


@pytest.mark.asyncio
@respx.mock
async def test_matrix_sink_non_2xx(caplog):
    sink_config = {
        "room_id": "!abc123:matrix.example.com",
        "access_token": "secret-token",
        "homeserver_url": "https://matrix.example.com"
    }
    sink = MatrixSink(sink_id="test-matrix", config=sink_config)

    respx.put(
        url=re.compile(r"https://matrix\.example\.com/_matrix/client/v3/rooms/.*")
    ).mock(return_value=httpx.Response(403, text="Forbidden"))

    success = await sink.send("Hello World!")
    assert success is False
    assert any("failed to send" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_matrix_sink_missing_homeserver_url_fails(caplog):
    """A sink without homeserver_url must fail immediately without guessing."""
    sink_config = {
        "room_id": "!abc123:matrix.example.com",
        "access_token": "secret-token",
        # homeserver_url intentionally absent
    }
    sink = MatrixSink(sink_id="test-matrix", config=sink_config)

    success = await sink.send("Hello World!")
    assert success is False
    assert any("missing required field 'homeserver_url'" in record.message for record in caplog.records)

