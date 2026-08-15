from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote, urlencode

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
        """Safely encodes strings or dictionaries for target URL query strings using RFC 3986 percent-encoding."""
        if isinstance(value, dict):
            return urlencode(value, quote_via=quote)
        return quote(str(value))

    async def render(
        self, template_body: str, payload: dict[str, Any], query_params: dict[str, str]
    ) -> tuple[bool, str]:
        """Compiles a template string against a wrapped source context safely and asynchronously.

        Returns (True, rendered_message) on success, or (False, error_message) on failure.
        """
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
            return (True, rendered.strip())
        except TimeoutError:
            err_msg = "Template rendering timed out (>0.5s)"
            logger.error(err_msg)
            return (False, err_msg)
        except Exception as exc:
            err_msg = f"Template rendering failed: {exc}"
            logger.error(err_msg)
            return (False, err_msg)
