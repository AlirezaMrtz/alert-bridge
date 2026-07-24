import json
import httpx
import pytest
import respx
from app.sinks.webhook import WebhookSink


@pytest.mark.asyncio
@respx.mock
async def test_webhook_sink_default_text_field():
    sink_config = {
        "url": "https://hooks.example.com/services/123",
    }
    sink = WebhookSink(sink_id="test-webhook", config=sink_config)

    route = respx.post("https://hooks.example.com/services/123").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )

    success, error = await sink.send("Alert: CPU High")
    assert success is True
    assert error is None
    assert route.called

    request = route.calls.last.request
    assert request.method == "POST"
    data = json.loads(request.content)
    assert data == {"text": "Alert: CPU High"}


@pytest.mark.asyncio
@respx.mock
async def test_webhook_sink_custom_text_field():
    sink_config = {
        "url": "https://discord.example.com/api/webhooks/123",
        "text_field": "content",
    }
    sink = WebhookSink(sink_id="discord-webhook", config=sink_config)

    route = respx.post("https://discord.example.com/api/webhooks/123").mock(
        return_value=httpx.Response(204)
    )

    success, error = await sink.send("Alert: Memory High")
    assert success is True
    assert error is None
    assert route.called

    data = json.loads(route.calls.last.request.content)
    assert data == {"content": "Alert: Memory High"}


@pytest.mark.asyncio
@respx.mock
async def test_webhook_sink_extra_fields_merged():
    sink_config = {
        "url": "https://hooks.example.com/custom",
        "text_field": "message",
        "extra_fields": {
            "channel": "#alerts",
            "username": "AlertBot",
            "severity": "critical",
        },
    }
    sink = WebhookSink(sink_id="custom-webhook", config=sink_config)

    route = respx.post("https://hooks.example.com/custom").mock(
        return_value=httpx.Response(200)
    )

    success, error = await sink.send("Alert: Disk Full")
    assert success is True
    assert error is None
    assert route.called

    data = json.loads(route.calls.last.request.content)
    assert data == {
        "channel": "#alerts",
        "username": "AlertBot",
        "severity": "critical",
        "message": "Alert: Disk Full",
    }


@pytest.mark.asyncio
@respx.mock
async def test_webhook_sink_custom_method_and_headers():
    sink_config = {
        "url": "https://api.example.com/alerts",
        "method": "PUT",
        "headers": {"Authorization": "Bearer token123", "X-Custom-Header": "value"},
    }
    sink = WebhookSink(sink_id="put-webhook", config=sink_config)

    route = respx.put("https://api.example.com/alerts").mock(
        return_value=httpx.Response(200)
    )

    success, error = await sink.send("Alert: Service Restored")
    assert success is True
    assert error is None
    assert route.called

    request = route.calls.last.request
    assert request.method == "PUT"
    assert request.headers["Authorization"] == "Bearer token123"
    assert request.headers["X-Custom-Header"] == "value"


@pytest.mark.asyncio
@respx.mock
async def test_webhook_sink_timeout(caplog):
    sink_config = {
        "url": "https://hooks.example.com/timeout",
    }
    sink = WebhookSink(sink_id="timeout-webhook", config=sink_config)

    respx.post("https://hooks.example.com/timeout").mock(
        side_effect=httpx.TimeoutException("Connection timed out")
    )

    success, error = await sink.send("Alert Test")
    assert success is False
    assert error is not None
    assert "Connection timed out" in error
    assert any("failed to send message" in record.message for record in caplog.records)


@pytest.mark.asyncio
@respx.mock
async def test_webhook_sink_5xx_error(caplog):
    sink_config = {
        "url": "https://hooks.example.com/500",
    }
    sink = WebhookSink(sink_id="error-webhook", config=sink_config)

    respx.post("https://hooks.example.com/500").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    success, error = await sink.send("Alert Test")
    assert success is False
    assert error is not None
    assert "500 Internal Server Error" in error
    assert any("failed to send message" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_webhook_sink_missing_url(caplog):
    sink = WebhookSink(sink_id="missing-url-webhook", config={})
    success, error = await sink.send("Alert Test")
    assert success is False
    assert error is not None
    assert "missing required configuration 'url'" in error
    assert any("missing required configuration 'url'" in record.message for record in caplog.records)
