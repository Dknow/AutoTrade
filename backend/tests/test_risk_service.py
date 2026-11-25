"""风控单测：单笔限额、名义价值上限、回撤熔断与复位。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.core.config import Settings
from app.services.risk_service import RiskLimitExceeded, RiskService


class FakeAccount:
    def __init__(self, equity: str) -> None:
        self.account: dict[str, Any] = {"totalEq": equity}


def _service(equity: str = "1000", **overrides: Any) -> RiskService:
    settings = Settings(
        risk_max_size=overrides.get("max_size", "1"),
        risk_max_notional=overrides.get("max_notional", 100),
        risk_max_drawdown_pct=overrides.get("max_drawdown", 10),
    )
    return RiskService(settings, FakeAccount(equity))  # type: ignore[arg-type]


def test_check_open_passes_within_limits() -> None:
    service = _service()
    service.check_open(size=Decimal("1"), notional=Decimal("100"))


def test_check_open_rejects_size_over_limit() -> None:
    service = _service()
    with pytest.raises(RiskLimitExceeded, match="超过单笔上限"):
        service.check_open(size=Decimal("2"), notional=Decimal("50"))


def test_check_open_rejects_notional_over_limit() -> None:
    service = _service()
    with pytest.raises(RiskLimitExceeded, match="名义价值"):
        service.check_open(size=Decimal("1"), notional=Decimal("150"))


def test_drawdown_trip_blocks_new_positions() -> None:
    account = FakeAccount("1000")
    settings = Settings(risk_max_size="1", risk_max_notional=100, risk_max_drawdown_pct=10)
    service = RiskService(settings, account)  # type: ignore[arg-type]

    service.refresh_equity()
    assert service.tripped is False

    account.account["totalEq"] = "850"
    service.refresh_equity()

    assert service.tripped is True
    assert "回撤" in service.trip_reason
    with pytest.raises(RiskLimitExceeded, match="熔断"):
        service.check_open(size=Decimal("0.1"), notional=Decimal("10"))


def test_reset_clears_tripped_state() -> None:
    service = _service()
    service.trip("测试熔断")
    assert service.snapshot()["tripped"] is True

    service.reset()
    snapshot = service.snapshot()
    assert snapshot["tripped"] is False
    assert snapshot["trip_reason"] == ""
    service.check_open(size=Decimal("0.1"), notional=Decimal("10"))


def test_snapshot_reports_drawdown_percentage() -> None:
    account = FakeAccount("1000")
    service = RiskService(
        Settings(risk_max_drawdown_pct=50), account  # type: ignore[arg-type]
    )
    service.refresh_equity()
    account.account["totalEq"] = "900"
    snapshot = service.snapshot()
    assert snapshot["drawdown_pct"] == "10"
    assert snapshot["peak_equity"] == "1000"
