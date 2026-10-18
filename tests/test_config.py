from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import (
    BridgeConfig,
    ConfigError,
    RouteConfig,
    SinkConfig,
    TemplateConfig,
    load_config,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_valid_config_loads(monkeypatch):
    monkeypatch.setenv("MATRIX_TOKEN", "real-token-value")
    config = load_config(FIXTURES / "valid_config.yaml")

    assert config.api_version == "v1alpha1"
    route = config.route_by_id("prometheus-in")
    assert route is not None
    assert route.forward_to == ("matrix-ops",)
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


def test_invalid_sink_type_raises():
    with pytest.raises(ConfigError, match="validation failed"):
        load_config(FIXTURES / "invalid_sink_type.yaml")


def test_empty_forward_to_raises(monkeypatch):
    monkeypatch.setenv("MATRIX_TOKEN", "real-token-value")
    with pytest.raises(ConfigError, match="validation failed"):
        load_config(FIXTURES / "empty_forward_to.yaml")


def test_nonexistent_forward_to_sink_raises(monkeypatch):
    monkeypatch.setenv("MATRIX_TOKEN", "real-token-value")
    with pytest.raises(ConfigError, match="references nonexistent sink"):
        load_config(FIXTURES / "nonexistent_forward_to_sink.yaml")


def test_webhook_sink_missing_url_raises():
    with pytest.raises(ConfigError, match="missing a valid 'url'"):
        load_config(FIXTURES / "webhook_missing_url.yaml")


def test_webhook_sink_invalid_method_raises():
    with pytest.raises(ConfigError, match="invalid HTTP method"):
        load_config(FIXTURES / "webhook_invalid_method.yaml")


def test_duplicate_sink_ids_raises():
    raw_data = {
        "apiVersion": "v1alpha1",
        "sinks": [
            {"id": "dup-sink", "type": "webhook", "config": {"url": "https://a.example.com"}},
            {"id": "dup-sink", "type": "webhook", "config": {"url": "https://b.example.com"}},
        ],
        "templates": [
            {"id": "t1", "engine": "jinja2", "format": "markdown", "body": "test"}
        ],
        "routes": [
            {"id": "r1", "secret_token": "token1", "template_ref": "t1", "forward_to": ["dup-sink"]}
        ],
    }
    with pytest.raises(ValidationError, match="Duplicate sink ID detected: 'dup-sink'"):
        BridgeConfig.model_validate(raw_data)


def test_duplicate_template_ids_raises():
    raw_data = {
        "apiVersion": "v1alpha1",
        "sinks": [
            {"id": "s1", "type": "webhook", "config": {"url": "https://a.example.com"}},
        ],
        "templates": [
            {"id": "dup-tmpl", "engine": "jinja2", "format": "markdown", "body": "test 1"},
            {"id": "dup-tmpl", "engine": "jinja2", "format": "markdown", "body": "test 2"},
        ],
        "routes": [
            {"id": "r1", "secret_token": "token1", "template_ref": "dup-tmpl", "forward_to": ["s1"]}
        ],
    }
    with pytest.raises(ValidationError, match="Duplicate template ID detected: 'dup-tmpl'"):
        BridgeConfig.model_validate(raw_data)


def test_duplicate_route_ids_raises():
    raw_data = {
        "apiVersion": "v1alpha1",
        "sinks": [
            {"id": "s1", "type": "webhook", "config": {"url": "https://a.example.com"}},
        ],
        "templates": [
            {"id": "t1", "engine": "jinja2", "format": "markdown", "body": "test"},
        ],
        "routes": [
            {"id": "dup-route", "secret_token": "token1", "template_ref": "t1", "forward_to": ["s1"]},
            {"id": "dup-route", "secret_token": "token2", "template_ref": "t1", "forward_to": ["s1"]},
        ],
    }
    with pytest.raises(ValidationError, match="Duplicate route ID detected: 'dup-route'"):
        BridgeConfig.model_validate(raw_data)


def test_matrix_sink_missing_room_id_raises():
    raw_data = {
        "apiVersion": "v1alpha1",
        "sinks": [
            {
                "id": "matrix-sink",
                "type": "matrix",
                "config": {
                    "access_token": "tok",
                    "homeserver_url": "https://matrix.org",
                },
            }
        ],
        "templates": [{"id": "t1", "engine": "jinja2", "format": "markdown", "body": "test"}],
        "routes": [{"id": "r1", "secret_token": "tok", "template_ref": "t1", "forward_to": ["matrix-sink"]}],
    }
    with pytest.raises(ValidationError, match="missing required field 'room_id'"):
        BridgeConfig.model_validate(raw_data)


def test_matrix_sink_missing_access_token_raises():
    raw_data = {
        "apiVersion": "v1alpha1",
        "sinks": [
            {
                "id": "matrix-sink",
                "type": "matrix",
                "config": {
                    "room_id": "!room:matrix.org",
                    "homeserver_url": "https://matrix.org",
                },
            }
        ],
        "templates": [{"id": "t1", "engine": "jinja2", "format": "markdown", "body": "test"}],
        "routes": [{"id": "r1", "secret_token": "tok", "template_ref": "t1", "forward_to": ["matrix-sink"]}],
    }
    with pytest.raises(ValidationError, match="missing required field 'access_token'"):
        BridgeConfig.model_validate(raw_data)


def test_matrix_sink_invalid_homeserver_url_path_raises():
    raw_data = {
        "apiVersion": "v1alpha1",
        "sinks": [
            {
                "id": "matrix-sink",
                "type": "matrix",
                "config": {
                    "room_id": "!room:matrix.org",
                    "access_token": "tok",
                    "homeserver_url": "https://matrix.org/extra/path",
                },
            }
        ],
        "templates": [{"id": "t1", "engine": "jinja2", "format": "markdown", "body": "test"}],
        "routes": [{"id": "r1", "secret_token": "tok", "template_ref": "t1", "forward_to": ["matrix-sink"]}],
    }
    with pytest.raises(ValidationError, match="Path must be empty"):
        BridgeConfig.model_validate(raw_data)


def test_matrix_sink_homeserver_url_with_query_raises():
    raw_data = {
        "apiVersion": "v1alpha1",
        "sinks": [
            {
                "id": "matrix-sink",
                "type": "matrix",
                "config": {
                    "room_id": "!room:matrix.org",
                    "access_token": "tok",
                    "homeserver_url": "https://matrix.org?token=leak",
                },
            }
        ],
        "templates": [{"id": "t1", "engine": "jinja2", "format": "markdown", "body": "test"}],
        "routes": [{"id": "r1", "secret_token": "tok", "template_ref": "t1", "forward_to": ["matrix-sink"]}],
    }
    with pytest.raises(ValidationError, match="Query parameters and URL fragments are not allowed"):
        BridgeConfig.model_validate(raw_data)


def test_empty_route_secret_token_raises():
    with pytest.raises(ValidationError, match="secret_token cannot be empty"):
        RouteConfig(
            id="r1",
            secret_token="",
            template_ref="t1",
            forward_to=["s1"],
        )


def test_config_models_are_frozen():
    sink = SinkConfig(id="s1", type="webhook", config={"url": "https://example.com"})
    with pytest.raises(ValidationError, match="Instance is frozen"):
        sink.id = "s2"

    route = RouteConfig(id="r1", secret_token="tok", template_ref="t1", forward_to=["s1"])
    with pytest.raises(ValidationError, match="Instance is frozen"):
        route.template_ref = "t2"


def test_forward_to_is_immutable_tuple(monkeypatch):
    monkeypatch.setenv("MATRIX_TOKEN", "real-token-value")
    config = load_config(FIXTURES / "valid_config.yaml")
    route = config.routes[0]
    assert isinstance(route.forward_to, tuple)
    assert not hasattr(route.forward_to, "append")


def test_webhook_sink_lowercase_method_raises():
    raw_data = {
        "apiVersion": "v1alpha1",
        "sinks": [
            {
                "id": "webhook-sink",
                "type": "webhook",
                "config": {
                    "url": "https://hooks.example.com/alerts",
                    "method": "post",  # lowercase intentionally
                },
            }
        ],
        "templates": [{"id": "t1", "engine": "jinja2", "format": "markdown", "body": "test"}],
        "routes": [{"id": "r1", "secret_token": "tok", "template_ref": "t1", "forward_to": ["webhook-sink"]}],
    }
    with pytest.raises(ValidationError, match="invalid HTTP method 'post'"):
        BridgeConfig.model_validate(raw_data)


def test_whitespace_only_id_raises():
    with pytest.raises(ValidationError, match="String should have at least 1 character"):
        SinkConfig(id="   ", type="webhook", config={"url": "https://example.com"})


def test_unsupported_template_engine_raises(tmp_path):
    config_content = """
apiVersion: v1alpha1
sinks:
  - id: s1
    type: webhook
    config:
      url: "https://example.com"
templates:
  - id: t1
    engine: mako
    format: markdown
    body: "test"
routes:
  - id: r1
    secret_token: "secret"
    template_ref: t1
    forward_to: ["s1"]
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content.strip(), encoding="utf-8")
    with pytest.raises(ConfigError, match="validation failed"):
        load_config(config_file)


def test_invalid_api_version_raises(tmp_path):
    raw_data = {
        "apiVersion": "v2",
        "sinks": [
            {"id": "s1", "type": "webhook", "config": {"url": "https://example.com"}},
        ],
        "templates": [
            {"id": "t1", "engine": "jinja2", "format": "markdown", "body": "test"},
        ],
        "routes": [
            {"id": "r1", "secret_token": "token1", "template_ref": "t1", "forward_to": ["s1"]},
        ],
    }
    with pytest.raises(ValidationError):
        BridgeConfig.model_validate(raw_data)

    config_content = """
apiVersion: v2
sinks:
  - id: s1
    type: webhook
    config:
      url: "https://example.com"
templates:
  - id: t1
    engine: jinja2
    format: markdown
    body: "test"
routes:
  - id: r1
    secret_token: "secret"
    template_ref: t1
    forward_to: ["s1"]
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content.strip(), encoding="utf-8")
    with pytest.raises(ConfigError, match="validation failed"):
        load_config(config_file)


def test_invalid_template_format_raises():
    with pytest.raises(ValidationError):
        TemplateConfig(
            id="t1",
            engine="jinja2",
            format="xml",  # type: ignore[arg-type]
            body="test",
        )


def test_sink_config_dict_strings_are_stripped():
    sink = SinkConfig(
        id="matrix-sink",
        type="matrix",
        config={
            "room_id": "  !room:matrix.org  ",
            "access_token": "  secret_token  ",
            "homeserver_url": "  https://matrix.org  ",
            "headers": {
                "Authorization": "  Bearer token123  ",
                "X-Custom": "  custom-value  ",
            },
        },
    )
    assert sink.config["room_id"] == "!room:matrix.org"
    assert sink.config["access_token"] == "secret_token"
    assert sink.config["homeserver_url"] == "https://matrix.org"
    assert sink.config["headers"]["Authorization"] == "Bearer token123"
    assert sink.config["headers"]["X-Custom"] == "custom-value"