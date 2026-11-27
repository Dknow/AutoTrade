"""量化回测框架：信号回测 + 成本模型 + 统计指标 + Purged Walk-Forward。

无未来函数约定：
- 指标只使用第 i 根及之前的数据（滚动窗口，见 indicators 模块）；
- 第 i 根收盘产生信号，第 i+1 根**开盘价**成交；
- 同一根 K 线内止损优先于止盈，止损后不在同根反手；
- Walk-Forward 的训练段与测试段之间强制留出 purge + embargo 的空档，
  避免重叠持仓与参数选择泄漏。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Sequence

from app.services.indicators import (
    atr_series,
    bollinger_series,
    donchian_series,
)

SUPPORTED_STRATEGIES = ("bollinger_mr", "donchian")

DEFAULT_FEE_RATE = 0.0005      # taker 0.05%
DEFAULT_SLIPPAGE_RATE = 0.0002  # 单边 0.02%

_BARS_PER_YEAR = {
    "1m": 525600,
    "3m": 175200,
    "5m": 105120,
    "15m": 35040,
    "30m": 17520,
    "1H": 8760,
    "2H": 4380,
    "4H": 2190,
    "6H": 1460,
    "12H": 730,
    "1D": 365,
    "2D": 182,
    "3D": 121,
    "1W": 52,
}

_MAX_CURVE_POINTS = 400
_MAX_TRADE_POINTS = 500


def bars_per_year(bar: str) -> float:
    """把 OKX 周期字符串换算成每年根数，用于夏普年化。"""
    key = (bar or "").strip()
    if not key:
        return 0.0
    normalized = key[:-1] + key[-1].upper()
    return float(_BARS_PER_YEAR.get(key) or _BARS_PER_YEAR.get(normalized) or 0.0)


@dataclass(frozen=True)
class Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    vol: float = 0.0


def bars_from_rows(rows: Iterable[Sequence[Any]]) -> list[Bar]:
    """把 OKX K 线（字符串数组）转成按时间升序的 Bar 列表。"""
    ordered: list[Bar] = []
    for row in sorted(rows, key=lambda item: int(item[0])):
        vol = 0.0
        if len(row) > 5 and str(row[5]).strip() not in ("", "None"):
            try:
                vol = float(row[5])
            except (TypeError, ValueError):
                vol = 0.0
        ordered.append(
            Bar(
                ts=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                vol=vol,
            )
        )
    return ordered


@dataclass(frozen=True)
class CostModel:
    """交易成本：手续费按名义价值双边、滑点按成交价单边不利、资金费率按持仓根数。"""

    fee_rate: float = DEFAULT_FEE_RATE
    slippage_rate: float = DEFAULT_SLIPPAGE_RATE
    funding_rate_per_bar: float = 0.0

    def validate(self) -> None:
        if self.fee_rate < 0 or self.slippage_rate < 0 or self.funding_rate_per_bar < 0:
            raise ValueError("成本参数不能为负")

    def scaled(self, multiplier: float) -> "CostModel":
        return CostModel(
            fee_rate=self.fee_rate * multiplier,
            slippage_rate=self.slippage_rate * multiplier,
            funding_rate_per_bar=self.funding_rate_per_bar * multiplier,
        )

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True)
class QuantParams:
    strategy: str = "bollinger_mr"
    period: int = 20
    k: float = 2.0
    atr_period: int = 14
    atr_multiple: float = 1.5
    rr: float = 2.0
    allow_short: bool = False
    max_hold_bars: int = 0

    def validate(self) -> None:
        if self.strategy not in SUPPORTED_STRATEGIES:
            raise ValueError(f"不支持的策略: {self.strategy}，可选 {SUPPORTED_STRATEGIES}")
        if self.period < 2:
            raise ValueError("指标周期必须大于等于 2")
        if self.k <= 0:
            raise ValueError("布林带宽倍数必须大于 0")
        if self.atr_period <= 0:
            raise ValueError("ATR 周期必须大于 0")
        if self.atr_multiple <= 0:
            raise ValueError("ATR 止损倍数必须大于 0")
        if self.rr < 0:
            raise ValueError("盈亏比倍数不能为负")
        if self.max_hold_bars < 0:
            raise ValueError("最大持仓根数不能为负")

    def warmup(self) -> int:
        return max(self.period + 1, self.atr_period + 1) + 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Indicators:
    upper: list[float | None]
    lower: list[float | None]
    atr: list[float | None]


def prepare_indicators(bars: list[Bar], params: QuantParams) -> _Indicators:
    """在完整序列上一次性算好指标；所有值都只依赖当根及之前的数据。"""
    closes = [bar.close for bar in bars]
    rows = [
        [str(bar.ts), str(bar.open), str(bar.high), str(bar.low), str(bar.close)] for bar in bars
    ]
    if params.strategy == "donchian":
        upper, lower = donchian_series(rows, params.period)
    else:
        _, upper, lower = bollinger_series(closes, params.period, params.k)
    return _Indicators(
        upper=upper,
        lower=lower,
        atr=atr_series(rows, params.atr_period),
    )


def _entry_signal(
    bars: list[Bar], ind: _Indicators, params: QuantParams, index: int
) -> str | None:
    """第 index 根收盘后的入场信号（第 index+1 根开盘执行）。

    离场只由止损 / 止盈 / 最大持仓根数决定，反向信号不会立刻平仓，
    与既有基线（1.5×ATR 止损 + 2R 止盈 + 时间止损）口径一致。
    """
    close = bars[index].close
    upper, lower = ind.upper[index], ind.lower[index]
    if upper is None or lower is None:
        return None
    if params.strategy == "donchian":
        if close > upper:
            return "long"
        if params.allow_short and close < lower:
            return "short"
        return None
    if close <= lower:
        return "long"
    if params.allow_short and close >= upper:
        return "short"
    return None


def _sample_curve(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(points) <= _MAX_CURVE_POINTS:
        return points
    step = len(points) / _MAX_CURVE_POINTS
    sampled = [points[int(index * step)] for index in range(_MAX_CURVE_POINTS)]
    sampled[-1] = points[-1]
    return sampled


def _sample_trades(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(trades) <= _MAX_TRADE_POINTS:
        return trades
    step = len(trades) / _MAX_TRADE_POINTS
    sampled = [trades[int(index * step)] for index in range(_MAX_TRADE_POINTS)]
    sampled[-1] = trades[-1]
    return sampled


def _max_drawdown_pct(curve: list[dict[str, Any]]) -> float | None:
    if len(curve) < 2:
        return None
    peak = 0.0
    worst = 0.0
    for point in curve:
        value = float(point["equity"])
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak * 100)
    return round(worst, 4)


def _sharpe(curve: list[dict[str, Any]], initial_equity: float, bpy: float) -> float | None:
    if len(curve) < 3 or initial_equity <= 0 or bpy <= 0:
        return None
    returns: list[float] = []
    previous = float(curve[0]["equity"])
    for point in curve[1:]:
        current = float(point["equity"])
        returns.append((current - previous) / initial_equity)
        previous = current
    if not returns:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / len(returns)
    std = variance**0.5
    if std <= 0:
        return None
    return round(mean / std * math.sqrt(bpy), 4)


def _max_consecutive_losses(trades: list[dict[str, Any]]) -> int:
    worst = current = 0
    for trade in trades:
        if trade["pnl"] > 0:
            current = 0
        else:
            current += 1
            worst = max(worst, current)
    return worst


def _metrics(
    trades: list[dict[str, Any]],
    curve: list[dict[str, Any]],
    initial_equity: float,
    bpy: float,
) -> dict[str, Any]:
    wins = [trade for trade in trades if trade["pnl"] > 0]
    losses = [trade for trade in trades if trade["pnl"] <= 0]
    gross_win = sum(trade["pnl"] for trade in wins)
    gross_loss = abs(sum(trade["pnl"] for trade in losses))
    r_values = [trade["r"] for trade in trades]
    net_pnl = sum(trade["pnl"] for trade in trades)
    final_equity = initial_equity + net_pnl
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "expectancy_r": round(sum(r_values) / len(r_values), 4) if r_values else 0.0,
        "total_r": round(sum(r_values), 4),
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else None,
        "net_pnl": round(net_pnl, 6),
        "return_pct": round(net_pnl / initial_equity * 100, 4) if initial_equity > 0 else 0.0,
        "final_equity": round(final_equity, 6),
        "max_drawdown_pct": _max_drawdown_pct(curve),
        "sharpe": _sharpe(curve, initial_equity, bpy),
        "avg_bars": round(sum(trade["bars"] for trade in trades) / len(trades), 2) if trades else 0.0,
        "max_consecutive_losses": _max_consecutive_losses(trades),
        "fees": round(sum(trade["fee"] for trade in trades), 6),
        "funding": round(sum(trade["funding"] for trade in trades), 6),
    }


def run_quant_backtest(
    bars: list[Bar],
    params: QuantParams,
    *,
    cost: CostModel | None = None,
    ct_val: float = 1.0,
    risk_pct: float = 0.01,
    initial_equity: float = 10_000.0,
    start: int = 0,
    end: int | None = None,
    prepared: _Indicators | None = None,
) -> dict[str, Any]:
    """在 bars[start:end] 上执行回测；指标仍基于完整序列计算，保证预热正确。

    仓位按「每笔风险 = 当前权益 × risk_pct」反推张数，因此 R、夏普、最大回撤
    在不同标的与不同价格区间之间可直接比较；固定张数会让这两个指标失去意义。
    """
    params.validate()
    cost = cost or CostModel()
    cost.validate()
    if ct_val <= 0:
        raise ValueError("合约面值必须大于 0")
    if not 0 < risk_pct <= 0.5:
        raise ValueError("单笔风险比例需在 (0, 0.5] 区间")
    if initial_equity <= 0:
        raise ValueError("初始权益必须大于 0")

    total = len(bars)
    end_index = total if end is None else min(int(end), total)
    start_index = max(int(start), 0)
    if end_index - start_index < 3:
        raise ValueError("回测区间至少需要 3 根 K 线")
    ind = prepared or prepare_indicators(bars, params)

    equity = float(initial_equity)
    position: dict[str, Any] | None = None
    trades: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    no_pending = object()
    pending: Any = no_pending
    slip = cost.slippage_rate

    def close_position(exit_px: float, ts: int, reason: str, index: int) -> None:
        nonlocal equity, position
        assert position is not None
        contracts = position["size"]
        direction = 1 if position["side"] == "long" else -1
        gross = (exit_px - position["entry_px"]) * direction * contracts * ct_val
        fee_close = exit_px * contracts * ct_val * cost.fee_rate
        equity += gross - fee_close
        pnl = gross - position["fee_open"] - fee_close - position["funding"]
        risk = position["risk"]
        trades.append(
            {
                "side": position["side"],
                "entry_ts": position["entry_ts"],
                "entry_px": round(position["entry_px"], 8),
                "exit_ts": ts,
                "exit_px": round(exit_px, 8),
                "size": round(contracts, 8),
                "bars": index - position["entry_index"],
                "risk": round(risk, 6),
                "pnl": round(pnl, 6),
                "r": round(pnl / risk, 4) if risk > 0 else 0.0,
                "fee": round(position["fee_open"] + fee_close, 6),
                "funding": round(position["funding"], 6),
                "reason": reason,
            }
        )
        position = None

    for index in range(start_index, end_index):
        bar = bars[index]

        # 1) 本根开盘执行上一根收盘留下的入场指令（只在空仓时）
        if pending is not no_pending:
            side = pending
            pending = no_pending
            if position is None and side is not None and index > 0:
                atr_value = ind.atr[index - 1]
                if atr_value and atr_value > 0:
                    entry_px = bar.open * (1 + slip) if side == "long" else bar.open * (1 - slip)
                    stop_distance = atr_value * params.atr_multiple
                    risk_budget = equity * risk_pct
                    contracts = risk_budget / (stop_distance * ct_val) if risk_budget > 0 else 0.0
                    if contracts > 0:
                        risk = stop_distance * contracts * ct_val
                        fee_open = entry_px * contracts * ct_val * cost.fee_rate
                        equity -= fee_open
                        reward_distance = stop_distance * params.rr
                        position = {
                            "side": side,
                            "entry_px": entry_px,
                            "entry_ts": bar.ts,
                            "entry_index": index,
                            "stop": entry_px - stop_distance if side == "long" else entry_px + stop_distance,
                            "target_px": (
                                (entry_px + reward_distance if side == "long" else entry_px - reward_distance)
                                if params.rr > 0
                                else None
                            ),
                            "size": contracts,
                            "risk": risk,
                            "fee_open": fee_open,
                            "funding": 0.0,
                        }

        # 2) 持仓中：资金费率 + 止损（优先）+ 止盈 + 时间止损
        if position is not None:
            contracts = position["size"]
            funding = bar.close * contracts * ct_val * cost.funding_rate_per_bar
            if funding:
                equity -= funding
                position["funding"] += funding

            if position["side"] == "long" and bar.low <= position["stop"]:
                exit_px = min(bar.open, position["stop"]) * (1 - slip)
                close_position(exit_px, bar.ts, "stop", index)
            elif position["side"] == "short" and bar.high >= position["stop"]:
                exit_px = max(bar.open, position["stop"]) * (1 + slip)
                close_position(exit_px, bar.ts, "stop", index)
            elif position["target_px"] is not None and position["side"] == "long" and bar.high >= position["target_px"]:
                exit_px = max(bar.open, position["target_px"]) * (1 - slip)
                close_position(exit_px, bar.ts, "target", index)
            elif position["target_px"] is not None and position["side"] == "short" and bar.low <= position["target_px"]:
                exit_px = min(bar.open, position["target_px"]) * (1 + slip)
                close_position(exit_px, bar.ts, "target", index)
            elif params.max_hold_bars and index - position["entry_index"] >= params.max_hold_bars:
                exit_px = bar.close * (1 - slip) if position["side"] == "long" else bar.close * (1 + slip)
                close_position(exit_px, bar.ts, "max_hold", index)

        # 3) 收盘后基于已收盘数据生成下一根开盘的入场信号
        if position is None and index + 1 < end_index:
            signal = _entry_signal(bars, ind, params, index)
            if signal is not None:
                pending = signal

        # 4) 记录收盘浮动权益
        unrealized = 0.0
        if position is not None:
            direction = 1 if position["side"] == "long" else -1
            unrealized = (bar.close - position["entry_px"]) * direction * position["size"] * ct_val
        curve.append({"ts": bar.ts, "equity": equity + unrealized})

    # 区间结束仍持仓：按最后一根收盘价强制平仓
    if position is not None:
        last_index = end_index - 1
        last_close = bars[last_index].close
        exit_px = last_close * (1 - slip) if position["side"] == "long" else last_close * (1 + slip)
        close_position(exit_px, bars[last_index].ts, "end", last_index)
        curve[-1] = {"ts": curve[-1]["ts"], "equity": equity}

    rounded_curve = [
        {"ts": point["ts"], "equity": round(float(point["equity"]), 6)} for point in curve
    ]
    bpy = bars_per_year(_infer_bar(bars))
    return {
        "params": params.to_dict(),
        "cost": cost.to_dict(),
        "range": {"start": bars[start_index].ts, "end": bars[end_index - 1].ts},
        "bars": end_index - start_index,
        "metrics": _metrics(trades, rounded_curve, float(initial_equity), bpy),
        "equity": _sample_curve(rounded_curve),
        "trades": _sample_trades(trades),
        "all_trades": trades,
    }


def _infer_bar(bars: list[Bar]) -> str:
    """用相邻时间戳推断周期，避免调用方忘记传 bar。"""
    if len(bars) < 2:
        return ""
    step_ms = bars[1].ts - bars[0].ts
    if step_ms <= 0:
        return ""
    hours = step_ms / 3_600_000
    for label, year_bars in _BARS_PER_YEAR.items():
        if year_bars <= 0:
            continue
        if abs(hours - (365 * 24 / year_bars)) < 0.01:
            return label
    return ""


def cost_sensitivity(
    bars: list[Bar],
    params: QuantParams,
    *,
    multipliers: Sequence[float] = (0.0, 0.5, 1.0, 2.0, 3.0),
    cost: CostModel | None = None,
    ct_val: float = 1.0,
    risk_pct: float = 0.01,
    initial_equity: float = 10_000.0,
) -> list[dict[str, Any]]:
    """按成本倍数重跑，观察期望 R 的衰减；0 倍即毛收益。"""
    base_cost = cost or CostModel()
    prepared = prepare_indicators(bars, params)
    rows: list[dict[str, Any]] = []
    for multiplier in multipliers:
        result = run_quant_backtest(
            bars,
            params,
            cost=base_cost.scaled(float(multiplier)),
            ct_val=ct_val,
            risk_pct=risk_pct,
            initial_equity=initial_equity,
            prepared=prepared,
        )
        metrics = result["metrics"]
        rows.append(
            {
                "multiplier": float(multiplier),
                "expectancy_r": metrics["expectancy_r"],
                "net_pnl": metrics["net_pnl"],
                "sharpe": metrics["sharpe"],
                "trades": metrics["trades"],
            }
        )
    return rows


DEFAULT_GRIDS: dict[str, list[QuantParams]] = {
    "bollinger_mr": [
        QuantParams(strategy="bollinger_mr", period=period, k=k, atr_period=14, atr_multiple=atr)
        for period in (15, 20, 25)
        for k in (1.8, 2.0, 2.2)
        for atr in (1.5, 2.0)
    ],
    "donchian": [
        QuantParams(strategy="donchian", period=period, atr_period=14, atr_multiple=atr, rr=rr)
        for period in (15, 20, 25)
        for atr in (1.5, 2.0)
        for rr in (1.5, 2.0, 3.0)
    ],
}


def purged_walk_forward(
    bars: list[Bar],
    *,
    strategy: str,
    cost: CostModel | None = None,
    ct_val: float = 1.0,
    risk_pct: float = 0.01,
    initial_equity: float = 10_000.0,
    folds: int = 3,
    train_bars: int | None = None,
    test_bars: int | None = None,
    purge_bars: int = 24,
    embargo_bars: int = 24,
    min_train_trades: int = 5,
    grid: Sequence[QuantParams] | None = None,
) -> dict[str, Any]:
    """滚动样本外验证：训练段选参 → 空档（purge + embargo）→ 测试段评估。"""
    if strategy not in SUPPORTED_STRATEGIES:
        raise ValueError(f"不支持的策略: {strategy}")
    if folds < 1:
        raise ValueError("folds 必须大于等于 1")
    if purge_bars < 0 or embargo_bars < 0:
        raise ValueError("purge/embargo 不能为负")
    if train_bars is None:
        train_bars = max(300, len(bars) // 2)
    if test_bars is None:
        test_bars = max(100, len(bars) // 6)
    if train_bars < 2 or test_bars < 2:
        raise ValueError("训练/测试窗口过短")

    gap = int(purge_bars) + int(embargo_bars)
    windows: list[dict[str, int]] = []
    for fold in range(folds):
        test_end = len(bars) - fold * test_bars
        test_start = test_end - test_bars
        train_end = test_start - gap
        train_start = train_end - train_bars
        if train_start < 0:
            break
        windows.append(
            {
                "train_start": train_start,
                "train_end": train_end,
                "test_start": test_start,
                "test_end": test_end,
                "gap_bars": gap,
            }
        )
    windows.reverse()
    if not windows:
        raise ValueError(
            "数据不足以构造 walk-forward 窗口：需要至少 "
            f"{train_bars + gap + test_bars} 根 K 线，当前 {len(bars)} 根"
        )

    combos = list(grid or DEFAULT_GRIDS[strategy])
    if not combos:
        raise ValueError("参数网格为空")

    base_cost = cost or CostModel()
    train_scores: list[list[dict[str, Any]]] = [[{} for _ in combos] for _ in windows]
    for combo_index, combo in enumerate(combos):
        prepared = prepare_indicators(bars, combo)
        for window_index, window in enumerate(windows):
            result = run_quant_backtest(
                bars,
                combo,
                cost=base_cost,
                ct_val=ct_val,
                risk_pct=risk_pct,
                initial_equity=initial_equity,
                start=window["train_start"],
                end=window["train_end"],
                prepared=prepared,
            )
            train_scores[window_index][combo_index] = result["metrics"]

    fold_results: list[dict[str, Any]] = []
    oos_trades: list[dict[str, Any]] = []
    skipped = 0
    for window_index, window in enumerate(windows):
        best_index = -1
        best_score = -math.inf
        for combo_index, metrics in enumerate(train_scores[window_index]):
            if metrics["trades"] < min_train_trades:
                continue
            score = metrics["expectancy_r"]
            if score > best_score:
                best_score = score
                best_index = combo_index
        if best_index < 0:
            skipped += 1
            fold_results.append(
                {
                    "index": window_index,
                    "window": window,
                    "skipped": True,
                    "reason": f"训练段没有参数达到最少 {min_train_trades} 笔交易",
                }
            )
            continue

        chosen = combos[best_index]
        prepared = prepare_indicators(bars, chosen)
        oos = run_quant_backtest(
            bars,
            chosen,
            cost=base_cost,
            ct_val=ct_val,
            risk_pct=risk_pct,
            initial_equity=initial_equity,
            start=window["test_start"],
            end=window["test_end"],
            prepared=prepared,
        )
        oos_trades.extend(oos["all_trades"])
        fold_results.append(
            {
                "index": window_index,
                "window": window,
                "skipped": False,
                "selected": chosen.to_dict(),
                "train": train_scores[window_index][best_index],
                "test": oos["metrics"],
            }
        )

    total_r = sum(trade["r"] for trade in oos_trades)
    wins = [trade for trade in oos_trades if trade["pnl"] > 0]
    gross_win = sum(trade["pnl"] for trade in wins)
    gross_loss = abs(sum(trade["pnl"] for trade in oos_trades if trade["pnl"] <= 0))
    aggregate = {
        "trades": len(oos_trades),
        "expectancy_r": round(total_r / len(oos_trades), 4) if oos_trades else 0.0,
        "total_r": round(total_r, 4),
        "win_rate": round(len(wins) / len(oos_trades) * 100, 2) if oos_trades else 0.0,
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else None,
        "avg_bars": (
            round(sum(trade["bars"] for trade in oos_trades) / len(oos_trades), 2)
            if oos_trades
            else 0.0
        ),
    }
    return {
        "strategy": strategy,
        "folds": len(windows),
        "skipped_folds": skipped,
        "purge_bars": purge_bars,
        "embargo_bars": embargo_bars,
        "aggregate": aggregate,
        "detail": fold_results,
    }
