from __future__ import annotations

import logging
import uuid
from typing import Any
from urllib.parse import quote

import httpx

from app.sinks.base import BaseSink

logger = logging.getLogger("bridge.sinks.matrix")


class MatrixSink(BaseSink):
    """Concrete sink implementation for dispatching messages to Matrix rooms."""

    async def send(self, message: str) -> tuple[bool, str | None]:
        room_id = self.config.get("room_id")
        access_token = self.config.get("access_token")

        if not room_id or not access_token:
            err_msg = f"Matrix sink '{self.sink_id}' missing required configuration (room_id or access_token)"
            logger.error(err_msg)
            return (False, err_msg)

        # homeserver_url must be declared explicitly in the sink config.
        homeserver = self.config.get("homeserver_url")
        if not homeserver:
            err_msg = (
                f"Matrix sink '{self.sink_id}' is missing required field 'homeserver_url'. "
                "Refusing to guess from the room ID. Add 'homeserver_url' to the sink config."
            )
            logger.error(err_msg)
            return (False, err_msg)

        encoded_room_id = quote(str(room_id), safe="!:")
        txn_id = uuid.uuid4().hex
        url = f"{homeserver.rstrip('/')}/_matrix/client/v3/rooms/{encoded_room_id}/send/m.room.message/{txn_id}"

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        json_data = {
            "msgtype": "m.text",
            "body": message,
        }

        try:
            response = await self.client.put(url, headers=headers, json=json_data, timeout=10.0)
            response.raise_for_status()
            return (True, None)
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            err_msg = f"Matrix sink '{self.sink_id}' failed to send message to room '{room_id}': {exc}"
            logger.error(err_msg)
            return (False, err_msg)
