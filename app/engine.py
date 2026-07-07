from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote_plus, urlencode

from jinja2 import ChainableUndefined, Environment

logger = logging.getLogger("bridge.engine")


class TemplateEngine:
    """Wrapper around Jinja2 configured for highly defensive payload parsing."""

    def __init__(self) -> None:
        # ChainableUndefined allows payload.missing.deep.key to evaluate to an
        # empty string seamlessly rather than raising an immediate AttributeError.
        self._env = Environment(
            undefined=ChainableUndefined,
            autoescape=False,  # We explicitly construct chat Markdown/HTML
        )
        # Register a native URL encoding filter for dynamic dashboard linkages
        self._env.filters["urlencode"] = self._urlencode_filter

    @staticmethod
    def _urlencode_filter(value: Any) -> str:
        """Safely encodes strings or dictionaries for target URL query strings."""
        if isinstance(value, dict):
            return urlencode(value)
        return quote_plus(str(value))

    def render(self, template_body: str, payload: dict[str, Any], query_params: dict[str, str]) -> str:
        """Compiles a template string against a wrapped source context."""
        try:
            template = self._env.from_string(template_body)
            
            # Enforce a predictable, uniform root context namespace
            context = {
                "payload": payload,
                "params": query_params,
            }
            
            return template.render(context).strip()
        except Exception as exc:
            logger.error("Failed to render alert template: %s", exc)
            # Fallback output ensuring the raw data is visible if structural rendering fails completely
            return f"⚠️ [Template Error] Raw Payload: {payload}"
