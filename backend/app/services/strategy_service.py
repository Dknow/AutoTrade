"""双均线策略引擎：EMA 金叉/死叉 + ATR 止损。

运行约束：
- 只使用已收盘 K 线，避免用未完成 K 线产生假信号；
- 一次只持一边仓位，出现反向交叉时先平旧仓再开新仓；
- 开仓前必须通过风控校验，被拒绝时记录原因且不重复重试；
- 冷却期内不再开仓，抑制震荡行情里的来回打脸。
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.core.config import Settings
from app.services.account_service import AccountService
from app.services.indicators import atr, cross_signal, ema_series
from app.services.instrument_service import InstrumentService, plain
from app.services.market_service import MarketService
from app.services.risk_service import RiskLimitExceeded, RiskService
from app.services.trade_service import TradeService

logger = logging.getLogger(__name__)

MAX_RECORDS = 50
BAR_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1H": 3600, "4H": 14400}


@dataclass
class StrategyConfig:
    fast: int = 9
    slow: int = 21
    atr_period: int = 14
    atr_multiple: float = 1.5
    size: Decimal = Decimal("0.01")
    lever: int = 10
    td_mode: str = "cross"
    cooldown_bars: int = 3

    @classmethod
    def from_settings(cls, settings: Settings) -> "StrategyConfig":
        return cls(
            fast=settings.strategy_fast,
            slow=settings.strategy_slow,
            atr_period=settings.strategy_atr_period,
            atr_multiple=settings.strategy_atr_multiple,
            size=Decimal(settings.strategy_size),
            lever=settings.strategy_lever,
            cooldown_bars=settings.strategy_cooldown_bars,
        )

    def validate(self) -> None:
        if self.fast <= 0 or self.slow <= 0:
            raise ValueError("均线周期必须大于 0")
        if self.fast >= self.slow:
            raise ValueError("快线周期必须小于慢线周期")
        if self.atr_period <= 0:
            raise ValueError("ATR 周期必须大于 0")
        if self.atr_multiple <= 0:
            raise ValueError("ATR 倍数必须大于 0")
        if self.size <= 0:
            raise ValueError("策略下单张数必须大于 0")
        if self.lever <= 0:
            raise ValueError("杠杆必须大于 0")
        if self.cooldown_bars < 0:
            raise ValueError("冷却 K 线数不能为负")

    def to_dict(self) -> dict[str, Any]:
        return {
            "fast": self.fast,
            "slow": self.slow,
            "atr_period": self.atr_period,
            "atr_multiple": self.atr_multiple,
            "size": plain(self.size),
            "lever": self.lever,
            "td_mode": self.td_mode,
            "cooldown_bars": self.cooldown_bars,
        }


@dataclass
class StrategyRecord:
    at: str
    event: str
    detail: str
    ok: bool = True


@dataclass
class StrategyService:
    settings: Settings
    market: MarketService
    trade: TradeService
    account: AccountService
    risk: RiskService
    instruments: InstrumentService
    config: StrategyConfig = field(default_factory=StrategyConfig)

    running: bool = False
    last_signal: str | None = None
    last_evaluated_ts: int = 0
    last_action_ms: int = 0
    last_error: str = ""
    records: deque[StrategyRecord] = field(default_factory=lambda: deque(maxlen=MAX_RECORDS))

    def __post_init__(self) -> None:
        self.market.add_candle_listener(self.on_candle)

    # ------------------------------------------------------------------ 控制
    async def start(self) -> dict[str, Any]:
        self.config.validate()
        self.running = True
        self._record("start", f"策略启动 | {self.config.to_dict()}")
        await self.on_candle(self.market.inst_id)
        return self.snapshot()

    async def stop(self) -> dict[str, Any]:
        if self.running:
            self._record("stop", "策略已停止")
        self.running = False
        return self.snapshot()

    def update_config(self, config: StrategyConfig) -> dict[str, Any]:
        if self.running:
            raise ValueError("策略运行中，请先停止再修改参数")
        config.validate()
        self.config = config
        self.last_signal = None
        self.last_evaluated_ts = 0
        self._record("config", f"参数已更新 | {config.to_dict()}")
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "inst_id": self.market.inst_id,
            "bar": self.market.bar,
            "config": self.config.to_dict(),
            "last_signal": self.last_signal,
            "last_evaluated_ts": self.last_evaluated_ts,
            "last_error": self.last_error,
            "records": [
                {"at": item.at, "event": item.event, "detail": item.detail, "ok": item.ok}
                for item in reversed(self.records)
            ],
        }

    # ------------------------------------------------------------------ 主流程
    async def on_candle(self, inst_id: str) -> None:
        if not self.running or inst_id != self.market.inst_id:
            return
        try:
            await self._evaluate()
        except Exception as exc:  # noqa: BLE001 - 单次评估失败不应终止策略
            self.last_error = str(exc)
            logger.exception("策略评估异常")
            self._record("error", f"评估异常: {exc}", ok=False)

    async def _evaluate(self) -> None:
        needed = max(self.config.slow, self.config.atr_period) + 3
        rows = list(reversed(self.market.candles(limit=needed)))  # 转为升序
        closed = rows[:-1]  # 丢弃未收盘的最后一根
        if len(closed) < needed - 1:
            return

        latest_ts = int(closed[-1][0])
        if latest_ts == self.last_evaluated_ts:
            return
        self.last_evaluated_ts = latest_ts

        closes = [float(row[4]) for row in closed]
        fast_line = ema_series(closes, self.config.fast)
        slow_line = ema_series(closes, self.config.slow)
        signal = cross_signal(fast_line, slow_line)
        if signal is None or signal == self.last_signal:
            return

        bar_seconds = BAR_SECONDS.get(self.market.bar, 60)
        if self.last_action_ms and latest_ts - self.last_action_ms < self.config.cooldown_bars * bar_seconds * 1000:
            remaining = self.config.cooldown_bars - (latest_ts - self.last_action_ms) // (bar_seconds * 1000)
            self._record("skip", f"{signal} 信号处于冷却期（剩余 {remaining} 根）", ok=False)
            self.last_signal = signal
            return

        atr_value = atr(closed, self.config.atr_period)
        if atr_value is None:
            self._record("skip", "ATR 数据不足，跳过", ok=False)
            return

        price = self._reference_price(closed)
        if price is None or price <= 0:
            self._record("skip", "缺少有效价格，跳过", ok=False)
            return

        self.last_signal = signal
        await self._execute(signal, price, atr_value)

    async def _execute(self, signal: str, price: float, atr_value: float) -> None:
        inst_id = self.market.inst_id
        spec = self.instruments.get(inst_id)
        size = self.config.size
        notional = spec.ct_val * size * Decimal(str(price))

        try:
            self.risk.check_open(size=size, notional=notional)
        except RiskLimitExceeded as exc:
            self.last_error = str(exc)
            self._record("risk", f"风控拦截: {exc}", ok=False)
            return

        # 先平掉反向持仓，再开新仓
        for position in self.account.active_positions():
            if position.get("instId") != inst_id:
                continue
            holding_side = position.get("posSide")
            if holding_side == ("short" if signal == "long" else "long"):
                try:
                    await self.trade.close_position(inst_id=inst_id, pos_side=str(holding_side), td_mode=self.config.td_mode)
                    self._record("close", f"反向信号，已平掉 {holding_side} 仓")
                except Exception as exc:  # noqa: BLE001
                    self._record("close", f"平仓失败: {exc}", ok=False)
                    self.last_error = str(exc)
                    return

        if signal == "long":
            stop_price = price - atr_value * self.config.atr_multiple
        else:
            stop_price = price + atr_value * self.config.atr_multiple
        if stop_price <= 0:
            self._record("skip", f"止损价计算异常: {stop_price:.4f}", ok=False)
            return

        try:
            result = await self.trade.open_position(
                inst_id=inst_id,
                pos_side=signal,
                size=size,
                ord_type="market",
                td_mode=self.config.td_mode,
                lever=self.config.lever,
                sl_trigger_px=Decimal(str(stop_price)),
            )
            self.last_action_ms = self.last_evaluated_ts
            self.last_error = ""
            self._record(
                "open",
                f"{'开多' if signal == 'long' else '开空'} {plain(size)} 张 | 参考价 {price:.2f} | 止损 {stop_price:.2f} | ordId={result['order'].get('ordId')}",
            )
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            self._record("open", f"开仓失败: {exc}", ok=False)

    # ------------------------------------------------------------------ 工具
    def _reference_price(self, closed: list[list[str]]) -> float | None:
        last = self.market.ticker.get("last")
        if last not in (None, ""):
            try:
                return float(last)
            except (TypeError, ValueError):
                pass
        return float(closed[-1][4]) if closed else None

    def _record(self, event: str, detail: str, *, ok: bool = True) -> None:
        record = StrategyRecord(at=datetime.now(timezone.utc).isoformat(timespec="seconds"), event=event, detail=detail, ok=ok)
        self.records.append(record)
        log = logger.info if ok else logger.warning
        log("策略[%s] %s", event, detail)
