"""交易服务单测：用 MockTransport 校验真实发出的下单参数（不联网、不下真单）。

重点覆盖双向持仓的三种组合，防止参数写错变成反向开仓：
- 开多 buy + long
- 开空 sell + short
- 平仓 反向 side + 原 posSide + reduceOnly
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from app.exchange.okx_client import OkxClient
from app.services.instrument_service import InstrumentNotFound, InstrumentService
from app.services.trade_service import TradeService, TradeValidationError

INSTRUMENT = {
    "instId": "BTC-USDT-SWAP",
    "ctVal": "0.01",
    "ctValCcy": "BTC",
    "lotSz": "0.01",
    "minSz": "0.01",
    "tickSz": "0.1",
    "lever": "100",
}


class FakeAccountService:
    def __init__(self, positions: list[dict], ready: bool = True) -> None:
        self._positions = positions
        self.ready = ready

    def active_positions(self) -> list[dict]:
        return self._positions


def _transport(record: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        record.append(request)
        path = request.url.path
        if path == "/api/v5/public/time":
            return httpx.Response(200, json={"code": "0", "data": [{"ts": "1795603850807"}]})
        if path == "/api/v5/public/instruments":
            return httpx.Response(200, json={"code": "0", "data": [INSTRUMENT]})
        if path == "/api/v5/trade/close-position":
            return httpx.Response(200, json={"code": "0", "data": [{"instId": "BTC-USDT-SWAP"}]})
        return httpx.Response(200, json={"code": "0", "data": [{"ordId": "1001"}]})

    return httpx.MockTransport(handler)


async def _build(record: list[httpx.Request], *, positions: list[dict] | None = None, ready: bool = True):
    client = OkxClient(
        api_key="k",
        secret_key="s",
        passphrase="p",
        simulated=True,
        transport=_transport(record),
    )
    instruments = InstrumentService(client)
    await instruments.load()
    service = TradeService(client, instruments, FakeAccountService(positions or [], ready=ready))
    return client, service


def _last_json(record: list[httpx.Request]) -> dict:
    return json.loads(record[-1].content.decode())


async def test_open_long_uses_buy_and_long() -> None:
    record: list[httpx.Request] = []
    client, service = await _build(record)
    async with client:
        await service.open_position(
            inst_id="BTC-USDT-SWAP", pos_side="long", size=1, price=60000
        )
    body = _last_json(record)
    assert record[-1].url.path == "/api/v5/trade/order"
    assert body["side"] == "buy"
    assert body["posSide"] == "long"
    assert "reduceOnly" not in body
    assert body["px"] == "60000"


async def test_open_short_uses_sell_and_short() -> None:
    record: list[httpx.Request] = []
    client, service = await _build(record)
    async with client:
        await service.open_position(
            inst_id="BTC-USDT-SWAP", pos_side="short", size=1, price=60000
        )
    body = _last_json(record)
    assert body["side"] == "sell"
    assert body["posSide"] == "short"


async def test_partial_close_long_uses_sell_reduce_only() -> None:
    record: list[httpx.Request] = []
    client, service = await _build(
        record, positions=[{"instId": "BTC-USDT-SWAP", "posSide": "long", "pos": "5"}]
    )
    async with client:
        await service.close_position(inst_id="BTC-USDT-SWAP", pos_side="long", size=2)
    body = _last_json(record)
    assert body["side"] == "sell"
    assert body["posSide"] == "long"
    assert body["reduceOnly"] is True
    assert body["sz"] == "2"


async def test_partial_close_short_uses_buy_reduce_only() -> None:
    record: list[httpx.Request] = []
    client, service = await _build(
        record, positions=[{"instId": "BTC-USDT-SWAP", "posSide": "short", "pos": "5"}]
    )
    async with client:
        await service.close_position(inst_id="BTC-USDT-SWAP", pos_side="short", size=2)
    body = _last_json(record)
    assert body["side"] == "buy"
    assert body["posSide"] == "short"
    assert body["reduceOnly"] is True


async def test_full_close_calls_close_position_endpoint() -> None:
    record: list[httpx.Request] = []
    client, service = await _build(
        record, positions=[{"instId": "BTC-USDT-SWAP", "posSide": "long", "pos": "5"}]
    )
    async with client:
        result = await service.close_position(inst_id="BTC-USDT-SWAP", pos_side="long")
    assert record[-1].url.path == "/api/v5/trade/close-position"
    body = _last_json(record)
    assert body["posSide"] == "long"
    assert body["autoCxl"] is True
    assert result["mode"] == "full"


async def test_size_aligned_to_lot_step() -> None:
    record: list[httpx.Request] = []
    client, service = await _build(record)
    async with client:
        await service.open_position(
            inst_id="BTC-USDT-SWAP", pos_side="long", size=Decimal("1.239"), price=60000
        )
    assert _last_json(record)["sz"] == "1.23"


async def test_price_aligned_to_tick() -> None:
    record: list[httpx.Request] = []
    client, service = await _build(record)
    async with client:
        await service.open_position(
            inst_id="BTC-USDT-SWAP", pos_side="long", size=1, price=Decimal("60000.37")
        )
    assert _last_json(record)["px"] == "60000.4"


async def test_reject_size_below_min() -> None:
    record: list[httpx.Request] = []
    client, service = await _build(record)
    async with client:
        with pytest.raises(TradeValidationError):
            await service.open_position(inst_id="BTC-USDT-SWAP", pos_side="long", size="0.001", price=60000)


async def test_reject_invalid_pos_side() -> None:
    record: list[httpx.Request] = []
    client, service = await _build(record)
    async with client:
        with pytest.raises(TradeValidationError):
            await service.open_position(inst_id="BTC-USDT-SWAP", pos_side="net", size=1, price=60000)


async def test_reject_close_without_position() -> None:
    record: list[httpx.Request] = []
    client, service = await _build(record, positions=[])
    async with client:
        with pytest.raises(TradeValidationError):
            await service.close_position(inst_id="BTC-USDT-SWAP", pos_side="long")


async def test_close_skips_local_check_when_cache_not_ready() -> None:
    """账户缓存未就绪时应放行，交给交易所 reduceOnly 兜底，避免刚开仓就平仓被误拒。"""
    record: list[httpx.Request] = []
    client, service = await _build(record, positions=[], ready=False)
    async with client:
        result = await service.close_position(inst_id="BTC-USDT-SWAP", pos_side="long")
    assert result["mode"] == "full"
    assert record[-1].url.path == "/api/v5/trade/close-position"


async def test_reject_close_size_over_held() -> None:
    record: list[httpx.Request] = []
    client, service = await _build(
        record, positions=[{"instId": "BTC-USDT-SWAP", "posSide": "long", "pos": "2"}]
    )
    async with client:
        with pytest.raises(TradeValidationError):
            await service.close_position(inst_id="BTC-USDT-SWAP", pos_side="long", size=3)


async def test_reject_leverage_over_instrument_max() -> None:
    record: list[httpx.Request] = []
    client, service = await _build(record)
    async with client:
        with pytest.raises(TradeValidationError):
            await service.set_leverage(inst_id="BTC-USDT-SWAP", lever=200, pos_side="long")


async def test_unknown_instrument_rejected() -> None:
    record: list[httpx.Request] = []
    client, service = await _build(record)
    async with client:
        with pytest.raises(InstrumentNotFound):
            await service.open_position(inst_id="BTC-USDC-SWAP", pos_side="long", size=1, price=60000)


async def test_attached_tp_sl_uses_attach_algo_ords() -> None:
    """OKX 已下线顶层 tpTriggerPx（54070），必须挂在 attachAlgoOrds 里。"""
    record: list[httpx.Request] = []
    client, service = await _build(record)
    async with client:
        await service.open_position(
            inst_id="BTC-USDT-SWAP",
            pos_side="long",
            size=1,
            price=60000,
            tp_trigger_px=61000,
            sl_trigger_px=55000,
        )
    body = _last_json(record)
    assert "tpTriggerPx" not in body
    assert "slTriggerPx" not in body
    algo = body["attachAlgoOrds"][0]
    assert algo["tpTriggerPx"] == "61000"
    assert algo["slTriggerPx"] == "55000"
    assert algo["tpOrdPx"] == "-1"
    assert algo["slOrdPx"] == "-1"


async def test_amend_order_sends_new_price() -> None:
    record: list[httpx.Request] = []
    client, service = await _build(record)
    async with client:
        await service.amend_order(inst_id="BTC-USDT-SWAP", ord_id="1001", price=Decimal("60100.09"))
    assert record[-1].url.path == "/api/v5/trade/amend-order"
    body = _last_json(record)
    assert body["newPx"] == "60100.1"
    assert "newSz" not in body
