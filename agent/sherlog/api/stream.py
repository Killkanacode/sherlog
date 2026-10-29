"""Server-sent events fan-out to the web UI."""

import asyncio
import json
from collections.abc import AsyncIterator

KEEPALIVE_S = 15


class Broadcaster:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def publish(self, type_: str, payload: dict) -> None:
        message = {"type": type_, **payload}
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass  # slow client; it will catch up from the REST API

    async def stream(self) -> AsyncIterator[str]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(queue)
        try:
            yield ": connected\n\n"
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_S)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"event: {message['type']}\ndata: {json.dumps(message, default=str)}\n\n"
        finally:
            self._subscribers.discard(queue)


broadcaster = Broadcaster()
