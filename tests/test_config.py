from pathlib import Path

import pytest

from app.config import ConfigError, load_config

FIXTURES = Path(__file__).parent / "fixtures"


def test_valid_config_loads(monkeypatch):
    monkeypatch.setenv("MATRIX_TOKEN", "real-token-value")
    config = load_config(FIXTURES / "valid_config.yaml")

    assert config.api_version == "v1alpha1"
    route = config.route_by_id("prometheus-in")
    assert route is not None
    assert route.forward_to == ["matrix-ops"]
    assert str(route.secret_token) == "**********"
    assert route.secret_token.get_secret_value() == "supersecrettoken123"

    sink = config.sink_by_id("matrix-ops")
    assert sink is not None
    assert sink.config["access_token"] == "real-token-value"


def test_missing_env_var_raises(monkeypatch):
    monkeypatch.delenv("MATRIX_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="MATRIX_TOKEN"):
        load_config(FIXTURES / "valid_config.yaml")


def test_malformed_yaml_raises():
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(FIXTURES / "malformed.yaml")


def test_missing_required_field_raises():
    with pytest.raises(ConfigError, match="validation failed"):
        load_config(FIXTURES / "missing_field.yaml")


def test_missing_file_raises():
    with pytest.raises(ConfigError, match="not found"):
        load_config(FIXTURES / "does_not_exist.yaml")


def test_nonexistent_template_ref_raises(monkeypatch):
    monkeypatch.setenv("MATRIX_TOKEN", "real-token-value")
    with pytest.raises(ConfigError, match="references nonexistent template"):
        load_config(FIXTURES / "nonexistent_template.yaml")


def test_matrix_sink_missing_homeserver_url_raises(monkeypatch):
    monkeypatch.setenv("MATRIX_TOKEN", "real-token-value")
    with pytest.raises(ConfigError, match="missing required field 'homeserver_url'"):
        load_config(FIXTURES / "matrix_missing_homeserver.yaml")