from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator, model_validator

logger = logging.getLogger("bridge.config")

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


class ConfigError(Exception):
    """Raised for anything wrong with config.yaml: missing file, bad YAML,
    unresolved env vars, or a schema mismatch."""


class SinkConfig(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    id: str = Field(..., min_length=1)
    type: Literal["matrix", "webhook"]
    config: dict[str, Any] = Field(default_factory=dict)


class TemplateConfig(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    id: str = Field(..., min_length=1)
    engine: Literal["jinja2"]
    format: str = Field(..., min_length=1)
    body: str = Field(..., min_length=1)


class RouteConfig(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    id: str = Field(..., min_length=1)
    secret_token: SecretStr
    template_ref: str = Field(..., min_length=1)
    forward_to: tuple[str, ...] = Field(..., min_length=1)

    @field_validator("secret_token")
    @classmethod
    def validate_secret_token_non_empty(cls, v: SecretStr) -> SecretStr:
        if not v.get_secret_value().strip():
            raise ValueError("secret_token cannot be empty or whitespace")
        return v


class BridgeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    api_version: str = Field(alias="apiVersion")
    sinks: list[SinkConfig]
    templates: list[TemplateConfig]
    routes: list[RouteConfig]

    @model_validator(mode="after")
    def validate_unique_ids(self) -> BridgeConfig:
        for entity_name, entity_list in [("sink", self.sinks), ("template", self.templates), ("route", self.routes)]:
            seen: set[str] = set()
            for item in entity_list:
                if item.id in seen:
                    raise ValueError(f"Duplicate {entity_name} ID detected: '{item.id}'")
                seen.add(item.id)
        return self

    @model_validator(mode="after")
    def validate_template_refs(self) -> BridgeConfig:
        template_ids = {t.id for t in self.templates}
        for route in self.routes:
            if route.template_ref not in template_ids:
                raise ValueError(f"Route '{route.id}' references nonexistent template '{route.template_ref}'")
        return self

    @model_validator(mode="after")
    def validate_route_sink_refs(self) -> BridgeConfig:
        sink_ids = {s.id for s in self.sinks}
        for route in self.routes:
            for sink_id in route.forward_to:
                if sink_id not in sink_ids:
                    raise ValueError(f"Route '{route.id}' references nonexistent sink '{sink_id}'")
        return self

    @model_validator(mode="after")
    def validate_matrix_sinks(self) -> BridgeConfig:
        """Every matrix sink must declare valid room_id, access_token, and a base homeserver_url."""
        for sink in self.sinks:
            if sink.type == "matrix":
                room_id = sink.config.get("room_id")
                if not isinstance(room_id, str) or not room_id.strip():
                    raise ValueError(
                        f"Sink '{sink.id}' (type: matrix) is missing required field 'room_id'"
                    )

                access_token = sink.config.get("access_token")
                if not isinstance(access_token, str) or not access_token.strip():
                    raise ValueError(
                        f"Sink '{sink.id}' (type: matrix) is missing required field 'access_token'"
                    )

                homeserver = sink.config.get("homeserver_url")
                if not isinstance(homeserver, str) or not homeserver.strip():
                    raise ValueError(
                        f"Sink '{sink.id}' (type: matrix) is missing required field 'homeserver_url'. "
                        f"Matrix sinks must declare the homeserver URL explicitly (e.g. 'https://matrix.example.com')."
                    )

                parsed = urlparse(homeserver)
                if parsed.scheme not in ("http", "https") or not parsed.netloc:
                    raise ValueError(
                        f"Sink '{sink.id}' (type: matrix) has invalid 'homeserver_url' '{homeserver}'. "
                        f"Must include valid scheme (http/https) and host."
                    )
                if parsed.path not in ("", "/"):
                    raise ValueError(
                        f"Sink '{sink.id}' (type: matrix) has invalid 'homeserver_url' '{homeserver}'. "
                        f"Path must be empty (e.g. 'https://matrix.example.com'), got path '{parsed.path}'."
                    )
                if parsed.query or parsed.fragment:
                    raise ValueError(
                        f"Sink '{sink.id}' (type: matrix) has invalid 'homeserver_url' '{homeserver}'. "
                        f"Query parameters and URL fragments are not allowed in homeserver_url."
                    )
        return self

    @model_validator(mode="after")
    def validate_webhook_sinks(self) -> BridgeConfig:
        """Every webhook sink must contain a valid URL (http:// or https://)
        and an accepted uppercase HTTP method if specified.
        """
        allowed_methods = {"GET", "POST", "PUT", "PATCH", "DELETE"}
        for sink in self.sinks:
            if sink.type == "webhook":
                url = sink.config.get("url")
                if not isinstance(url, str) or not url.strip():
                    raise ValueError(
                        f"Sink '{sink.id}' (type: webhook) is missing a valid 'url' starting with 'http://' or 'https://'"
                    )
                parsed = urlparse(url)
                if parsed.scheme not in ("http", "https") or not parsed.netloc:
                    raise ValueError(
                        f"Sink '{sink.id}' (type: webhook) has invalid 'url' '{url}'. "
                        f"Must include valid scheme (http/https) and host."
                    )
                method = sink.config.get("method")
                if method is not None:
                    if not isinstance(method, str) or method not in allowed_methods:
                        raise ValueError(
                            f"Sink '{sink.id}' (type: webhook) has invalid HTTP method '{method}'. "
                            f"Allowed methods: {', '.join(sorted(allowed_methods))}"
                        )
        return self

    def sink_by_id(self, sink_id: str) -> SinkConfig | None:
        return next((s for s in self.sinks if s.id == sink_id), None)

    def template_by_id(self, template_id: str) -> TemplateConfig | None:
        return next((t for t in self.templates if t.id == template_id), None)

    def route_by_id(self, route_id: str) -> RouteConfig | None:
        return next((r for r in self.routes if r.id == route_id), None)


def _resolve_env_placeholders(raw: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        value = os.getenv(name)
        if value is None:
            raise ConfigError(f"environment variable '{name}' is referenced in config but not set")
        return value

    return _ENV_VAR_PATTERN.sub(replace, raw)


def load_config(path: str | Path) -> BridgeConfig:
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")

    raw = config_path.read_text(encoding="utf-8")
    resolved = _resolve_env_placeholders(raw)

    try:
        data = yaml.safe_load(resolved)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {config_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"{config_path} did not parse to a mapping at the top level")

    try:
        config = BridgeConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"config validation failed:\n{exc}") from exc

    # SecretStr masks itself in str()/repr(), so this is safe to log as-is.
    route_summary = ", ".join(f"{r.id}->{r.secret_token}" for r in config.routes)
    sink_summary = ", ".join(s.id for s in config.sinks)
    logger.info(
        "loaded %d routes (%s) and %d sinks (%s)",
        len(config.routes), route_summary, len(config.sinks), sink_summary,
    )

    return config