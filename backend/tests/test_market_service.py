"""行情服务缓存与消息处理测试。"""

from __future__ import annotations

import asyncio

import pytest

from app.exchange.okx_ws import PUBLIC_WS_URL, OkxWsClient
from app.services.market_service import MAX_CANDLES, MarketService, _ticker_payload


class _FakeHub:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def broadcast(self, message: dict) -> None:
        self.messages.append(message)


def _row(ts: int) -> list[str]:
    return [str(ts), "1", "2", "0.5", "1.5", "10", "10", "15", "1"]


def test_ticker_payload_mapping() -> None:
    payload = _ticker_payload(
        {
            "instId": "BTC-USDT-SWAP",
            "last": "58000.1",
            "askPx": "58000.2",
            "bidPx": "58000.0",
            "open24h": "57000",
            "high24h": "59000",
            "low24h": "56000",
            "vol24h": "100",
            "ts": "1700000000000",
        }
    )
    assert payload["inst_id"] == "BTC-USDT-SWAP"
    assert payload["last"] == "58000.1"
    assert payload["bid"] == "58000.0"
    assert payload["ts"] == "1700000000000"


def test_seed_and_sort_candles() -> None:
    service = MarketService()
    service.seed_candles([_row(3), _row(1), _row(2)])
    rows = service.candles(2)
    assert [row[0] for row in rows] == ["3", "2"]


def test_candles_are_trimmed() -> None:
    service = MarketService()
    service.seed_candles([_row(ts) for ts in range(1, MAX_CANDLES + 120)])
    assert len(service.candles(MAX_CANDLES * 2)) == MAX_CANDLES
    assert service.candles(1)[0][0] == str(MAX_CANDLES + 119)


async def test_on_message_updates_cache_and_broadcasts() -> None:
    service = MarketService()
    hub = _FakeHub()
    service.hub = hub  # type: ignore[assignment]

    await service._on_message(
        {
            "arg": {"channel": "tickers", "instId": "BTC-USDT-SWAP"},
            "data": [{"instId": "BTC-USDT-SWAP", "last": "58000", "ts": "1"}],
        }
    )
    assert hub.messages[-1]["type"] == "ticker"
    assert service.ticker["last"] == "58000"

    await service._on_message({"arg": {"channel": "candle1m"}, "data": [_row(1000)]})
    assert hub.messages[-1]["type"] == "candle"
    assert service.candles(1)[0][0] == "1000"


async def test_public_ws_receives_ticker() -> None:
    client = OkxWsClient(PUBLIC_WS_URL, name="ws-test", ping_interval=60)
    received = asyncio.Event()
    messages: list[dict] = []

    async def handler(message: dict) -> None:
        messages.append(message)
        received.set()

    client.set_handler(handler)
    await client.start()
    await client.subscribe([{"channel": "tickers", "instId": "BTC-USDT-SWAP"}])
    try:
        await asyncio.wait_for(received.wait(), timeout=15)
    except asyncio.TimeoutError:
        pytest.skip("OKX 公共 WebSocket 不可达或超时")
    finally:
        await client.stop()

    assert messages[0]["arg"]["channel"] == "tickers"
    assert messages[0]["data"][0]["instId"] == "BTC-USDT-SWAP"
