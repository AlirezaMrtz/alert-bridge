from __future__ import annotations

import asyncio
import logging
from http import HTTPStatus
from typing import Any

import httpx

from app.sinks.base import BaseSink

logger = logging.getLogger("bridge.sinks.webhook")

MAX_ATTEMPTS = 3


class WebhookSink(BaseSink):
    """Concrete sink implementation for dispatching messages to generic HTTP webhooks."""

    async def send(self, message: str, *, fmt: str = "text") -> tuple[bool, str | None]:
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
                try:
                    reason = HTTPStatus(status_code).phrase
                except ValueError:
                    reason = "Unknown status"
                # Fail fast on 4xx client errors (except 429 Too Many Requests) without retrying
                if 400 <= status_code < 500 and status_code != 429:
                    err_msg = f"Webhook sink '{self.sink_id}' client error (HTTP {status_code} {reason})"
                    logger.error(err_msg)
                    return (False, err_msg)

                err_msg = f"Webhook sink '{self.sink_id}' HTTP error (HTTP {status_code} {reason})"
                if attempt < MAX_ATTEMPTS - 1:
                    retry_after = exc.response.headers.get("Retry-After")
                    try:
                        sleep_time = max(0.0, min(float(retry_after), 2.0)) if retry_after else 0.05 * (2 ** attempt)
                    except (ValueError, TypeError):
                        sleep_time = 0.05 * (2 ** attempt)
                    if self.metrics is not None:
                        self.metrics.retry(self.sink_id, "http")
                    logger.warning(
                        "Retrying Webhook sink '%s' (attempt %d/%d) after HTTP %d (sleeping %.2fs)",
                        self.sink_id, attempt + 1, MAX_ATTEMPTS, status_code, sleep_time
                    )
                    await asyncio.sleep(sleep_time)
                else:
                    logger.error(err_msg)
                    return (False, err_msg)
            except httpx.HTTPError as exc:
                # HTTPX exception messages may include credential-bearing URLs.
                cause = "Connection timed out" if isinstance(exc, httpx.TimeoutException) else type(exc).__name__
                err_msg = f"Webhook sink '{self.sink_id}' network/timeout error: {cause}"
                if attempt < MAX_ATTEMPTS - 1:
                    sleep_time = 0.05 * (2 ** attempt)
                    if self.metrics is not None:
                        self.metrics.retry(self.sink_id, "network")
                    logger.warning(
                        "Retrying Webhook sink '%s' (attempt %d/%d) after network error (%s, sleeping %.2fs)",
                        self.sink_id, attempt + 1, MAX_ATTEMPTS, type(exc).__name__, sleep_time
                    )
                    await asyncio.sleep(sleep_time)
                else:
                    logger.error(err_msg)
                    return (False, err_msg)
