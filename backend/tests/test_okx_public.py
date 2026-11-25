"""OKX 公共接口冒烟测试（需要网络）。

DNS 被污染或网络不可达时自动跳过，不阻塞本地开发。
"""

from __future__ import annotations

import pytest

from app.exchange.okx_client import OkxAPIError


async def test_server_time_reachable(okx_client) -> None:
    try:
        server_ms = await okx_client.get_server_time()
    except OkxAPIError as exc:
        pytest.skip(f"OKX 不可达，跳过公共接口测试：{exc}")
    assert server_ms > 1_600_000_000_000


async def test_btc_usdt_swap_instrument_exists(okx_client) -> None:
    try:
        instruments = await okx_client.get_instruments("SWAP", "BTC-USDT-SWAP")
    except OkxAPIError as exc:
        pytest.skip(f"OKX 不可达，跳过公共接口测试：{exc}")
    assert len(instruments) == 1
    instrument = instruments[0]
    assert instrument["instId"] == "BTC-USDT-SWAP"
    assert instrument["ctType"] == "linear"
    for field in ("ctVal", "lotSz", "minSz", "tickSz"):
        assert field in instrument, f"合约缺少关键字段 {field}"


async def test_ticker_and_candles(okx_client) -> None:
    try:
        ticker = await okx_client.get_ticker("BTC-USDT-SWAP")
        candles = await okx_client.get_candles("BTC-USDT-SWAP", bar="1H", limit=5)
    except OkxAPIError as exc:
        pytest.skip(f"OKX 不可达，跳过公共接口测试：{exc}")
    assert ticker.get("instId") == "BTC-USDT-SWAP"
    assert float(ticker["last"]) > 0
    assert 0 < len(candles) <= 5
