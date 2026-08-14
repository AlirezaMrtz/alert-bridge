from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import httpx


class BaseSink(ABC):
    """Abstract base class representing a destination sink for alert delivery."""

    def __init__(self, sink_id: str, config: dict[str, Any], client: httpx.AsyncClient) -> None:
        self.sink_id = sink_id
        self.config = config
        self.client = client

    @abstractmethod
    async def send(self, message: str) -> tuple[bool, str | None]:
        """Sends a rendered text message to the configured sink.

        Must return (True, None) on success, (False, <reason>) on failure, and catch all exceptions.
        """
        pass
