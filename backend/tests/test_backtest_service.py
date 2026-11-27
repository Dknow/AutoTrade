"""回测引擎测试：信号时点、止损触发、移动止损、统计口径。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.backtest_service import BacktestParams, fetch_history_candles, run_backtest


def _candles(
    closes: list[float], *, step_ms: int = 60_000, wick: float = 0.3, start_index: int = 0
) -> list[list[str]]:
    """把收盘价序列转成 OKX 格式 K 线（开盘价取上一根收盘价）。"""
    rows: list[list[str]] = []
    prev = closes[0]
    for index, close in enumerate(closes):
        open_px = prev
        high = max(open_px, close) + wick
        low = min(open_px, close) - wick
        ts = 1_700_000_000_000 + (start_index + index) * step_ms
        rows.append(
            [str(ts), f"{open_px:.4f}", f"{high:.4f}", f"{low:.4f}", f"{close:.4f}", "0", "0", "0", "1"]
        )
        prev = close
    return rows


def _rise_then_fall() -> list[float]:
    closes = [100.0] * 30
    closes += [100.0 + index for index in range(1, 31)]   # 涨到 130
    closes += [130.0 - index * 1.0 for index in range(1, 31)]  # 跌回 100 附近
    return closes


def _rise_then_crash() -> list[float]:
    closes = [100.0] * 30
    closes += [100.0 + index * 1.0 for index in range(1, 21)]  # 涨到 120
    closes += [100.0]  # 一根急跌 20
    closes += [100.0] * 5
    return closes


def _params(**overrides) -> BacktestParams:
    base = dict(
        inst_id="BTC-USDT-SWAP",
        bar="1m",
        limit=200,
        size=Decimal("1"),
        fee_rate=0.0005,
        initial_equity=1000.0,
        cooldown_bars=0,
    )
    base.update(overrides)
    return BacktestParams(**base)


def test_backtest_enters_on_next_open_and_books_pnl() -> None:
    rows = _candles(_rise_then_fall())
    result = run_backtest(rows, ct_val=1.0, params=_params())

    trades = result["trades"]
    assert trades, "上涨后下跌应至少产生一笔交易"
    assert trades[0]["side"] == "long"

    # 每笔成交价必须能对应到某根 K 线的开盘价（证明用下一根开盘成交，而非信号根收盘）
    opens = {round(float(row[1]), 8) for row in rows}
    for trade in trades:
        assert round(trade["entry_px"], 8) in opens
        assert round(trade["exit_px"], 8) in opens

    summary = result["summary"]
    assert summary["trades"] == len(trades)
    assert summary["fees"] > 0
    assert 0 <= summary["max_drawdown_pct"] <= 100
    # 已实现盈亏之和应等于权益变化（最后强制平仓，无浮盈残留）
    total_pnl = sum(trade["pnl"] for trade in trades)
    assert abs(total_pnl - (summary["final_equity"] - 1000.0)) < 1e-6
    assert result["candles"] == len(rows)


def test_backtest_stop_loss_triggers_with_tight_atr() -> None:
    rows = _candles(_rise_then_fall(), wick=1.5)
    result = run_backtest(rows, ct_val=1.0, params=_params(atr_multiple=0.2, cooldown_bars=0))
    reasons = {trade["reason"] for trade in result["trades"]}
    assert "stop" in reasons, f"极小 ATR 倍数应触发止损，实际 {reasons}"


def test_trailing_stop_locks_profit_before_crash() -> None:
    """急跌前移动止损应已上移，能以高于固定止损的价位离场并保住利润。"""
    rows = _candles(_rise_then_crash())
    # 用较大的 ATR 倍数，保证趋势内的正常波动不会误触发止损
    trailing = run_backtest(rows, ct_val=1.0, params=_params(trailing=True, atr_multiple=6.0))
    fixed = run_backtest(rows, ct_val=1.0, params=_params(trailing=False, atr_multiple=6.0))

    first_trailing = trailing["trades"][0]
    first_fixed = fixed["trades"][0]
    assert first_trailing["reason"] == "stop"
    assert first_trailing["pnl"] > 0, "上涨后急跌，移动止损应锁住部分利润"
    assert first_trailing["exit_px"] > first_fixed["exit_px"]
    assert first_trailing["pnl"] > first_fixed["pnl"]


def test_backtest_rejects_bad_params() -> None:
    rows = _candles(_rise_then_fall())
    with pytest.raises(ValueError, match="快线周期必须小于慢线周期"):
        run_backtest(rows, ct_val=1.0, params=_params(fast=30, slow=10))
    with pytest.raises(ValueError, match="K 线数量"):
        run_backtest(rows[:5], ct_val=1.0, params=_params())


class FakeCandlesClient:
    def __init__(self, pages: list[list[list[str]]]) -> None:
        self.pages = pages
        self.calls: list[dict] = []

    async def get_candles(self, inst_id: str, *, bar: str = "1m", limit: int = 100, after=None, before=None):
        self.calls.append({"inst_id": inst_id, "bar": bar, "limit": limit, "after": after})
        index = len(self.calls) - 1
        return self.pages[index] if index < len(self.pages) else []


async def test_fetch_history_candles_paginates_and_dedupes() -> None:
    page1 = _candles([100.0 + index for index in range(300)])
    page2 = _candles([100.0 + index for index in range(300, 600)], start_index=300)
    client = FakeCandlesClient([page2, page1])
    rows = await fetch_history_candles(client, "BTC-USDT-SWAP", "1m", 600)

    assert len(rows) == 600
    timestamps = [int(row[0]) for row in rows]
    assert timestamps == sorted(timestamps)
    assert len(set(timestamps)) == len(timestamps)
    # 第二页必须带上第一页最早时间戳（OKX 用 after 向历史翻页）
    assert client.calls[1]["after"] == page2[0][0]
    assert client.calls[0]["limit"] == 300
