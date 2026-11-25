"""风控服务：下单前限额校验 + 权益回撤熔断 + Kill Switch 状态。

职责边界：
- 只做判断与状态维护，不直接下单（下单由策略或路由调用交易服务完成）；
- 数据来源全部取本地缓存（账户私有频道推送），避免每次校验都打 OKX 接口；
- 熔断后禁止开仓，但允许平仓，防止熔断反而锁死风险敞口。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.core.config import Settings
from app.services.account_service import AccountService

logger = logging.getLogger(__name__)


class RiskLimitExceeded(RuntimeError):
    """风控拦截：本次开仓不满足限额或已熔断。"""


class RiskService:
    def __init__(self, settings: Settings, account_service: AccountService) -> None:
        self.settings = settings
        self.account_service = account_service

        self.max_size = Decimal(settings.risk_max_size)
        self.max_notional = Decimal(str(settings.risk_max_notional))
        self.max_drawdown_pct = Decimal(str(settings.risk_max_drawdown_pct))

        self.peak_equity: Decimal | None = None
        self.last_equity: Decimal | None = None
        self.tripped = False
        self.trip_reason = ""
        self.tripped_at: str = ""

    # ------------------------------------------------------------------ 状态
    def refresh_equity(self) -> None:
        """从账户缓存读取总权益并更新峰值与回撤。"""
        equity = self._current_equity()
        if equity is None:
            return
        self.last_equity = equity
        if self.peak_equity is None or equity > self.peak_equity:
            self.peak_equity = equity
        if not self.tripped and self.drawdown_pct() >= self.max_drawdown_pct:
            self.trip(f"权益回撤 {self.drawdown_pct():.2f}% 达到阈值 {self.max_drawdown_pct}%")

    def drawdown_pct(self) -> Decimal:
        if self.peak_equity is None or self.last_equity is None or self.peak_equity <= 0:
            return Decimal(0)
        return (self.peak_equity - self.last_equity) / self.peak_equity * 100

    def trip(self, reason: str) -> None:
        if self.tripped:
            return
        self.tripped = True
        self.trip_reason = reason
        self.tripped_at = datetime.now(timezone.utc).isoformat()
        logger.warning("风控熔断触发: %s", reason)

    def reset(self) -> None:
        self.tripped = False
        self.trip_reason = ""
        self.tripped_at = ""
        self.peak_equity = None
        self.last_equity = None
        logger.info("风控熔断已人工复位")

    def snapshot(self) -> dict[str, Any]:
        self.refresh_equity()
        return {
            "tripped": self.tripped,
            "trip_reason": self.trip_reason,
            "tripped_at": self.tripped_at,
            "peak_equity": _plain(self.peak_equity),
            "equity": _plain(self.last_equity),
            "drawdown_pct": _plain(self.drawdown_pct()),
            "max_drawdown_pct": str(self.max_drawdown_pct),
            "max_size": str(self.max_size),
            "max_notional": str(self.max_notional),
        }

    # ------------------------------------------------------------------ 校验
    def check_open(self, *, size: Decimal, notional: Decimal) -> None:
        """开仓前校验，不通过直接抛 RiskLimitExceeded。"""
        self.refresh_equity()
        if self.tripped:
            raise RiskLimitExceeded(f"风控已熔断：{self.trip_reason}")
        if size > self.max_size:
            raise RiskLimitExceeded(f"下单张数 {size} 超过单笔上限 {self.max_size}")
        if self.max_notional > 0 and notional > self.max_notional:
            raise RiskLimitExceeded(
                f"名义价值 {notional:.2f} USDT 超过单笔上限 {self.max_notional} USDT"
            )

    # ------------------------------------------------------------------ 内部
    def _current_equity(self) -> Decimal | None:
        account = self.account_service.account or {}
        raw = account.get("totalEq") or account.get("eq")
        if raw in (None, ""):
            return None
        try:
            return Decimal(str(raw))
        except ArithmeticError:  # noqa: BLE001 - 字段异常时跳过，不阻塞交易
            return None


def _plain(value: Decimal | None) -> str | None:
    return format(value.normalize(), "f") if value is not None else None
