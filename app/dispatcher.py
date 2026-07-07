from __future__ import annotations

import logging
from app.config import SinkConfig


logger = logging.getLogger("bridge.dispatcher")


class SinkDispatcher:
    """Dispatches rendered alerts to their configured destination sinks."""

    def __init__(self, sinks: list[SinkConfig]) -> None:
        self.sinks = {s.id: s for s in sinks}

    async def dispatch(self, sink_id: str, message: str) -> bool:
        """Dispatches a message to a specific sink."""
        sink = self.sinks.get(sink_id)
        if not sink:
            logger.error("Sink not found in configuration: %s", sink_id)
            return False

        logger.info("Dispatching alert to sink '%s' (type: %s): %r", sink_id, sink.type, message)
        
        # In Step 3, we mock/stub the dispatching.
        # We will implement real HTTP calls/integrations in Step 4.
        return True
