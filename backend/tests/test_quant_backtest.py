"""量化回测框架：无未来函数、成本模型、指标口径与 Purged Walk-Forward。"""

from __future__ import annotations

import math

import pytest

from app.services.quant_backtest import (
    Bar,
    CostModel,
    QuantParams,
    bars_from_rows,
    cost_sensitivity,
    purged_walk_forward,
    run_quant_backtest,
)

BASE_TS = 1_700_000_000_000
STEP_MS = 4 * 3600 * 1000


def _breakout_bars() -> list[Bar]:
    """前 29 根横盘 → 第 29 根突破 → 第 30 根跳空高开 → 之后涨到 120。"""
    bars: list[Bar] = []
    for index in range(29):
        bars.append(Bar(BASE_TS + index * STEP_MS, 100.0, 101.0, 99.0, 100.0))
    bars.append(Bar(BASE_TS + 29 * STEP_MS, 100.0, 111.0, 100.0, 110.0))
    bars.append(Bar(BASE_TS + 30 * STEP_MS, 115.0, 116.0, 114.0, 115.0))
    for index in range(31, 41):
        bars.append(Bar(BASE_TS + index * STEP_MS, 119.0, 121.0, 118.0, 120.0))
    return bars


def _wave_bars(count: int = 1200) -> list[Bar]:
    bars: list[Bar] = []
    for index in range(count):
        price = 100 + 18 * math.sin(index / 17) + index * 0.05
        open_px = price
        close = price * (1 + 0.01 * math.sin(index / 5))
        high = max(open_px, close) * 1.004
        low = min(open_px, close) * 0.996
        bars.append(Bar(BASE_TS + index * STEP_MS, open_px, high, low, close))
    return bars


def _params(**overrides) -> QuantParams:
    base = dict(strategy="donchian", period=5, atr_period=14, atr_multiple=1.5, rr=2.0)
    base.update(overrides)
    return QuantParams(**base)


def test_entry_uses_next_bar_open_not_signal_close() -> None:
    bars = _breakout_bars()
    result = run_quant_backtest(
        bars,
        _params(),
        cost=CostModel(fee_rate=0.0005, slippage_rate=0.0002),
        ct_val=1.0,
    )

    trades = result["trades"]
    assert trades, "突破后应产生交易"
    trade = trades[0]
    assert trade["side"] == "long"
    assert trade["entry_ts"] == bars[30].ts, "信号在第 29 根收盘，必须在第 30 根成交"
    assert trade["entry_px"] == pytest.approx(115.0 * 1.0002, rel=1e-9)
    assert trade["exit_px"] > trade["entry_px"]
    assert trade["r"] > 0
    assert result["metrics"]["expectancy_r"] == pytest.approx(trade["r"], abs=1e-4)


def test_cost_sensitivity_decays_with_costs() -> None:
    bars = _breakout_bars()
    rows = cost_sensitivity(bars, _params(), multipliers=(0.0, 1.0, 3.0))
    assert [row["multiplier"] for row in rows] == [0.0, 1.0, 3.0]
    assert rows[0]["expectancy_r"] >= rows[1]["expectancy_r"] >= rows[2]["expectancy_r"]


def test_metrics_require_minimum_bars() -> None:
    with pytest.raises(ValueError, match="至少需要 3 根"):
        run_quant_backtest(_breakout_bars()[:2], _params())


def test_bars_from_rows_sorts_and_parses() -> None:
    rows = [
        ["2", "1", "2", "0.5", "1.5", "10"],
        ["1", "1", "2", "0.5", "1.0", ""],
    ]
    bars = bars_from_rows(rows)
    assert [bar.ts for bar in bars] == [1, 2]
    assert bars[0].vol == 0.0
    assert bars[1].vol == 10.0


def test_purged_walk_forward_keeps_gap_between_train_and_test() -> None:
    bars = _wave_bars(1200)
    result = purged_walk_forward(
        bars,
        strategy="donchian",
        folds=2,
        train_bars=400,
        test_bars=100,
        purge_bars=5,
        embargo_bars=5,
        min_train_trades=1,
        cost=CostModel(),
    )

    assert result["folds"] == 2
    assert result["purge_bars"] == 5
    assert result["embargo_bars"] == 5
    for fold in result["detail"]:
        window = fold["window"]
        assert window["test_start"] - window["train_end"] == 10, "训练与测试之间必须留出空档"
        assert window["train_end"] > window["train_start"]
        assert window["test_end"] > window["test_start"]
    assert result["aggregate"]["trades"] >= 0


def test_purged_walk_forward_rejects_short_history() -> None:
    with pytest.raises(ValueError, match="数据不足"):
        purged_walk_forward(
            _wave_bars(200),
            strategy="donchian",
            folds=3,
            train_bars=400,
            test_bars=100,
            purge_bars=5,
            embargo_bars=5,
        )
