import asyncio
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
async def test_matrix_sink_retries_on_5xx_and_succeeds(http_client):
    sink_config = {
        "room_id": "!abc123:matrix.example.com",
        "access_token": "secret-token",
        "homeserver_url": "https://matrix.example.com",
    }
    sink = MatrixSink(sink_id="test-matrix", config=sink_config, client=http_client)

    route = respx.put(
        url=re.compile(r"https://matrix\.example\.com/_matrix/client/v3/rooms/.*")
    )
    route.side_effect = [
        httpx.Response(500, text="Internal Server Error"),
        httpx.Response(200, json={"event_id": "$12345"}),
    ]

    success, error = await sink.send("Hello Retry!")
    assert success is True
    assert error is None
    assert route.call_count == 2
    # Verify same txn_id in both calls
    assert route.calls[0].request.url == route.calls[1].request.url


@pytest.mark.asyncio
@respx.mock
async def test_matrix_sink_retries_on_429_and_succeeds(http_client):
    sink_config = {
        "room_id": "!abc123:matrix.example.com",
        "access_token": "secret-token",
        "homeserver_url": "https://matrix.example.com",
    }
    sink = MatrixSink(sink_id="test-matrix", config=sink_config, client=http_client)

    route = respx.put(
        url=re.compile(r"https://matrix\.example\.com/_matrix/client/v3/rooms/.*")
    )
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0.01"}, text="Too Many Requests"),
        httpx.Response(200, json={"event_id": "$12345"}),
    ]

    success, error = await sink.send("Hello 429 Retry!")
    assert success is True
    assert error is None
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_matrix_sink_retry_after_clamped_to_max(http_client, monkeypatch):
    sink_config = {
        "room_id": "!abc123:matrix.example.com",
        "access_token": "secret-token",
        "homeserver_url": "https://matrix.example.com",
    }
    sink = MatrixSink(sink_id="test-matrix", config=sink_config, client=http_client)

    route = respx.put(
        url=re.compile(r"https://matrix\.example\.com/_matrix/client/v3/rooms/.*")
    )
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "300"}, text="Too Many Requests"),
        httpx.Response(200, json={"event_id": "$12345"}),
    ]

    sleep_calls = []
    async def mock_sleep(duration):
        sleep_calls.append(duration)

    monkeypatch.setattr(asyncio, "sleep", mock_sleep)

    success, error = await sink.send("Hello Clamped 429!")
    assert success is True
    assert error is None
    assert route.call_count == 2
    assert sleep_calls == [2.0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("header_val", "expected_sleep"),
    [
        ("invalid_string", 0.05),
        ("0", 0.0),
        ("-10", 0.0),
    ],
)
@respx.mock
async def test_retry_after_edge_cases(http_client, monkeypatch, header_val, expected_sleep):
    sink_config = {
        "room_id": "!abc123:matrix.example.com",
        "access_token": "secret-token",
        "homeserver_url": "https://matrix.example.com",
    }
    sink = MatrixSink(sink_id="test-matrix", config=sink_config, client=http_client)

    route = respx.put(
        url=re.compile(r"https://matrix\.example\.com/_matrix/client/v3/rooms/.*")
    )
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": header_val}, text="Too Many Requests"),
        httpx.Response(200, json={"event_id": "$12345"}),
    ]

    sleep_calls = []
    async def mock_sleep(duration):
        sleep_calls.append(duration)

    monkeypatch.setattr(asyncio, "sleep", mock_sleep)

    success, error = await sink.send("Hello Edge Case!")
    assert success is True
    assert error is None
    assert route.call_count == 2
    assert sleep_calls == [expected_sleep]


@pytest.mark.asyncio
@respx.mock
async def test_matrix_sink_does_not_retry_on_4xx(http_client):
    sink_config = {
        "room_id": "!abc123:matrix.example.com",
        "access_token": "secret-token",
        "homeserver_url": "https://matrix.example.com",
    }
    sink = MatrixSink(sink_id="test-matrix", config=sink_config, client=http_client)

    route = respx.put(
        url=re.compile(r"https://matrix\.example\.com/_matrix/client/v3/rooms/.*")
    ).mock(return_value=httpx.Response(403, text="Forbidden"))

    success, error = await sink.send("Hello 403!")
    assert success is False
    assert error is not None
    assert "403" in error
    assert route.call_count == 1


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
    assert any("error" in record.message for record in caplog.records)


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
    assert "403" in error
    assert any("client error" in record.message for record in caplog.records)


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
