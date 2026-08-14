import json
import re
import httpx
import pytest
import respx
from app.sinks.matrix import MatrixSink


@pytest.mark.asyncio
@respx.mock
async def test_matrix_sink_success(http_client):
    sink_config = {
        "room_id": "!abc123:matrix.example.com",
        "access_token": "secret-token",
        "homeserver_url": "https://matrix.example.com"
    }
    sink = MatrixSink(sink_id="test-matrix", config=sink_config, client=http_client)

    route = respx.put(
        url=re.compile(r"https://matrix\.example\.com/_matrix/client/v3/rooms/.*")
    ).mock(return_value=httpx.Response(200, json={"event_id": "$12345"}))

    success, error = await sink.send("Hello World!")
    assert success is True
    assert error is None
    assert route.called
    
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer secret-token"
    
    data = json.loads(request.content)
    assert data["msgtype"] == "m.text"
    assert data["body"] == "Hello World!"


@pytest.mark.asyncio
@respx.mock
async def test_matrix_sink_url_encoding_special_chars(http_client):
    sink_config = {
        "room_id": "!room with space:server?token=LEAK",
        "access_token": "secret-token",
        "homeserver_url": "https://matrix.example.com",
    }
    sink = MatrixSink(sink_id="test-matrix-special", config=sink_config, client=http_client)

    route = respx.put(
        url=re.compile(r"https://matrix\.example\.com/_matrix/client/v3/rooms/.*")
    ).mock(return_value=httpx.Response(200, json={"event_id": "$12345"}))

    success, error = await sink.send("Hello Special")
    assert success is True
    assert error is None
    assert route.called

    request = route.calls.last.request
    request_url_str = str(request.url)
    assert "!room%20with%20space:server%3Ftoken%3DLEAK" in request_url_str
    assert "?token=LEAK" not in request_url_str
    assert "+" not in request_url_str


@pytest.mark.asyncio
@respx.mock
async def test_matrix_sink_timeout(http_client, caplog):
    sink_config = {
        "room_id": "!abc123:matrix.example.com",
        "access_token": "secret-token",
        "homeserver_url": "https://matrix.example.com"
    }
    sink = MatrixSink(sink_id="test-matrix", config=sink_config, client=http_client)

    respx.put(
        url=re.compile(r"https://matrix\.example\.com/_matrix/client/v3/rooms/.*")
    ).mock(side_effect=httpx.TimeoutException("Connection timed out"))

    success, error = await sink.send("Hello World!")
    assert success is False
    assert error is not None
    assert "Connection timed out" in error
    assert any("failed to send" in record.message for record in caplog.records)


@pytest.mark.asyncio
@respx.mock
async def test_matrix_sink_non_2xx(http_client, caplog):
    sink_config = {
        "room_id": "!abc123:matrix.example.com",
        "access_token": "secret-token",
        "homeserver_url": "https://matrix.example.com"
    }
    sink = MatrixSink(sink_id="test-matrix", config=sink_config, client=http_client)

    respx.put(
        url=re.compile(r"https://matrix\.example\.com/_matrix/client/v3/rooms/.*")
    ).mock(return_value=httpx.Response(403, text="Forbidden"))

    success, error = await sink.send("Hello World!")
    assert success is False
    assert error is not None
    assert "403 Forbidden" in error
    assert any("failed to send" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_matrix_sink_missing_homeserver_url_fails(http_client, caplog):
    """A sink without homeserver_url must fail immediately without guessing."""
    sink_config = {
        "room_id": "!abc123:matrix.example.com",
        "access_token": "secret-token",
        # homeserver_url intentionally absent
    }
    sink = MatrixSink(sink_id="test-matrix", config=sink_config, client=http_client)

    success, error = await sink.send("Hello World!")
    assert success is False
    assert error is not None
    assert "missing required field 'homeserver_url'" in error
    assert any("missing required field 'homeserver_url'" in record.message for record in caplog.records)
