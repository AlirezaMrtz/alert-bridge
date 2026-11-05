from __future__ import annotations

import asyncio
import logging
from time import perf_counter
from collections.abc import Sequence
from typing import Any

import httpx
from pydantic import BaseModel

from app.config import SinkConfig
from app.metrics import DeliveryMetrics
from app.sinks.matrix import MatrixSink
from app.sinks.webhook import WebhookSink

logger = logging.getLogger("bridge.dispatcher")
DISPATCH_TIMEOUT_SECONDS = 35.0

SINK_REGISTRY = {
    "matrix": MatrixSink,
    "webhook": WebhookSink,
}


class SinkResult(BaseModel):
    sink_id: str
    success: bool
    error: str | None = None


class SinkDispatcher:
    """Dispatches rendered alerts to their configured destination sinks."""

    def __init__(self, sinks: Sequence[SinkConfig], client: httpx.AsyncClient, max_concurrency: int = 50,
                 *, metrics: DeliveryMetrics | None = None) -> None:
        self.sinks = {s.id: s for s in sinks}
        self.client = client
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self.metrics = metrics if metrics is not None else DeliveryMetrics(self.sinks)

    async def dispatch(self, sink_id: str, message: str, *, fmt: str = "text") -> tuple[bool, str | None]:
        """Dispatches a message to a specific sink by instantiating its concrete handler."""
        sink_config = self.sinks.get(sink_id)
        if not sink_config:
            err_msg = f"Sink '{sink_id}' not found in configuration"
            logger.error(err_msg)
            return (False, err_msg)

        sink_class = SINK_REGISTRY.get(sink_config.type)
        if not sink_class:
            err_msg = f"Unsupported sink type '{sink_config.type}' for sink '{sink_id}'"
            logger.error(err_msg)
            return (False, err_msg)

        logger.info("Dispatching alert to sink '%s' (type: %s, message_len=%d)", sink_id, sink_config.type, len(message))
        sink_instance = sink_class(sink_config.id, sink_config.config, self.client)
        sink_instance.metrics = self.metrics
        async with self._semaphore:
            self.metrics.active += 1
            try:
                return await sink_instance.send(message, fmt=fmt)
            finally:
                self.metrics.active -= 1

    async def dispatch_many(self, sink_ids: Sequence[str], message: str, *, fmt: str = "text") -> list[SinkResult]:
        """Dispatches a message concurrently to multiple sinks using asyncio.gather."""
        async def _dispatch_one(sink_id: str) -> SinkResult:
            started = perf_counter()
            outcome = "cancelled"
            try:
                success, error = await asyncio.wait_for(
                    self.dispatch(sink_id, message, fmt=fmt),
                    timeout=DISPATCH_TIMEOUT_SECONDS,
                )
                outcome = "success" if success else "failure"
                return SinkResult(sink_id=sink_id, success=success, error=error)
            except TimeoutError:
                outcome = "timeout"
                logger.error("Dispatch timed out for sink '%s'", sink_id)
                return SinkResult(sink_id=sink_id, success=False, error="Dispatch timed out")
            except Exception as exc:
                outcome = "failure"
                # Exceptions may contain destination URLs or other sensitive data.
                error_type = type(exc).__name__
                logger.error("Unhandled exception dispatching to sink '%s' (%s)", sink_id, error_type)
                return SinkResult(sink_id=sink_id, success=False, error=f"Unhandled sink error ({error_type})")
            finally:
                self.metrics.record_delivery(sink_id, outcome, perf_counter() - started)

        results = await asyncio.gather(*[_dispatch_one(s_id) for s_id in sink_ids])
        return list(results)
