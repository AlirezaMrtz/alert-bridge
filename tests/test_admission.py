import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError
from starlette.background import BackgroundTask

from app.admission import DeliveryAdmission
from app.config import DeliveryConfig
from app.main import DeliveryResponse, app, lifespan, MAX_ALERT_BODY_BYTES
from app.dispatcher import SinkDispatcher

REAL_DISPATCH_MANY = SinkDispatcher.dispatch_many

HEADERS = {"X-Secret-Token": "supersecrettoken123", "Content-Type": "application/json"}
URL = "/alerts/prometheus-in"


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setenv("MATRIX_TOKEN", "test-token")
    monkeypatch.setattr("app.main.CONFIG_PATH", str(Path(__file__).parent / "fixtures/valid_config.yaml"))
    monkeypatch.setattr("app.dispatcher.SinkDispatcher.dispatch_many", AsyncMock(return_value=[]))
    async with lifespan(app):
        app.state.admission = DeliveryAdmission(2)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client


@pytest.mark.asyncio
async def test_saturation_rejects_without_waiting_and_recovers(client, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow_dispatch(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            started.set()
        await release.wait()

    monkeypatch.setattr(app.state.dispatcher, "dispatch_many", slow_dispatch)
    accepted = [asyncio.create_task(client.post(URL, headers=HEADERS, json={})) for _ in range(2)]
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert app.state.admission.in_use == 2
        rejected = await asyncio.wait_for(asyncio.gather(*[
            client.post(URL, headers=HEADERS, content=b"malformed JSON") for _ in range(20)
        ]), 2)
        assert all(r.status_code == 503 and r.headers["retry-after"] == "1" for r in rejected)
        assert calls == 2
        assert app.state.admission.in_use == 2
        assert (await client.get("/health")).status_code == 200
        assert (await client.post(URL, json={})).status_code == 401
        assert (await client.post("/alerts/missing", json={})).status_code == 404
    finally:
        release.set()
        responses = await asyncio.gather(*accepted)
    assert all(r.status_code == 202 for r in responses)
    assert app.state.admission.in_use == 0
    assert (await client.post(URL, headers=HEADERS, json={})).status_code == 202
    assert app.state.admission.in_use == 0


@pytest.mark.asyncio
async def test_fanout_is_reserved_atomically(client):
    route = app.state.config.routes[0].model_copy(update={"forward_to": ("matrix-ops", "matrix-ops")})
    app.state.config = app.state.config.model_copy(update={"routes": [route]})
    held = app.state.admission.try_reserve(1)
    try:
        response = await client.post(URL, headers=HEADERS, json={})
        assert response.status_code == 503
        assert app.state.admission.in_use == 1
        app.state.dispatcher.dispatch_many.assert_not_called()
    finally:
        held.release()
    assert (await client.post(URL, headers=HEADERS, json={})).status_code == 202
    assert app.state.admission.in_use == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("body,code", [
    pytest.param(b"broken", 422, id="malformed-json"),
    pytest.param(b"[]", 422, id="non-object-json"),
    pytest.param(b"x" * (MAX_ALERT_BODY_BYTES + 1), 413, id="oversized-body"),
])
async def test_invalid_body_releases_reservation(client, body, code):
    assert (await client.post(URL, headers=HEADERS, content=body)).status_code == code
    assert app.state.admission.in_use == 0
    app.state.dispatcher.dispatch_many.assert_not_called()


@pytest.mark.asyncio
async def test_render_failure_and_exception_release_reservation(client, monkeypatch):
    monkeypatch.setattr(app.state.engine, "render", AsyncMock(return_value=(False, "safe error")))
    assert (await client.post(URL, headers=HEADERS, json={})).status_code == 500
    assert app.state.admission.in_use == 0
    monkeypatch.setattr(app.state.engine, "render", AsyncMock(side_effect=RuntimeError("failure")))
    with pytest.raises(RuntimeError):
        await client.post(URL, headers=HEADERS, json={})
    assert app.state.admission.in_use == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["render", "dispatch"])
async def test_cancellation_releases_reservation(client, monkeypatch, phase):
    entered = asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    owner, name = (app.state.engine, "render") if phase == "render" else (app.state.dispatcher, "dispatch_many")
    monkeypatch.setattr(owner, name, blocked)
    task = asyncio.create_task(client.post(URL, headers=HEADERS, json={}))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert app.state.admission.in_use == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert app.state.admission.in_use == 0


@pytest.mark.asyncio
async def test_failed_response_send_releases_capacity_without_dispatch():
    admission = DeliveryAdmission(1)
    reservation = admission.try_reserve(1)
    dispatch = AsyncMock()
    response = DeliveryResponse(reservation, content={}, background=BackgroundTask(dispatch))

    async def send(message):
        raise OSError("disconnected")

    with pytest.raises(OSError):
        await response({"type": "http"}, AsyncMock(), send)
    assert admission.in_use == 0
    dispatch.assert_not_called()
    reservation.release()
    assert admission.in_use == 0


@pytest.mark.asyncio
async def test_background_exception_releases_capacity(client, monkeypatch):
    monkeypatch.setattr(app.state.dispatcher, "dispatch_many", AsyncMock(side_effect=RuntimeError("failure")))
    with pytest.raises(RuntimeError):
        await client.post(URL, headers=HEADERS, json={})
    assert app.state.admission.in_use == 0


@pytest.mark.asyncio
async def test_delivery_timeout_releases_capacity_and_allows_recovery(client, monkeypatch):
    from types import MethodType

    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr("app.dispatcher.DISPATCH_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("app.sinks.matrix.MatrixSink.send", blocked)
    monkeypatch.setattr(app.state.dispatcher, "dispatch_many", MethodType(REAL_DISPATCH_MANY, app.state.dispatcher))
    assert (await client.post(URL, headers=HEADERS, json={})).status_code == 202
    assert app.state.admission.in_use == 0
    monkeypatch.setattr("app.sinks.matrix.MatrixSink.send", AsyncMock(return_value=(True, None)))
    assert (await client.post(URL, headers=HEADERS, json={})).status_code == 202
    assert app.state.admission.in_use == 0


@pytest.mark.parametrize("field", ["max_outstanding", "max_concurrency"])
@pytest.mark.parametrize("value", [0, -1, True, "10", 1.5])
def test_delivery_settings_reject_invalid_limits(field, value):
    with pytest.raises(ValidationError):
        DeliveryConfig(**{field: value})


def test_configuration_rejects_route_larger_than_capacity():
    from app.config import BridgeConfig
    import yaml

    data = yaml.safe_load((Path(__file__).parent / "fixtures/valid_config.yaml").read_text())
    data["routes"][0]["forward_to"] *= 2
    data["delivery"] = {"max_outstanding": 1}
    with pytest.raises(ValidationError, match="fan-out exceeds"):
        BridgeConfig.model_validate(data)

@pytest.mark.asyncio
async def test_reservation_remains_until_the_last_fanout_target_finishes(client, monkeypatch):
    from types import MethodType

    route = app.state.config.routes[0].model_copy(update={"forward_to": ("matrix-ops", "matrix-ops")})
    app.state.config = app.state.config.model_copy(update={"routes": [route]})
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def send(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            await release.wait()
        return True, None

    monkeypatch.setattr("app.sinks.matrix.MatrixSink.send", send)
    monkeypatch.setattr(app.state.dispatcher, "dispatch_many", MethodType(REAL_DISPATCH_MANY, app.state.dispatcher))
    task = asyncio.create_task(client.post(URL, headers=HEADERS, json={}))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert app.state.admission.in_use == 2
        assert (await client.post(URL, headers=HEADERS, json={})).status_code == 503
    finally:
        release.set()
        assert (await task).status_code == 202
    assert app.state.admission.in_use == 0
