from __future__ import annotations

import logging
from typing import Any

import httpx

from app.sinks.base import BaseSink

logger = logging.getLogger("bridge.sinks.webhook")


class WebhookSink(BaseSink):
    """Concrete sink implementation for dispatching messages to generic HTTP webhooks."""

    async def send(self, message: str) -> tuple[bool, str | None]:
        url = self.config.get("url")
        if not url:
            err_msg = f"Webhook sink '{self.sink_id}' missing required configuration 'url'"
            logger.error(err_msg)
            return (False, err_msg)

        method = str(self.config.get("method", "POST")).upper()
        headers = self.config.get("headers") or {}
        text_field = str(self.config.get("text_field", "text"))
        extra_fields = self.config.get("extra_fields") or {}

        # Construct JSON body: shallow merge extra_fields with text_field
        payload: dict[str, Any] = dict(extra_fields)
        payload[text_field] = message

        try:
            async with httpx.AsyncClient() as client:
                response = await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=payload,
                    timeout=10.0,
                )
                response.raise_for_status()
                return (True, None)
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            err_msg = f"Webhook sink '{self.sink_id}' failed to send message to URL '{url}': {exc}"
            logger.error(err_msg)
            return (False, err_msg)
