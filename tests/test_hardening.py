import logging
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.main import MAX_ALERT_BODY_BYTES, app
from app.sinks.webhook import WebhookSink

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MATRIX_TOKEN", "test-matrix-token")
    monkeypatch.setattr("app.main.CONFIG_PATH", str(FIXTURES / "valid_config.yaml"))
    monkeypatch.setattr("app.dispatcher.SinkDispatcher.dispatch_many", AsyncMock())
    with TestClient(app) as test_client:
        yield test_client


def test_rejects_oversized_alert_without_dispatching(client, monkeypatch):
    dispatch = AsyncMock()
    monkeypatch.setattr("app.dispatcher.SinkDispatcher.dispatch_many", dispatch)
    response = client.post(
        "/alerts/prometheus-in",
        headers={"X-Secret-Token": "supersecrettoken123"},
        content=b"x" * (MAX_ALERT_BODY_BYTES + 1),
    )
    assert response.status_code == 413
    dispatch.assert_not_called()


def test_rejects_unauthenticated_body_before_json_parsing(client):
    response = client.post(
        "/alerts/prometheus-in",
        headers={"Content-Type": "application/json"},
        content=b"{malformed json",
    )
    assert response.status_code == 401


def test_rejects_non_object_json(client):
    response = client.post(
        "/alerts/prometheus-in",
        headers={"X-Secret-Token": "supersecrettoken123"},
        json=["not", "an", "object"],
    )
    assert response.status_code == 422


def test_openapi_still_documents_json_request_body(client):
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/alerts/{route_id}"]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"]["type"] == "object"


@pytest.mark.asyncio
async def test_webhook_http_error_does_not_log_secret_url(caplog):
    url = "https://hooks.example.com/services/private-token?token=another-secret"

    def handler(request):
        return httpx.Response(403, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        sink = WebhookSink("test-webhook", {"url": url}, http_client)
        success, error = await sink.send("test")

    assert success is False
    assert "HTTP 403" in error
    for value in ("private-token", "another-secret", url):
        assert value not in caplog.text
        assert value not in error


@pytest.mark.asyncio
async def test_webhook_network_error_does_not_log_secret_url(caplog):
    url = "https://hooks.example.com/services/private-token?token=another-secret"

    def handler(request):
        raise httpx.ConnectError(f"Failed to connect to {url}", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        sink = WebhookSink("test-webhook", {"url": url}, http_client)
        success, error = await sink.send("test")

    assert success is False
    assert "ConnectError" in error
    for value in ("private-token", "another-secret", url):
        assert value not in caplog.text
        assert value not in error


@pytest.mark.asyncio
async def test_dispatcher_does_not_return_raw_unhandled_exceptions(caplog, monkeypatch):
    from app.config import SinkConfig
    from app.dispatcher import SinkDispatcher

    url = "https://hooks.example.com/services/private-token"

    async def failing_send(self, message, *, fmt="text"):
        raise RuntimeError(f"Request failed: {url}")

    monkeypatch.setattr(WebhookSink, "send", failing_send)
    async with httpx.AsyncClient() as http_client:
        dispatcher = SinkDispatcher([SinkConfig(id="test", type="webhook", config={"url": url})], http_client)
        result = (await dispatcher.dispatch_many(["test"], "a secret alert"))[0]

    assert result.success is False
    assert "RuntimeError" in result.error
    assert url not in result.error
    assert url not in caplog.text

@pytest.mark.asyncio
async def test_matrix_network_error_does_not_log_sensitive_url(caplog):
    from app.sinks.matrix import MatrixSink

    url = "https://private-token@matrix.example.com"

    def handler(request):
        raise httpx.ConnectError(f"Failed to connect to {url}", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        sink = MatrixSink(
            "matrix",
            {"room_id": "!room:matrix.example.com", "access_token": "matrix-token", "homeserver_url": url},
            http_client,
        )
        success, error = await sink.send("test")

    assert success is False
    assert "ConnectError" in error
    for value in ("private-token", "matrix-token"):
        assert value not in caplog.text
        assert value not in error


@pytest.mark.asyncio
async def test_stream_without_content_length_is_limited():
    from fastapi import HTTPException
    from starlette.requests import Request
    from app.main import read_alert_payload

    chunks = iter([
        {"type": "http.request", "body": b"a" * MAX_ALERT_BODY_BYTES, "more_body": True},
        {"type": "http.request", "body": b"b", "more_body": False},
    ])

    async def receive():
        return next(chunks)

    request = Request({"type": "http", "method": "POST", "path": "/alerts/test", "headers": []}, receive)
    with pytest.raises(HTTPException) as exc_info:
        await read_alert_payload(request)
    assert exc_info.value.status_code == 413


@pytest.mark.parametrize("content_type", [None, "text/plain", "text/json"])
def test_non_json_content_type_is_rejected(client, content_type):
    headers = {"X-Secret-Token": "supersecrettoken123"}
    if content_type is not None:
        headers["Content-Type"] = content_type
    response = client.post("/alerts/prometheus-in", headers=headers, content=b'{}')
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "dict_type"


@pytest.mark.parametrize("content_type", [
    "application/json", "application/json; charset=utf-8", "application/problem+json",
])
def test_json_content_types_are_accepted(client, content_type):
    response = client.post(
        "/alerts/prometheus-in",
        headers={"X-Secret-Token": "supersecrettoken123", "Content-Type": content_type},
        content=b'{}',
    )
    assert response.status_code == 202


@pytest.mark.parametrize("body", [b"", b"null"])
def test_optional_body_preserves_empty_object_default(client, body):
    response = client.post(
        "/alerts/prometheus-in",
        headers={"X-Secret-Token": "supersecrettoken123", "Content-Type": "application/json"},
        content=body,
    )
    assert response.status_code == 202
    app.state.dispatcher.dispatch_many.assert_called_once_with(
        ("matrix-ops",), "**Alert**:", fmt="markdown",
    )


@pytest.mark.parametrize("body", [
    pytest.param(b'"text"', id="string"),
    pytest.param(b'42', id="number"),
    pytest.param(b'true', id="boolean"),
    pytest.param(b'\xff', id="invalid-encoding"),
    pytest.param(b'{"a":' + b'[' * 100000 + b'0' + b']' * 100000 + b'}', id="decoder-recursion-limit"),
])
def test_invalid_or_non_object_payload_does_not_dispatch(client, body):
    response = client.post(
        "/alerts/prometheus-in",
        headers={"X-Secret-Token": "supersecrettoken123", "Content-Type": "application/json"},
        content=body,
    )
    assert response.status_code == 422
    app.state.dispatcher.dispatch_many.assert_not_called()


@pytest.mark.parametrize("route,token,expected", [
    ("missing", "wrong", 404), ("prometheus-in", "wrong", 401),
])
def test_route_and_auth_checks_do_not_read_body(client, monkeypatch, route, token, expected):
    async def forbidden_stream(self):
        raise AssertionError("The request body must not be read before authentication")
        yield b""  # Keep this an async generator, like Request.stream.

    monkeypatch.setattr(Request, "stream", forbidden_stream)
    response = client.post(
        f"/alerts/{route}",
        headers={"X-Secret-Token": token, "Content-Length": str(MAX_ALERT_BODY_BYTES + 1)},
        content=b"invalid JSON",
    )
    assert response.status_code == expected
    app.state.dispatcher.dispatch_many.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("declared_length", [None, b"2"])
async def test_stream_limit_cannot_be_bypassed_by_content_length(declared_length):
    from fastapi import HTTPException
    from app.main import read_alert_payload

    headers = [] if declared_length is None else [(b"content-length", declared_length)]
    chunks = iter([
        {"type": "http.request", "body": b"a" * MAX_ALERT_BODY_BYTES, "more_body": True},
        {"type": "http.request", "body": b"b", "more_body": False},
    ])

    async def receive():
        return next(chunks)

    request = Request({"type": "http", "method": "POST", "path": "/", "headers": headers}, receive)
    with pytest.raises(HTTPException) as exc_info:
        await read_alert_payload(request)
    assert exc_info.value.status_code == 413


def test_alert_at_exact_size_limit_is_accepted(client):
    body = b'{"padding":"' + b'a' * (MAX_ALERT_BODY_BYTES - len(b'{"padding":""}')) + b'"}'
    assert len(body) == MAX_ALERT_BODY_BYTES
    response = client.post(
        "/alerts/prometheus-in",
        headers={"X-Secret-Token": "supersecrettoken123", "Content-Type": "application/json"},
        content=body,
    )
    assert response.status_code == 202


@pytest.mark.asyncio
async def test_template_errors_do_not_expose_payload_values(caplog):
    from app.engine import TemplateEngine

    secret = "sensitive-incident-value"
    success, error = await TemplateEngine().render("{{ payload[payload.key]() }}", {"key": secret}, {})
    assert success is False
    assert "UndefinedError" in error
    assert secret not in error
    assert secret not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("sink_type", ["webhook", "matrix"])
@pytest.mark.parametrize("status_code", [200, 403, 500, 599])
async def test_transport_logs_do_not_expose_secrets_at_debug(client, caplog, sink_type, status_code):
    from app.sinks.matrix import MatrixSink

    url = "https://hooks.example.com/private-path-secret?token=query-secret"
    config = {"url": url, "headers": {"Authorization": "Bearer header-secret"}}
    sink_class = WebhookSink
    if sink_type == "matrix":
        sink_class = MatrixSink
        config = {
            "room_id": "!private-room:matrix.example.com", "access_token": "matrix-token-secret",
            "homeserver_url": "https://matrix.example.com",
        }

    def handler(request):
        # Reason phrases come from remote servers and must not enter safe errors.
        return httpx.Response(status_code, request=request, extensions={"reason_phrase": b"remote-secret"})

    with caplog.at_level(logging.DEBUG):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            success, error = await sink_class("test", config, http_client).send("payload-secret")
    assert success is (status_code == 200)
    for secret in ("private-path-secret", "query-secret", "header-secret", "matrix-token-secret", "private-room", "remote-secret", "payload-secret"):
        assert secret not in caplog.text
        assert secret not in (error or "")


@pytest.mark.asyncio
async def test_dispatcher_does_not_log_rendered_message_at_debug(caplog):
    from app.config import SinkConfig
    from app.dispatcher import SinkDispatcher

    def handler(request):
        return httpx.Response(200, request=request)

    with caplog.at_level(logging.DEBUG, logger="bridge.dispatcher"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            dispatcher = SinkDispatcher([
                SinkConfig(id="test", type="webhook", config={"url": "https://hooks.example.com"}),
            ], http_client)
            result = (await dispatcher.dispatch_many(["test"], "payload-secret"))[0]
    assert result.success is True
    assert "message_len=14" in caplog.text
    assert "payload-secret" not in caplog.text
