"""回测引擎：用历史 K 线复现「双均线 + ATR 止损」策略。

关键约定（避免未来函数）：
- 第 i 根 K 线收盘后计算信号，第 i+1 根开盘价成交；
- 同一根 K 线内止损优先于开仓，止损后不在同根反手；
- 移动止损的回调价差在开仓时用 ATR×倍数 确定，之后只随最优价单向移动；
- 手续费按名义价值双边收取，滑点按成交价百分比、不利方向单边计算。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.exchange.okx_client import OkxClient
from app.services.indicators import atr_series, ema_series

logger = logging.getLogger(__name__)

MAX_HISTORY_LIMIT = 1440
PAGE_SIZE = 300
MAX_CURVE_POINTS = 400


@dataclass(frozen=True)
class BacktestParams:
    inst_id: str = "BTC-USDT-SWAP"
    bar: str = "1m"
    limit: int = 500
    fast: int = 9
    slow: int = 21
    atr_period: int = 14
    atr_multiple: float = 1.5
    size: Decimal = Decimal("0.01")
    cooldown_bars: int = 3
    trailing: bool = True
    fee_rate: float = 0.0005
    slippage_rate: float = 0.0
    initial_equity: float = 1000.0

    def validate(self) -> None:
        if not self.inst_id:
            raise ValueError("回测标的不为空")
        if self.fast <= 0 or self.slow <= 0:
            raise ValueError("均线周期必须大于 0")
        if self.fast >= self.slow:
            raise ValueError("快线周期必须小于慢线周期")
        if self.atr_period <= 0:
            raise ValueError("ATR 周期必须大于 0")
        if self.atr_multiple <= 0:
            raise ValueError("ATR 倍数必须大于 0")
        if self.size <= 0:
            raise ValueError("回测张数必须大于 0")
        if self.cooldown_bars < 0:
            raise ValueError("冷却 K 线数不能为负")
        if self.fee_rate < 0 or self.slippage_rate < 0:
            raise ValueError("手续费率与滑点不能为负")
        if self.initial_equity <= 0:
            raise ValueError("初始权益必须大于 0")
        if not 100 <= self.limit <= MAX_HISTORY_LIMIT:
            raise ValueError(f"K 线数量需在 100 ~ {MAX_HISTORY_LIMIT} 之间")

    def to_dict(self) -> dict[str, Any]:
        return {
            "inst_id": self.inst_id,
            "bar": self.bar,
            "limit": self.limit,
            "fast": self.fast,
            "slow": self.slow,
            "atr_period": self.atr_period,
            "atr_multiple": self.atr_multiple,
            "size": format(self.size.normalize(), "f"),
            "cooldown_bars": self.cooldown_bars,
            "trailing": self.trailing,
            "fee_rate": self.fee_rate,
            "slippage_rate": self.slippage_rate,
            "initial_equity": self.initial_equity,
        }


async def fetch_history_candles(
    client: OkxClient, inst_id: str, bar: str, limit: int
) -> list[list[str]]:
    """分页拉取历史 K 线（OKX 单次最多 300 根），返回按时间升序、去重后的结果。"""
    rows: dict[int, list[str]] = {}
    cursor: str | None = None
    remaining = min(limit, MAX_HISTORY_LIMIT)
    while remaining > 0:
        # OKX 语义：after = 返回比该时间戳更早的数据（翻页向历史方向）
        page = await client.get_candles(
            inst_id, bar=bar, limit=min(PAGE_SIZE, remaining), after=cursor
        )
        if not page:
            break
        before_size = len(rows)
        for row in page:
            if isinstance(row, list) and row:
                rows[int(row[0])] = row
        # 没有新增时间戳说明翻页参数没有生效，直接停止避免死循环
        if len(rows) == before_size:
            break
        remaining -= len(page)
        earliest = min(int(row[0]) for row in page if row)
        cursor = str(earliest)
        if len(page) < PAGE_SIZE:
            break
    return [rows[ts] for ts in sorted(rows)]


def _signal_at(
    fast_line: list[float | None], slow_line: list[float | None], index: int
) -> str | None:
    """与 indicators.cross_signal 完全一致的穿越判断。"""
    if index < 1:
        return None
    f_now, f_prev = fast_line[index], fast_line[index - 1]
    s_now, s_prev = slow_line[index], slow_line[index - 1]
    if None in (f_now, f_prev, s_now, s_prev):
        return None
    if f_prev <= s_prev and f_now > s_now:
        return "long"
    if f_prev >= s_prev and f_now < s_now:
        return "short"
    return None


def _sample_curve(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(points) <= MAX_CURVE_POINTS:
        return points
    step = len(points) / MAX_CURVE_POINTS
    sampled = [points[int(index * step)] for index in range(MAX_CURVE_POINTS)]
    sampled[-1] = points[-1]
    return sampled


def _max_drawdown_pct(curve: list[dict[str, Any]]) -> float:
    peak = 0.0
    worst = 0.0
    for point in curve:
        value = float(point["equity"])
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak * 100)
    return round(worst, 4)


def run_backtest(rows: list[list[str]], *, ct_val: float, params: BacktestParams) -> dict[str, Any]:
    """执行回测，rows 为 OKX K 线（任意顺序，内部排序为升序）。"""
    params.validate()
    ordered = sorted(rows, key=lambda row: int(row[0]))
    if len(ordered) < max(params.slow, params.atr_period + 1) + 2:
        raise ValueError("K 线数量不足以计算指标，请增加回测根数")

    closes = [float(row[4]) for row in ordered]
    fast_line = ema_series(closes, params.fast)
    slow_line = ema_series(closes, params.slow)
    atr_line = atr_series(ordered, params.atr_period)
    warmup = max(params.slow, params.atr_period + 1)

    size = float(params.size)
    slip = params.slippage_rate
    fee_rate = params.fee_rate
    equity = float(params.initial_equity)
    curve: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    fees_total = 0.0

    position: dict[str, Any] | None = None
    pending: str | None = None
    last_open_index = -10**9

    def close_position(exit_px: float, ts: int, reason: str, index: int) -> None:
        nonlocal equity, position, fees_total
        assert position is not None
        direction = 1 if position["side"] == "long" else -1
        gross = (exit_px - position["entry_px"]) * direction * size * ct_val
        fee_close = exit_px * size * ct_val * fee_rate
        equity += gross - fee_close
        fees_total += fee_close
        pnl = gross - position["fee_open"] - fee_close
        trades.append(
            {
                "side": position["side"],
                "entry_ts": position["entry_ts"],
                "entry_px": round(position["entry_px"], 8),
                "exit_ts": ts,
                "exit_px": round(exit_px, 8),
                "size": format(params.size.normalize(), "f"),
                "pnl": round(pnl, 6),
                "fee": round(position["fee_open"] + fee_close, 6),
                "reason": reason,
                "bars": index - position["entry_index"],
            }
        )
        position = None

    for index, row in enumerate(ordered):
        ts = int(row[0])
        open_px = float(row[1])
        high = float(row[2])
        low = float(row[3])
        close = float(row[4])

        # 1) 本根开盘执行上一根收盘产生的信号
        if pending is not None:
            if position is not None:
                exit_px = open_px * (1 - slip) if position["side"] == "long" else open_px * (1 + slip)
                close_position(exit_px, ts, "signal", index)
            if position is None and index - last_open_index >= params.cooldown_bars:
                atr_value = atr_line[index - 1]
                if atr_value and atr_value > 0:
                    spread = atr_value * params.atr_multiple
                    entry_px = open_px * (1 + slip) if pending == "long" else open_px * (1 - slip)
                    fee_open = entry_px * size * ct_val * fee_rate
                    equity -= fee_open
                    fees_total += fee_open
                    position = {
                        "side": pending,
                        "entry_px": entry_px,
                        "entry_ts": ts,
                        "entry_index": index,
                        "spread": spread,
                        "stop": entry_px - spread if pending == "long" else entry_px + spread,
                        "peak": entry_px,
                        "trough": entry_px,
                        "fee_open": fee_open,
                    }
                    last_open_index = index
            pending = None

        # 2) 持仓中：更新移动止损并检查触发（用本根 high/low，跳空则按开盘价成交）
        if position is not None:
            if params.trailing:
                if position["side"] == "long":
                    position["peak"] = max(position["peak"], high)
                    position["stop"] = max(position["stop"], position["peak"] - position["spread"])
                else:
                    position["trough"] = min(position["trough"], low)
                    position["stop"] = min(position["stop"], position["trough"] + position["spread"])

            if position["side"] == "long" and low <= position["stop"]:
                exit_px = min(open_px, position["stop"]) * (1 - slip)
                close_position(exit_px, ts, "stop", index)
            elif position["side"] == "short" and high >= position["stop"]:
                exit_px = max(open_px, position["stop"]) * (1 + slip)
                close_position(exit_px, ts, "stop", index)

        # 3) 收盘后基于已收盘数据生成信号，留到下一根开盘执行
        #    持仓时只接受反向信号（先平后反手），同向信号忽略
        if index >= warmup:
            signal = _signal_at(fast_line, slow_line, index)
            if signal is not None and (position is None or position["side"] != signal):
                pending = signal

        # 4) 记录收盘时的浮动权益
        unrealized = 0.0
        if position is not None:
            direction = 1 if position["side"] == "long" else -1
            unrealized = (close - position["entry_px"]) * direction * size * ct_val
        curve.append({"ts": ts, "equity": round(equity + unrealized, 6)})

    # 剩余持仓按最后收盘价强平
    if position is not None:
        last_close = float(ordered[-1][4])
        exit_px = last_close * (1 - slip) if position["side"] == "long" else last_close * (1 + slip)
        close_position(exit_px, int(ordered[-1][0]), "end", len(ordered) - 1)
        curve[-1] = {"ts": curve[-1]["ts"], "equity": round(equity, 6)}

    wins = [trade for trade in trades if trade["pnl"] > 0]
    losses = [trade for trade in trades if trade["pnl"] <= 0]
    gross_win = sum(trade["pnl"] for trade in wins)
    gross_loss = abs(sum(trade["pnl"] for trade in losses))
    net_pnl = equity - float(params.initial_equity)

    return {
        "inst_id": params.inst_id,
        "bar": params.bar,
        "params": params.to_dict(),
        "candles": len(ordered),
        "range": {"start": int(ordered[0][0]), "end": int(ordered[-1][0])},
        "summary": {
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
            "net_pnl": round(net_pnl, 6),
            "return_pct": round(net_pnl / float(params.initial_equity) * 100, 4),
            "final_equity": round(equity, 6),
            "max_drawdown_pct": _max_drawdown_pct(curve),
            "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else None,
            "avg_win": round(gross_win / len(wins), 6) if wins else 0.0,
            "avg_loss": round(-gross_loss / len(losses), 6) if losses else 0.0,
            "fees": round(fees_total, 6),
        },
        "equity": _sample_curve(curve),
        "trades": trades,
    }
