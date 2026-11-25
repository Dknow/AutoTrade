"""策略引擎单测：用假行情/假交易验证信号、冷却、反向平仓与风控拦截。

不联网、不下真单，重点验证"什么信号下做了什么动作"。
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import Settings
from app.services.risk_service import RiskLimitExceeded, RiskService
from app.services.strategy_service import StrategyConfig, StrategyService

INST = "BTC-USDT-SWAP"


class FakeMarket:
    inst_id = INST
    bar = "1m"

    def __init__(self, closes: list[float]) -> None:
        self.tickers: dict[str, dict[str, Any]] = {INST: {"last": str(closes[-1])}}
        # 传入的 closes 全部视为已收盘，额外补一根未收盘 K 线（策略会丢弃它）
        self._rows = [_row(index, close) for index, close in enumerate(closes)]
        self._rows.append(_row(len(closes), closes[-1]))
        self.listeners: list[Any] = []

    @property
    def ticker(self) -> dict[str, Any]:
        return self.ticker_for(self.inst_id)

    def ticker_for(self, inst_id: str) -> dict[str, Any]:
        return self.tickers.get(inst_id, {})

    def has(self, inst_id: str) -> bool:
        return inst_id in self.tickers

    async def add_instrument(self, inst_id: str) -> bool:
        self.tickers.setdefault(inst_id, {})
        return True

    def add_candle_listener(self, listener: Any) -> None:
        self.listeners.append(listener)

    def candles(self, limit: int = 200) -> list[list[str]]:
        return self.candles_for(self.inst_id, limit)

    def candles_for(self, inst_id: str, limit: int = 200) -> list[list[str]]:
        # 与真实服务一致：倒序返回，最后一根视为未收盘
        return list(reversed(self._rows))[:limit]


class FakeTrade:
    def __init__(self) -> None:
        self.opened: list[dict] = []
        self.closed: list[dict] = []
        self.stops: list[dict] = []
        self.trailing_stops: list[dict] = []
        self.cancelled: list[dict] = []
        self.raise_on_open: Exception | None = None

    async def open_position(self, **kwargs: Any) -> dict:
        if self.raise_on_open:
            raise self.raise_on_open
        self.opened.append(kwargs)
        return {"order": {"ordId": "fake-1"}}

    async def close_position(self, **kwargs: Any) -> dict:
        self.closed.append(kwargs)
        return {"closed": [], "mode": "full"}

    async def place_stop_loss(self, **kwargs: Any) -> dict:
        self.stops.append(kwargs)
        return {
            "algo": {"algoId": f"algo-{len(self.stops)}"},
            "triggerPx": str(kwargs["trigger_px"]),
            "sz": str(kwargs["size"]),
        }

    async def place_trailing_stop(self, **kwargs: Any) -> dict:
        self.trailing_stops.append(kwargs)
        return {
            "algo": {"algoId": f"trail-{len(self.trailing_stops)}"},
            "callbackSpread": str(kwargs["callback_spread"]),
            "sz": str(kwargs["size"]),
        }

    async def cancel_stop_loss(self, **kwargs: Any) -> dict:
        self.cancelled.append(kwargs)
        return {"cancelled": list(kwargs["algo_ids"])}


class FakeAccount:
    def __init__(self, positions: list[dict] | None = None) -> None:
        self._positions = positions or []
        self.account: dict[str, Any] = {"totalEq": "1000"}

    def active_positions(self) -> list[dict]:
        return self._positions


class FakeInstruments:
    def get(self, inst_id: str) -> SimpleNamespace:
        return SimpleNamespace(ct_val=Decimal("0.01"), tick_sz=Decimal("0.1"))


class FakeStore:
    def __init__(self, data: dict | None = None) -> None:
        self.data: dict = data or {}

    def load(self) -> dict:
        return self.data

    def save(self, payload: dict) -> None:
        self.data = dict(payload)


def _row(ts: int, close: float) -> list[str]:
    return [str(ts * 60_000), str(close), str(close + 1), str(close - 1), str(close), "0", "0", "0", "1"]


def _build(
    closes: list[float],
    *,
    positions: list[dict] | None = None,
    size: str = "0.01",
    cooldown: int = 0,
    risk_max_size: str = "1",
    risk_max_notional: float = 100000,
    trailing: bool = True,
    store: Any | None = None,
) -> tuple[StrategyService, FakeMarket, FakeTrade, FakeAccount]:
    settings = Settings(risk_max_size=risk_max_size, risk_max_notional=risk_max_notional)
    market = FakeMarket(closes)
    trade = FakeTrade()
    account = FakeAccount(positions)
    risk = RiskService(settings, account)  # type: ignore[arg-type]
    service = StrategyService(
        settings=settings,
        market=market,  # type: ignore[arg-type]
        trade=trade,  # type: ignore[arg-type]
        account=account,  # type: ignore[arg-type]
        risk=risk,
        instruments=FakeInstruments(),  # type: ignore[arg-type]
        config=StrategyConfig(
            fast=2,
            slow=4,
            atr_period=2,
            atr_multiple=1.0,
            size=Decimal(size),
            cooldown_bars=cooldown,
            trailing=trailing,
        ),
        store=store,
    )
    return service, market, trade, account


# 先横盘、再下跌、最后拉高，构造出金叉
GOLDEN_CLOSES = [5.0] * 5 + [3.0] * 5 + [20.0]


async def test_golden_cross_opens_long_with_native_trailing_stop() -> None:
    service, _, trade, _ = _build(GOLDEN_CLOSES)
    service.running = True
    await service.on_candle(INST)

    assert len(trade.opened) == 1
    order = trade.opened[0]
    assert order["pos_side"] == "long"
    assert order["ord_type"] == "market"
    # 默认开启移动止损：由交易所原生 move_order_stop 维护，不再挂固定条件单
    assert trade.stops == []
    assert len(trade.trailing_stops) == 1
    assert trade.trailing_stops[0]["pos_side"] == "long"
    assert Decimal(str(trade.trailing_stops[0]["callback_spread"])) > 0
    assert service.stop_spread is not None
    assert service.stop_price is None
    assert service.last_signal == "long"


async def test_same_candle_is_not_evaluated_twice() -> None:
    service, _, trade, _ = _build(GOLDEN_CLOSES)
    service.running = True
    await service.on_candle(INST)
    await service.on_candle(INST)
    assert len(trade.opened) == 1


async def test_does_nothing_when_not_running() -> None:
    service, _, trade, _ = _build(GOLDEN_CLOSES)
    await service.on_candle(INST)
    assert trade.opened == []


async def test_insufficient_history_does_nothing() -> None:
    service, _, trade, _ = _build([5.0, 4.0, 3.0])
    service.running = True
    await service.on_candle(INST)
    assert trade.opened == []


async def test_reverse_signal_closes_opposite_position_first() -> None:
    service, _, trade, _ = _build(
        GOLDEN_CLOSES,
        positions=[{"instId": INST, "posSide": "short", "pos": "0.01"}],
    )
    service.running = True
    await service.on_candle(INST)

    assert len(trade.closed) == 1
    assert trade.closed[0]["pos_side"] == "short"
    assert len(trade.opened) == 1
    assert trade.opened[0]["pos_side"] == "long"


async def test_risk_limit_blocks_open() -> None:
    service, _, trade, _ = _build(GOLDEN_CLOSES, size="5", risk_max_size="1")
    service.running = True
    await service.on_candle(INST)

    assert trade.opened == []
    assert "风控拦截" in service.last_error or "超过单笔上限" in service.last_error
    assert any(record.event == "risk" for record in service.records)


async def test_open_failure_is_recorded_and_does_not_raise() -> None:
    service, _, trade, _ = _build(GOLDEN_CLOSES)
    trade.raise_on_open = RuntimeError("交易所拒绝")
    service.running = True
    await service.on_candle(INST)

    assert service.last_error == "交易所拒绝"
    assert any(record.event == "open" and not record.ok for record in service.records)


async def test_invalid_config_rejected() -> None:
    service, _, _, _ = _build(GOLDEN_CLOSES)
    with pytest.raises(ValueError):
        await service.update_config(StrategyConfig(fast=10, slow=5))


def test_snapshot_contains_records_and_config() -> None:
    service, _, _, _ = _build(GOLDEN_CLOSES)
    snapshot = service.snapshot()
    assert snapshot["running"] is False
    assert snapshot["config"]["fast"] == 2
    assert isinstance(snapshot["records"], list)


async def test_cooldown_blocks_consecutive_signal() -> None:
    # 先金叉开多，紧接着死叉；冷却期内应拦截，不产生第二次开仓
    service, market, trade, _ = _build(GOLDEN_CLOSES, cooldown=30)
    service.running = True
    await service.on_candle(INST)
    assert len(trade.opened) == 1

    # 追加一根收盘价 1.0 的已收盘 K 线（触发死叉）+ 一根未收盘 K 线
    next_ts = len(GOLDEN_CLOSES) + 1
    market._rows.extend([_row(next_ts, 1.0), _row(next_ts + 1, 1.0)])
    await service.on_candle(INST)

    assert len(trade.opened) == 1
    assert any(record.event == "skip" for record in service.records)


async def test_trailing_stop_is_maintained_by_exchange() -> None:
    service, market, trade, account = _build(GOLDEN_CLOSES)
    service.running = True
    await service.on_candle(INST)
    assert len(trade.trailing_stops) == 1

    # 价格继续上涨：交易所自己上移触发价，策略不应重复撤挂
    account._positions = [{"instId": INST, "posSide": "long", "pos": "0.01"}]
    market.tickers[INST]["last"] = "40"
    next_ts = len(GOLDEN_CLOSES) + 1
    market._rows.extend([_row(next_ts, 40.0), _row(next_ts + 1, 40.0)])
    await service.on_candle(INST)

    assert trade.cancelled == []
    assert len(trade.trailing_stops) == 1
    assert service.stop_spread is not None


async def test_trailing_disabled_uses_fixed_conditional_stop() -> None:
    service, market, trade, account = _build(GOLDEN_CLOSES, trailing=False)
    service.running = True
    await service.on_candle(INST)
    assert trade.trailing_stops == []
    assert len(trade.stops) == 1
    assert trade.stops[0]["pos_side"] == "long"
    assert service.stop_price is not None
    assert service.stop_spread is None

    account._positions = [{"instId": INST, "posSide": "long", "pos": "0.01"}]
    market.tickers[INST]["last"] = "40"
    next_ts = len(GOLDEN_CLOSES) + 1
    market._rows.extend([_row(next_ts, 40.0), _row(next_ts + 1, 40.0)])
    await service.on_candle(INST)

    assert trade.cancelled == []
    assert len(trade.stops) == 1


async def test_config_is_persisted_and_restored() -> None:
    store = FakeStore()
    service, _, _, _ = _build(GOLDEN_CLOSES, store=store)
    service.running = True
    await service.on_candle(INST)
    assert store.data["last_signal"] == "long"

    # 重启：用同一份存储重建服务，应恢复配置与信号，但不恢复运行状态与止损单
    restored, _, _, _ = _build(GOLDEN_CLOSES, store=store)
    assert restored.config.fast == 2
    assert restored.last_signal == "long"
    assert restored.running is False
    assert restored.stop_price is None
    assert restored.stop_spread is None
    assert restored.stop_algo_ids == []
