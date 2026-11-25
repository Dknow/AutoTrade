"""行情服务：订阅 OKX 公共频道，缓存最新 ticker 与 K 线，并广播给前端。"""

from __future__ import annotations

import logging
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
    def __init__(self, inst_id: str = "BTC-USDT-SWAP", bar: str = "1m") -> None:
        self.inst_id = inst_id
        self.bar = bar
        self.ticker: dict[str, Any] = {}
        self._candles: dict[int, list[str]] = {}
        self.hub = ClientHub()
        # tickers 在 public 端点；K 线频道已迁移到 business 端点
        self.ws = OkxWsClient(PUBLIC_WS_URL, name="public")
        self.candle_ws = OkxWsClient(BUSINESS_WS_URL, name="business")

    async def start(self) -> None:
        self.ws.set_handler(self._on_message)
        self.candle_ws.set_handler(self._on_message)
        await self.ws.start()
        await self.candle_ws.start()
        await self.ws.subscribe([{"channel": "tickers", "instId": self.inst_id}])
        await self.candle_ws.subscribe([{"channel": f"candle{self.bar}", "instId": self.inst_id}])
        logger.info("行情服务已启动: %s %s", self.inst_id, self.bar)

    async def stop(self) -> None:
        await self.candle_ws.stop()
        await self.ws.stop()

    # ------------------------------------------------------------ 读取缓存
    def seed_candles(self, rows: list[list[str]]) -> None:
        for row in rows:
            if isinstance(row, list) and row:
                self._candles[int(row[0])] = row
        self._trim_candles()

    def candles(self, limit: int = 200) -> list[list[str]]:
        rows = sorted(self._candles.values(), key=lambda row: int(row[0]), reverse=True)
        return rows[:limit]

    def snapshot(self) -> dict[str, Any]:
        return {
            "inst_id": self.inst_id,
            "bar": self.bar,
            "ticker": self.ticker,
            "candles": self.candles(200),
            "ws_connected": self.ws.connected and self.candle_ws.connected,
        }

    # ------------------------------------------------------------ 消息处理
    async def _on_message(self, message: dict[str, Any]) -> None:
        arg = message.get("arg") or {}
        channel = str(arg.get("channel", ""))
        data = message.get("data") or []

        if channel == "tickers" and data:
            self.ticker = _ticker_payload(data[0])
            await self.hub.broadcast({"type": "ticker", "data": self.ticker})
            return

        if channel.startswith("candle") and data:
            for row in data:
                if isinstance(row, list) and row:
                    self._candles[int(row[0])] = row
            self._trim_candles()
            await self.hub.broadcast({"type": "candle", "data": self.candles(2)})

    def _trim_candles(self) -> None:
        if len(self._candles) <= MAX_CANDLES:
            return
        keep = sorted(self._candles, reverse=True)[:MAX_CANDLES]
        self._candles = {ts: self._candles[ts] for ts in keep}
