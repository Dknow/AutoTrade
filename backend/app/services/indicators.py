"""技术指标：EMA / SMA / 标准差 / 布林 / 唐奇安 / ATR，纯 Python 实现。

约定：所有序列型指标与输入等长，未预热的位置为 None；
指标第 i 个值只依赖第 i 根及之前的数据，可直接用于「第 i 根收盘出信号、第 i+1 根开盘成交」的回测。
"""

from __future__ import annotations


def _clean(value: float) -> float:
    return 0.0 if abs(value) < 1e-12 else value


def sma_series(values: list[float], period: int) -> list[float | None]:
    """简单移动平均，前 period-1 个位置为 None。"""
    result: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return result
    window = sum(values[:period])
    result[period - 1] = window / period
    for index in range(period, len(values)):
        window += values[index] - values[index - period]
        result[index] = window / period
    return result


def rolling_std_series(values: list[float], period: int) -> list[float | None]:
    """滚动总体标准差（ddof=0），用于布林带。"""
    result: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return result
    window = values[:period]
    for index in range(period - 1, len(values)):
        if index >= period:
            window = values[index - period + 1 : index + 1]
        mean = sum(window) / period
        variance = sum((value - mean) ** 2 for value in window) / period
        result[index] = variance**0.5
    return result


def bollinger_series(
    values: list[float], period: int = 20, k: float = 2.0
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """布林带，返回 (中轨, 上轨, 下轨)。"""
    mid = sma_series(values, period)
    std = rolling_std_series(values, period)
    upper: list[float | None] = [None] * len(values)
    lower: list[float | None] = [None] * len(values)
    for index, (m, s) in enumerate(zip(mid, std)):
        if m is None or s is None:
            continue
        upper[index] = m + k * s
        lower[index] = m - k * s
    return mid, upper, lower


def donchian_series(
    rows: list[list[str]], period: int
) -> tuple[list[float | None], list[float | None]]:
    """唐奇安通道，返回 (上轨, 下轨)。

    第 i 根使用**之前** period 根（不含当根）的最高/最低价，
    因此 close[i] 突破 upper[i] 不会用到当根自身的高点。
    """
    upper: list[float | None] = [None] * len(rows)
    lower: list[float | None] = [None] * len(rows)
    if period <= 0:
        return upper, lower
    for index in range(period, len(rows)):
        window = rows[index - period : index]
        upper[index] = max(float(row[2]) for row in window)
        lower[index] = min(float(row[3]) for row in window)
    return upper, lower


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


def atr_series(rows: list[list[str]], period: int) -> list[float | None]:
    """逐根 ATR，口径与 atr() 一致：第 i 根取截至当根的 period 个真实波幅均值。

    与 atr() 的差别只是返回完整序列且未预热为 None，方便回测逐根取用。
    """
    result: list[float | None] = [None] * len(rows)
    if period <= 0 or len(rows) < period + 1:
        return result
    for index in range(period, len(rows)):
        total = 0.0
        for offset in range(index - period + 1, index + 1):
            high = float(rows[offset][2])
            low = float(rows[offset][3])
            prev_close = float(rows[offset - 1][4])
            total += max(high - low, abs(high - prev_close), abs(low - prev_close))
        result[index] = total / period
    return result
