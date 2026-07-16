from __future__ import annotations

import logging
from app.config import SinkConfig
from app.sinks.matrix import MatrixSink

logger = logging.getLogger("bridge.dispatcher")

SINK_REGISTRY = {
    "matrix": MatrixSink,
}


class SinkDispatcher:
    """Dispatches rendered alerts to their configured destination sinks."""

    def __init__(self, sinks: list[SinkConfig]) -> None:
        self.sinks = {s.id: s for s in sinks}

    async def dispatch(self, sink_id: str, message: str) -> bool:
        """Dispatches a message to a specific sink by instantiating its concrete handler."""
        sink_config = self.sinks.get(sink_id)
        if not sink_config:
            logger.error("Sink not found in configuration: %s", sink_id)
            return False

        logger.info("Dispatching alert to sink '%s' (type: %s): %r", sink_id, sink_config.type, message)
        
        sink_class = SINK_REGISTRY.get(sink_config.type)
        if not sink_class:
            logger.error("Unsupported sink type '%s' for sink '%s'", sink_config.type, sink_id)
            return False

        sink_instance = sink_class(sink_config.id, sink_config.config)
        return await sink_instance.send(message)

