from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote_plus, urlencode

from jinja2 import ChainableUndefined
from jinja2.sandbox import SandboxedEnvironment

logger = logging.getLogger("bridge.engine")


class TemplateEngine:
    """Wrapper around Jinja2 configured for highly defensive payload parsing."""

    def __init__(self) -> None:
        # SandboxedEnvironment prevents arbitrary Python code execution.
        # ChainableUndefined allows payload.missing.deep.key to evaluate to an
        # empty string seamlessly rather than raising an immediate AttributeError.
        self._env = SandboxedEnvironment(
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

    async def render(self, template_body: str, payload: dict[str, Any], query_params: dict[str, str]) -> str:
        """Compiles a template string against a wrapped source context safely and asynchronously."""
        try:
            template = self._env.from_string(template_body)
            context = {
                "payload": payload,
                "params": query_params,
            }
            rendered = await asyncio.wait_for(
                asyncio.to_thread(template.render, context),
                timeout=0.5,
            )
            return rendered.strip()
        except TimeoutError:
            logger.error("Template rendering timed out (>0.5s)")
            return "⚠️ [Template Error] Alert suppressed"
        except Exception as exc:
            logger.error("Template rendering failed: %s", exc)
            return "⚠️ [Template Error] Alert suppressed"
