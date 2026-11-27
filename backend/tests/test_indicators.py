"""指标计算单测：EMA、交叉判断、ATR。"""

from __future__ import annotations

import pytest

from app.services.indicators import (
    atr,
    atr_series,
    bollinger_series,
    cross_signal,
    donchian_series,
    ema_series,
    rolling_std_series,
    sma_series,
)


def _row(ts: int, high: float, low: float, close: float) -> list[str]:
    return [str(ts), str(close), str(high), str(low), str(close), "0", "0", "0", "1"]


def test_ema_constant_series_stays_constant() -> None:
    values = [10.0] * 10
    result = ema_series(values, 3)
    assert result[0] is None
    assert result[1] is None
    assert result[2] == pytest.approx(10.0)
    assert result[-1] == pytest.approx(10.0)


def test_ema_reacts_faster_with_shorter_period() -> None:
    values = [10.0] * 5 + [20.0] * 5
    fast = ema_series(values, 2)
    slow = ema_series(values, 4)
    assert fast[-1] > slow[-1]


def test_ema_returns_none_when_too_short() -> None:
    assert ema_series([1.0, 2.0], 5) == [None, None]


def test_cross_signal_detects_golden_and_death_cross() -> None:
    fast_up = [1.0, 1.0, 3.0]
    slow_up = [2.0, 2.0, 2.0]
    assert cross_signal(fast_up, slow_up) == "long"

    fast_down = [3.0, 3.0, 1.0]
    slow_down = [2.0, 2.0, 2.0]
    assert cross_signal(fast_down, slow_down) == "short"


def test_cross_signal_returns_none_without_cross() -> None:
    assert cross_signal([3.0, 4.0], [1.0, 1.0]) is None
    assert cross_signal([None, 3.0], [1.0, 1.0]) is None


def test_atr_computes_average_true_range() -> None:
    rows = [
        _row(1, 10, 8, 9),
        _row(2, 11, 9, 10),
        _row(3, 12, 10, 11),
    ]
    # TR: (11-9, |11-9|, |9-9|)=2; (12-10, |12-10|, |10-10|)=2
    assert atr(rows, 2) == pytest.approx(2.0)


def test_atr_returns_none_when_insufficient() -> None:
    rows = [_row(1, 10, 8, 9), _row(2, 11, 9, 10)]
    assert atr(rows, 5) is None


def test_sma_series_matches_manual_average() -> None:
    result = sma_series([1.0, 2.0, 3.0, 4.0], 2)
    assert result[0] is None
    assert result[1] == pytest.approx(1.5)
    assert result[2] == pytest.approx(2.5)
    assert result[3] == pytest.approx(3.5)


def test_rolling_std_matches_population_std() -> None:
    # 窗口 [2,4,4,4,5,5,7,9] 的总体标准差为 2
    values = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
    result = rolling_std_series(values, 8)
    assert result[7] == pytest.approx(2.0)
    assert result[6] is None


def test_bollinger_bands_use_mid_and_std() -> None:
    values = [10.0, 10.0, 10.0, 10.0, 10.0]
    mid, upper, lower = bollinger_series(values, 3, 2.0)
    assert mid[-1] == pytest.approx(10.0)
    assert upper[-1] == pytest.approx(10.0)
    assert lower[-1] == pytest.approx(10.0)


def test_donchian_uses_prior_bars_only() -> None:
    rows = [
        _row(1, 10, 8, 9),
        _row(2, 12, 9, 10),
        _row(3, 11, 7, 9),
        _row(4, 20, 15, 18),  # 当根高点 20 不应出现在自己的上轨里
    ]
    upper, lower = donchian_series(rows, 3)
    assert upper[2] is None
    assert upper[3] == pytest.approx(12.0)
    assert lower[3] == pytest.approx(7.0)


def test_atr_series_matches_atr_scalar() -> None:
    rows = [_row(index, 10 + index, 8 + index, 9 + index) for index in range(1, 6)]
    series = atr_series(rows, 2)
    assert series[0] is None
    assert series[1] is None
    assert series[2] == pytest.approx(atr(rows[:3], 2))
    assert series[-1] == pytest.approx(atr(rows, 2))
