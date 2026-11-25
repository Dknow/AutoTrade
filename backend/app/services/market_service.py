"""行情服务：订阅多个 OKX 合约的公共频道，缓存 ticker 与 K 线，并广播给前端。

多标的约定：
- `inst_ids` 首批订阅列表，`inst_id` 属性返回默认标的（首个）；
- `candles()` / `ticker` 面向默认标的，多标的场景用 `candles_for()` / `ticker_for()`；
- 策略运行时切换标的会调用 `add_instrument()` 动态加订阅。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.exchange.okx_ws import BUSINESS_WS_URL, PUBLIC_WS_URL, OkxWsClient
from app.services.broadcaster import ClientHub

logger = logging.getLogger(__name__)

MAX_CANDLES = 500


def _ticker_payload(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "inst_id": raw.get("instId"),
        "last": raw.get("last"),
        "ask": raw.get("askPx"),
        "bid": raw.get("bidPx"),
        "open_24h": raw.get("open24h"),
        "high_24h": raw.get("high24h"),
        "low_24h": raw.get("low24h"),
        "vol_24h": raw.get("vol24h"),
        "ts": raw.get("ts"),
    }


class MarketService:
    def __init__(self, inst_ids: list[str] | None = None, bar: str = "1m") -> None:
        self.inst_ids: list[str] = list(dict.fromkeys(inst_ids or ["BTC-USDT-SWAP"]))
        self.bar = bar
        self._tickers: dict[str, dict[str, Any]] = {}
        self._candles: dict[str, dict[int, list[str]]] = {inst_id: {} for inst_id in self.inst_ids}
        self.hub = ClientHub()
        self._candle_listeners: list[Callable[[str], Awaitable[None]]] = []
        # tickers 在 public 端点；K 线频道已迁移到 business 端点
        self.ws = OkxWsClient(PUBLIC_WS_URL, name="public")
        self.candle_ws = OkxWsClient(BUSINESS_WS_URL, name="business")

    # ------------------------------------------------------------ 标的与兼容
    @property
    def inst_id(self) -> str:
        """默认标的（兼容单标的调用）。"""
        return self.inst_ids[0]

    @property
    def ticker(self) -> dict[str, Any]:
        return self._tickers.get(self.inst_id, {})

    def ticker_for(self, inst_id: str) -> dict[str, Any]:
        return self._tickers.get(inst_id, {})

    def has(self, inst_id: str) -> bool:
        return inst_id in self._candles

    def add_candle_listener(self, listener: Callable[[str], Awaitable[None]]) -> None:
        """注册 K 线更新回调，供策略引擎驱动评估。"""
        self._candle_listeners.append(listener)

    # ------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        self.ws.set_handler(self._on_message)
        self.candle_ws.set_handler(self._on_message)
        await self.ws.start()
        await self.candle_ws.start()
        await self.ws.subscribe([{"channel": "tickers", "instId": inst_id} for inst_id in self.inst_ids])
        await self.candle_ws.subscribe(
            [{"channel": f"candle{self.bar}", "instId": inst_id} for inst_id in self.inst_ids]
        )
        logger.info("行情服务已启动: %s %s", ",".join(self.inst_ids), self.bar)

    async def stop(self) -> None:
        await self.candle_ws.stop()
        await self.ws.stop()

    async def add_instrument(self, inst_id: str) -> bool:
        """动态追加订阅（策略切换标的时使用）；已订阅则返回 False。"""
        if inst_id in self._candles:
            return False
        self.inst_ids.append(inst_id)
        self._candles[inst_id] = {}
        await self.ws.subscribe([{"channel": "tickers", "instId": inst_id}])
        await self.candle_ws.subscribe([{"channel": f"candle{self.bar}", "instId": inst_id}])
        logger.info("行情服务动态订阅: %s", inst_id)
        return True

    # ------------------------------------------------------------ 读取缓存
    def seed_candles(self, rows: list[list[str]], inst_id: str | None = None) -> None:
        target = inst_id or self.inst_id
        bucket = self._candles.setdefault(target, {})
        for row in rows:
            if isinstance(row, list) and row:
                bucket[int(row[0])] = row
        self._trim_candles(target)

    def candles(self, limit: int = 200) -> list[list[str]]:
        return self.candles_for(self.inst_id, limit)

    def candles_for(self, inst_id: str, limit: int = 200) -> list[list[str]]:
        bucket = self._candles.get(inst_id) or {}
        rows = sorted(bucket.values(), key=lambda row: int(row[0]), reverse=True)
        return rows[:limit]

    def snapshot(self) -> dict[str, Any]:
        return {
            "inst_id": self.inst_id,
            "inst_ids": list(self.inst_ids),
            "bar": self.bar,
            "ticker": self.ticker,
            "tickers": dict(self._tickers),
            "candles": self.candles(200),
            "ws_connected": self.ws.connected and self.candle_ws.connected,
        }

    # ------------------------------------------------------------ 消息处理
    async def _on_message(self, message: dict[str, Any]) -> None:
        arg = message.get("arg") or {}
        channel = str(arg.get("channel", ""))
        data = message.get("data") or []
        inst_id = str(arg.get("instId") or self.inst_id)

        if channel == "tickers" and data:
            self._tickers[inst_id] = _ticker_payload(data[0])
            await self.hub.broadcast(
                {"type": "ticker", "inst_id": inst_id, "data": self._tickers[inst_id]}
            )
            return

        if channel.startswith("candle") and data:
            bucket = self._candles.setdefault(inst_id, {})
            for row in data:
                if isinstance(row, list) and row:
                    bucket[int(row[0])] = row
            self._trim_candles(inst_id)
            await self.hub.broadcast(
                {"type": "candle", "inst_id": inst_id, "data": self.candles_for(inst_id, 2)}
            )
            for listener in list(self._candle_listeners):
                try:
                    await listener(inst_id)
                except Exception:  # noqa: BLE001 - 单个监听器异常不影响行情推送
                    logger.exception("K 线监听器执行失败")

    def _trim_candles(self, inst_id: str) -> None:
        bucket = self._candles.get(inst_id) or {}
        if len(bucket) <= MAX_CANDLES:
            return
        keep = sorted(bucket, reverse=True)[:MAX_CANDLES]
        self._candles[inst_id] = {ts: bucket[ts] for ts in keep}
