"""技术指标：EMA 与 ATR，纯 Python 实现（不引入 pandas/numpy）。"""

from __future__ import annotations


def ema_series(values: list[float], period: int) -> list[float | None]:
    """指数移动平均，前 period-1 个位置为 None。"""
    if period <= 0 or len(values) < period:
        return [None] * len(values)
    multiplier = 2 / (period + 1)
    result: list[float | None] = [None] * len(values)
    previous = sum(values[:period]) / period
    result[period - 1] = previous
    for index in range(period, len(values)):
        previous = values[index] * multiplier + previous * (1 - multiplier)
        result[index] = previous
    return result


def cross_signal(
    fast: list[float | None], slow: list[float | None]
) -> str | None:
    """用最后一组快慢线判断金叉/死叉，返回 long / short / None。"""
    if len(fast) < 2 or len(slow) < 2:
        return None
    f_now, f_prev = fast[-1], fast[-2]
    s_now, s_prev = slow[-1], slow[-2]
    if None in (f_now, f_prev, s_now, s_prev):
        return None
    if f_prev <= s_prev and f_now > s_now:
        return "long"
    if f_prev >= s_prev and f_now < s_now:
        return "short"
    return None


def atr(rows: list[list[str]], period: int = 14) -> float | None:
    """平均真实波幅；rows 为 OKX K 线（升序），需要 period+1 根。"""
    if period <= 0 or len(rows) < period + 1:
        return None
    true_ranges: list[float] = []
    for index in range(1, len(rows)):
        high = float(rows[index][2])
        low = float(rows[index][3])
        prev_close = float(rows[index - 1][4])
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if len(true_ranges) < period:
        return None
    return sum(true_ranges[-period:]) / period
