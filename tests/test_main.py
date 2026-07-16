from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app.main import app

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def client(monkeypatch):
    # Dynamically inject the test configuration fixture into app state for tests
    monkeypatch.setenv("MATRIX_TOKEN", "mock-matrix-token")
    monkeypatch.setattr("app.main.CONFIG_PATH", str(FIXTURES / "valid_config.yaml"))
    
    # Mock SinkDispatcher.dispatch to prevent downstream network requests during ingress tests
    async def mock_dispatch(self, sink_id: str, message: str) -> bool:
        return True
    monkeypatch.setattr("app.dispatcher.SinkDispatcher.dispatch", mock_dispatch)

    with TestClient(app) as client:
        yield client


def test_alert_ingress_authorized_header(client):
    payload = {"message": "Database backup completed successfully"}
    response = client.post(
        "/alerts/prometheus-in",
        headers={"X-Secret-Token": "supersecrettoken123"},
        json=payload,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["route"] == "prometheus-in"
    assert "matrix-ops" in data["dispatched_to"]


def test_alert_ingress_authorized_query_param(client):
    payload = {"message": "Service degraded"}
    response = client.post(
        "/alerts/prometheus-in?token=supersecrettoken123",
        json=payload,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "success"


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

