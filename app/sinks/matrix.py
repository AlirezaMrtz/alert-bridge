from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx

from app.sinks.base import BaseSink

logger = logging.getLogger("bridge.sinks.matrix")


class MatrixSink(BaseSink):
    """Concrete sink implementation for dispatching messages to Matrix rooms."""

    async def send(self, message: str) -> bool:
        room_id = self.config.get("room_id")
        access_token = self.config.get("access_token")

        if not room_id or not access_token:
            logger.error(
                "Matrix sink '%s' missing required configuration (room_id or access_token)",
                self.sink_id,
            )
            return False

        # homeserver_url must be declared explicitly in the sink config.
        homeserver = self.config.get("homeserver_url")
        if not homeserver:
            logger.error(
                "Matrix sink '%s' is missing required field 'homeserver_url'. "
                "Refusing to guess from the room ID. Add 'homeserver_url' to the sink config.",
                self.sink_id,
            )
            return False

        txn_id = uuid.uuid4().hex
        url = f"{homeserver.rstrip('/')}/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn_id}"

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        json_data = {
            "msgtype": "m.text",
            "body": message,
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.put(url, headers=headers, json=json_data, timeout=10.0)
                response.raise_for_status()
                return True
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            logger.error(
                "Matrix sink '%s' failed to send message to room '%s': %s",
                self.sink_id,
                room_id,
                exc,
            )
            return False
