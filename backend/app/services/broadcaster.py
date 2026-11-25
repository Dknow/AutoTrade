"""前端 WebSocket 客户端管理与广播。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class ClientHub:
    """维护前端连接，串行化每个连接的发送，避免并发 send 报错。"""

    def __init__(self) -> None:
        self._clients: dict[Any, asyncio.Lock] = {}

    @property
    def count(self) -> int:
        return len(self._clients)

    def add(self, websocket: Any) -> None:
        self._clients[websocket] = asyncio.Lock()

    def remove(self, websocket: Any) -> None:
        self._clients.pop(websocket, None)

    async def broadcast(self, message: dict[str, Any]) -> None:
        for websocket, lock in list(self._clients.items()):
            try:
                async with lock:
                    await websocket.send_json(message)
            except Exception:  # noqa: BLE001 - 客户端已断开即可移除
                self.remove(websocket)

    async def send(self, websocket: Any, message: dict[str, Any]) -> None:
        lock = self._clients.get(websocket)
        if lock is None:
            return
        try:
            async with lock:
                await websocket.send_json(message)
        except Exception:  # noqa: BLE001
            self.remove(websocket)
