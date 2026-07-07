from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, SecretStr, ValidationError, model_validator

logger = logging.getLogger("bridge.config")

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


class ConfigError(Exception):
    """Raised for anything wrong with config.yaml: missing file, bad YAML,
    unresolved env vars, or a schema mismatch."""


class SinkConfig(BaseModel):
    id: str
    type: str
    config: dict[str, Any] = Field(default_factory=dict)


class TemplateConfig(BaseModel):
    id: str
    engine: str
    format: str
    body: str


class RouteConfig(BaseModel):
    id: str
    secret_token: SecretStr
    template_ref: str
    forward_to: list[str]


class BridgeConfig(BaseModel):
    api_version: str = Field(alias="apiVersion")
    sinks: list[SinkConfig]
    templates: list[TemplateConfig]
    routes: list[RouteConfig]

    @model_validator(mode="after")
    def validate_template_refs(self) -> BridgeConfig:
        template_ids = {t.id for t in self.templates}
        for route in self.routes:
            if route.template_ref not in template_ids:
                raise ValueError(f"Route '{route.id}' references nonexistent template '{route.template_ref}'")
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