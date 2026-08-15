from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any
from urllib.parse import quote

import httpx

from app.sinks.base import BaseSink

logger = logging.getLogger("bridge.sinks.matrix")

MAX_ATTEMPTS = 3


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
        # Generate txn_id ONCE so retries use the identical idempotency token
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

        for attempt in range(MAX_ATTEMPTS):
            try:
                response = await self.client.put(url, headers=headers, json=json_data, timeout=10.0)
                response.raise_for_status()
                return (True, None)
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                # Fail fast on 4xx client errors (except 429 Too Many Requests) without retrying
                if 400 <= status_code < 500 and status_code != 429:
                    err_msg = f"Matrix sink '{self.sink_id}' client error (HTTP {status_code}) sending message to room '{room_id}': {exc}"
                    logger.error(err_msg)
                    return (False, err_msg)

                err_msg = f"Matrix sink '{self.sink_id}' HTTP {status_code} error sending message to room '{room_id}': {exc}"
                if attempt < MAX_ATTEMPTS - 1:
                    retry_after = exc.response.headers.get("Retry-After")
                    try:
                        sleep_time = max(0.0, min(float(retry_after), 2.0)) if retry_after else 0.05 * (2 ** attempt)
                    except (ValueError, TypeError):
                        sleep_time = 0.05 * (2 ** attempt)
                    logger.warning(
                        "Retrying Matrix sink '%s' (attempt %d/%d) after HTTP %d error: %s (sleeping %.2fs)",
                        self.sink_id, attempt + 1, MAX_ATTEMPTS, status_code, exc, sleep_time
                    )
                    await asyncio.sleep(sleep_time)
                else:
                    logger.error(err_msg)
                    return (False, err_msg)
            except (httpx.TimeoutException, httpx.HTTPError) as exc:
                err_msg = f"Matrix sink '{self.sink_id}' network/timeout error sending message to room '{room_id}': {exc}"
                if attempt < MAX_ATTEMPTS - 1:
                    sleep_time = 0.05 * (2 ** attempt)
                    logger.warning(
                        "Retrying Matrix sink '%s' (attempt %d/%d) after network error: %s (sleeping %.2fs)",
                        self.sink_id, attempt + 1, MAX_ATTEMPTS, exc, sleep_time
                    )
                    await asyncio.sleep(sleep_time)
                else:
                    logger.error(err_msg)
                    return (False, err_msg)
