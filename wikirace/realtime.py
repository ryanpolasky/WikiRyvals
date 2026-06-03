"""Real-time match channel: a tiny WebSocket pub/sub keyed by match id.

Both players in a head-to-head open a WebSocket to their match. The server
pushes three things over it so the lobby never has to poll:

  * ``progress`` , the opponent's live position (current article + click count,
    deliberately *not* the full path, so you can't shadow their route),
  * ``countdown`` , kept in sync if we ever drive it server-side, and
  * ``resolved`` , the final result the instant the match decides, so both
    screens flip to the results card together.

The matchmaker and the ``/visit`` hot path run in FastAPI's sync threadpool, so
they can't ``await`` a coroutine directly. ``publish`` bridges that gap: it hops
the message onto the asyncio loop captured at startup via
``run_coroutine_threadsafe``. If no loop is bound yet (e.g. in unit tests) it is
a safe no-op.
"""

from __future__ import annotations

import asyncio
from typing import Any


class MatchHub:
    def __init__(self) -> None:
        self._rooms: dict[str, set[Any]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the serving event loop so sync code can publish onto it."""
        self._loop = loop

    async def connect(self, match_id: str, ws: Any) -> None:
        async with self._lock:
            self._rooms.setdefault(match_id, set()).add(ws)

    async def disconnect(self, match_id: str, ws: Any) -> None:
        async with self._lock:
            room = self._rooms.get(match_id)
            if room is not None:
                room.discard(ws)
                if not room:
                    self._rooms.pop(match_id, None)

    def room_size(self, match_id: str) -> int:
        room = self._rooms.get(match_id)
        return len(room) if room else 0

    async def broadcast(self, match_id: str, message: dict) -> None:
        """Send ``message`` to every socket in the room, dropping dead ones."""
        room = list(self._rooms.get(match_id, ()))
        if not room:
            return
        dead: list[Any] = []
        for ws in room:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                live = self._rooms.get(match_id)
                if live is not None:
                    for ws in dead:
                        live.discard(ws)
                    if not live:
                        self._rooms.pop(match_id, None)

    def publish(self, match_id: str, message: dict) -> None:
        """Thread-safe fire-and-forget broadcast, callable from sync handlers."""
        loop = self._loop
        if loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self.broadcast(match_id, message), loop)
        except RuntimeError:
            pass  # loop not running (shutdown) , drop the message


hub = MatchHub()
