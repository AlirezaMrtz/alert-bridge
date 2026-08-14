from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.dispatcher import SinkResult

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def mock_dispatch_many(monkeypatch):
    mock = AsyncMock(return_value=[SinkResult(sink_id="matrix-ops", success=True)])
    monkeypatch.setattr("app.dispatcher.SinkDispatcher.dispatch_many", mock)
    return mock


@pytest.fixture
def client(monkeypatch, mock_dispatch_many):
    # Dynamically inject the test configuration fixture into app state for tests
    monkeypatch.setenv("MATRIX_TOKEN", "mock-matrix-token")
    monkeypatch.setattr("app.main.CONFIG_PATH", str(FIXTURES / "valid_config.yaml"))

    with TestClient(app) as client:
        yield client


def test_health_check(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_alert_ingress_authorized_header(client, mock_dispatch_many):
    payload = {"message": "Database backup completed successfully"}
    response = client.post(
        "/alerts/prometheus-in",
        headers={"X-Secret-Token": "supersecrettoken123"},
        json=payload,
    )
    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "accepted"
    assert data["route"] == "prometheus-in"
    assert data["scheduled_sinks"] == ["matrix-ops"]
    mock_dispatch_many.assert_called_once_with(["matrix-ops"], "**Alert**: Database backup completed successfully")


def test_alert_ingress_query_params_passed(client, mock_dispatch_many):
    payload = {"message": "Service degraded"}
    response = client.post(
        "/alerts/prometheus-in?env=production",
        headers={"X-Secret-Token": "supersecrettoken123"},
        json=payload,
    )
    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "accepted"
    assert data["route"] == "prometheus-in"
    assert data["scheduled_sinks"] == ["matrix-ops"]
    mock_dispatch_many.assert_called_once_with(["matrix-ops"], "**Alert**: Service degraded")


def test_token_query_param_scrubbed_from_template_context(client, mock_dispatch_many):
    payload = {"message": "Service degraded"}
    response = client.post(
        "/alerts/prometheus-in?token=secret123&env=prod",
        headers={"X-Secret-Token": "supersecrettoken123"},
        json=payload,
    )
    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "accepted"
    mock_dispatch_many.assert_called_once_with(["matrix-ops"], "**Alert**: Service degraded")


def test_alert_ingress_render_failure_returns_500_and_does_not_dispatch(client, mock_dispatch_many, monkeypatch):
    mock_render = AsyncMock(return_value=(False, "Template rendering failed: simulation"))
    monkeypatch.setattr("app.engine.TemplateEngine.render", mock_render)

    response = client.post(
        "/alerts/prometheus-in",
        headers={"X-Secret-Token": "supersecrettoken123"},
        json={"message": "Failure payload"},
    )
    assert response.status_code == 500
    assert "Template rendering failed: simulation" in response.json()["detail"]
    mock_dispatch_many.assert_not_called()


def test_alert_ingress_query_token_rejected(client):
    response = client.post(
        "/alerts/prometheus-in?token=supersecrettoken123",
        json={"message": "Query token attempt"},
    )
    assert response.status_code == 401
    assert "Invalid or missing secret token" in response.json()["detail"]


def test_alert_ingress_unauthorized(client):
    response = client.post(
        "/alerts/prometheus-in",
        json={"message": "Hack attempt"},
    )
    assert response.status_code == 401
    assert "Invalid or missing secret token" in response.json()["detail"]


def test_alert_ingress_wrong_token(client):
    response = client.post(
        "/alerts/prometheus-in",
        headers={"X-Secret-Token": "wrong-token"},
        json={"message": "Hello"},
    )
    assert response.status_code == 401


def test_alert_ingress_route_not_found(client):
    response = client.post(
        "/alerts/non-existent-route",
        headers={"X-Secret-Token": "some-token"},
        json={},
    )
    assert response.status_code == 404


def test_alert_ingress_malformed_json(client):
    response = client.post(
        "/alerts/prometheus-in",
        headers={
            "X-Secret-Token": "supersecrettoken123",
            "Content-Type": "application/json",
        },
        content="{ malformed: json }",
    )
    assert response.status_code == 422
