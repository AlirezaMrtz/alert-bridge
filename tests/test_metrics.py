import asyncio

import httpx
import pytest

from app.config import SinkConfig
from app.dispatcher import SinkDispatcher
from app.main import app
from app.metrics import DeliveryMetrics
from tests.test_admission import client, HEADERS, URL


def test_histogram_cumulative_buckets_and_label_escaping():
    sink = 'room"\\\nname'
    metrics = DeliveryMetrics([sink])
    metrics.record_delivery(sink, "success", 0.05)
    metrics.record_delivery(sink, "failure", 0.5)
    text = metrics.render(capacity=8, outstanding=2, concurrency=1)
    samples = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert len(samples) == len({line.rsplit(" ", 1)[0] for line in samples})
    assert 'sink_id="room\\"\\\\\\nname"' in text
    buckets = [float(line.rsplit(" ", 1)[1]) for line in samples if "_bucket{" in line]
    assert buckets == [0, 1, 1, 2, 2, 2, 2, 2, 2]
    assert metrics.sinks[sink]["sum"] == pytest.approx(0.55)
    assert '_count{sink_id="room\\"\\\\\\nname"} 2\n' in text
    assert text.endswith("\n")
    metrics.record_delivery("unknown-user-input", "success", 1)
    assert "unknown-user-input" not in metrics.render(capacity=8, outstanding=0, concurrency=1)


@pytest.mark.asyncio
async def test_scrape_works_at_capacity_and_counts_only_accepted_and_overloaded(client):
    assert (await client.post(URL, headers=HEADERS, json={})).status_code == 202
    assert (await client.post(URL, headers=HEADERS, content=b"bad")).status_code == 422
    held = app.state.admission.try_reserve(2)
    try:
        assert (await client.post(URL, headers=HEADERS, json={})).status_code == 503
        response = await client.get("/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
        assert 'alert_bridge_alerts_total{outcome="accepted"} 1\n' in response.text
        assert 'alert_bridge_alerts_total{outcome="overloaded"} 1\n' in response.text
        assert 'alert_bridge_delivery_outstanding 2\n' in response.text
        assert 'supersecrettoken123' not in response.text
    finally:
        held.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http", "network"])
@pytest.mark.parametrize("sink_type", ["webhook", "matrix"])
async def test_real_dispatch_counts_retries_and_outcome_without_secret_labels(failure, sink_type):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if failure == "network":
            raise httpx.ConnectError("private-credential", request=request)
        return httpx.Response(500 if calls == 1 else 200, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        config = {
            "url": "https://example.com/private-credential", "headers": {"Authorization": "header-credential"},
        } if sink_type == "webhook" else {
            "room_id": "!local:example.com", "access_token": "header-credential",
            "homeserver_url": "https://example.com",
        }
        dispatcher = SinkDispatcher([SinkConfig(id="local", type=sink_type, config=config)], http_client)
        result = (await dispatcher.dispatch_many(["local"], "private-payload"))[0]
    metrics = dispatcher.metrics
    assert metrics.active == 0
    assert metrics.sinks["local"]["count"] == 1
    assert metrics.sinks["local"]["retries"][failure] == (1 if failure == "http" else 2)
    assert result.success is (failure == "http")
    outcome = "success" if result.success else "failure"
    assert metrics.sinks["local"]["outcomes"][outcome] == 1
    text = metrics.render(capacity=2, outstanding=0, concurrency=1)
    assert all(value not in text for value in ["private-credential", "header-credential", "private-payload", "https://"])


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [True, False])
async def test_timeout_and_cancellation_record_once_and_clear_active(monkeypatch, cancel):
    entered = asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("app.sinks.webhook.WebhookSink.send", blocked)
    monkeypatch.setattr("app.dispatcher.DISPATCH_TIMEOUT_SECONDS", 0.05)
    async with httpx.AsyncClient() as http_client:
        dispatcher = SinkDispatcher([SinkConfig(id="local", type="webhook", config={"url": "http://example.com"})], http_client)
        task = asyncio.create_task(dispatcher.dispatch_many(["local"], "hello"))
        await asyncio.wait_for(entered.wait(), 1)
        assert dispatcher.metrics.active == 1
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task)[0].success is False
    assert dispatcher.metrics.active == 0
    assert dispatcher.metrics.sinks["local"]["outcomes"]["cancelled" if cancel else "timeout"] == 1
    assert dispatcher.metrics.sinks["local"]["count"] == 1


def test_metrics_instances_do_not_share_state():
    first = DeliveryMetrics(["local"])
    first.alerts["accepted"] += 1
    first.record_delivery("local", "success", 0.1)
    second = DeliveryMetrics(["local"])
    assert second.alerts["accepted"] == 0
    assert second.sinks["local"]["count"] == 0


@pytest.mark.asyncio
async def test_cancelling_fanout_records_waiting_targets_without_active_gauge_leaks(monkeypatch):
    entered = asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("app.sinks.webhook.WebhookSink.send", blocked)
    async with httpx.AsyncClient() as client:
        dispatcher = SinkDispatcher([SinkConfig(id="local", type="webhook", config={"url": "http://example.com"})], client, 1)
        task = asyncio.create_task(dispatcher.dispatch_many(["local", "local"], "hello"))
        await asyncio.wait_for(entered.wait(), 1)
        assert dispatcher.metrics.active == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert dispatcher.metrics.active == 0
    assert dispatcher.metrics.sinks["local"]["count"] == 2
    assert dispatcher.metrics.sinks["local"]["outcomes"]["cancelled"] == 2
