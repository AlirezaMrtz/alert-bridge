from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.sinks.base import BaseSink

logger = logging.getLogger("bridge.sinks.webhook")

MAX_ATTEMPTS = 3


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

        for attempt in range(MAX_ATTEMPTS):
            try:
                response = await self.client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=payload,
                    timeout=10.0,
                )
                response.raise_for_status()
                return (True, None)
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                # Fail fast on 4xx client errors (except 429 Too Many Requests) without retrying
                if 400 <= status_code < 500 and status_code != 429:
                    err_msg = f"Webhook sink '{self.sink_id}' client error (HTTP {status_code}) sending message to URL '{url}': {exc}"
                    logger.error(err_msg)
                    return (False, err_msg)

                err_msg = f"Webhook sink '{self.sink_id}' HTTP {status_code} error sending message to URL '{url}': {exc}"
                if attempt < MAX_ATTEMPTS - 1:
                    retry_after = exc.response.headers.get("Retry-After")
                    try:
                        sleep_time = max(0.0, min(float(retry_after), 2.0)) if retry_after else 0.05 * (2 ** attempt)
                    except (ValueError, TypeError):
                        sleep_time = 0.05 * (2 ** attempt)
                    logger.warning(
                        "Retrying Webhook sink '%s' (attempt %d/%d) after HTTP %d error: %s (sleeping %.2fs)",
                        self.sink_id, attempt + 1, MAX_ATTEMPTS, status_code, exc, sleep_time
                    )
                    await asyncio.sleep(sleep_time)
                else:
                    logger.error(err_msg)
                    return (False, err_msg)
            except (httpx.TimeoutException, httpx.HTTPError) as exc:
                err_msg = f"Webhook sink '{self.sink_id}' network/timeout error sending message to URL '{url}': {exc}"
                if attempt < MAX_ATTEMPTS - 1:
                    sleep_time = 0.05 * (2 ** attempt)
                    logger.warning(
                        "Retrying Webhook sink '%s' (attempt %d/%d) after network error: %s (sleeping %.2fs)",
                        self.sink_id, attempt + 1, MAX_ATTEMPTS, exc, sleep_time
                    )
                    await asyncio.sleep(sleep_time)
                else:
                    logger.error(err_msg)
                    return (False, err_msg)
