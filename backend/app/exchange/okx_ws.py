"""OKX v5 WebSocket 客户端：订阅、心跳、自动重连、私有频道登录。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

import websockets

from app.exchange.okx_sign import sign

logger = logging.getLogger(__name__)

PUBLIC_WS_URL = "wss://ws.okx.com/ws/v5/public"
BUSINESS_WS_URL = "wss://ws.okx.com/ws/v5/business"
PRIVATE_WS_URL = "wss://ws.okx.com/ws/v5/private"
PRIVATE_SIMULATED_WS_URL = "wss://wspap.okx.com/ws/v5/private"

MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


class OkxWsError(RuntimeError):
    pass


class OkxWsClient:
    """一个可自动重连的 OKX WebSocket 连接。

    公共频道直接订阅；私有频道需要 login=true，登录成功后才补发订阅。
    """

    def __init__(
        self,
        url: str,
        *,
        api_key: str = "",
        secret_key: str = "",
        passphrase: str = "",
        login: bool = False,
        ping_interval: float = 20.0,
        max_backoff: float = 30.0,
        name: str = "ws",
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.login = login
        self.ping_interval = ping_interval
        self.max_backoff = max_backoff
        self.name = name

        self._subscriptions: dict[str, dict[str, Any]] = {}
        self._handler: MessageHandler | None = None
        self._ws: Any = None
        self._task: asyncio.Task[None] | None = None
        self._connected = False
        self._logged_in = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def logged_in(self) -> bool:
        return self._logged_in

    def set_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_forever(), name=f"okx-{self.name}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._close_socket()

    async def subscribe(self, args: list[dict[str, Any]]) -> None:
        fresh: list[dict[str, Any]] = []
        for arg in args:
            key = f"{arg.get('channel')}:{arg.get('instId', arg.get('instType', ''))}"
            if key in self._subscriptions:
                continue
            self._subscriptions[key] = arg
            fresh.append(arg)
        if fresh and self._logged_in:
            await self._send({"op": "subscribe", "args": fresh})

    # ------------------------------------------------------------------ 内部
    async def _run_forever(self) -> None:
        backoff = 1.0
        while True:
            try:
                await self._connect_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 重连需要兜住所有网络异常
                logger.warning("[%s] 连接异常: %s，%.0fs 后重连", self.name, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)

    async def _connect_once(self) -> None:
        async with websockets.connect(
            self.url,
            ping_interval=None,
            open_timeout=10,
            close_timeout=5,
            max_queue=2048,
        ) as ws:
            self._ws = ws
            self._connected = True
            logger.info("[%s] 已连接 %s", self.name, self.url)
            try:
                if self.login:
                    await self._do_login(ws)
                self._logged_in = True
                if self._subscriptions:
                    await self._send({"op": "subscribe", "args": list(self._subscriptions.values())})
                heartbeat = asyncio.create_task(self._heartbeat(ws), name=f"okx-{self.name}-ping")
                try:
                    async for raw in ws:
                        await self._on_raw_message(raw)
                finally:
                    heartbeat.cancel()
                    try:
                        await heartbeat
                    except asyncio.CancelledError:
                        pass
            finally:
                self._connected = False
                self._logged_in = False
                self._ws = None
                logger.info("[%s] 连接已断开", self.name)

    async def _do_login(self, ws: Any) -> None:
        timestamp = str(int(time.time()))
        payload = {
            "op": "login",
            "args": [
                {
                    "apiKey": self.api_key,
                    "passphrase": self.passphrase,
                    "timestamp": timestamp,
                    "sign": sign(self.secret_key, timestamp, "GET", "/users/self/verify"),
                }
            ],
        }
        await ws.send(json.dumps(payload))
        deadline = asyncio.get_running_loop().time() + 10
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise OkxWsError("私有频道登录超时")
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            message = json.loads(raw)
            if message.get("event") == "login":
                if str(message.get("code")) != "0":
                    raise OkxWsError(f"私有频道登录失败: {message.get('msg')}")
                logger.info("[%s] 私有频道登录成功", self.name)
                return

    async def _heartbeat(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(self.ping_interval)
            try:
                await ws.send("ping")
            except Exception:  # noqa: BLE001 - 连接已断开时由主循环处理
                return

    async def _on_raw_message(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        if raw == "pong":
            return
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("[%s] 无法解析消息: %s", self.name, raw[:120])
            return
        event = message.get("event")
        if event == "error":
            logger.error("[%s] 服务端错误: %s", self.name, message)
            return
        if event and event != "subscribe":
            return
        if self._handler and message.get("data"):
            await self._handler(message)

    async def _send(self, payload: dict[str, Any]) -> None:
        if not self._ws:
            return
        await self._ws.send(json.dumps(payload))

    async def _close_socket(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.close()
        except Exception:  # noqa: BLE001 - 关闭失败无需处理
            pass
