"""量化回测接口：从本地 K 线库读取数据，返回收益/风险指标与样本外验证。"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.services.quant_backtest import (
    DEFAULT_FEE_RATE,
    DEFAULT_SLIPPAGE_RATE,
    CostModel,
    QuantParams,
    bars_from_rows,
    cost_sensitivity,
    purged_walk_forward,
    run_quant_backtest,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["quant"])


class CostPayload(BaseModel):
    fee_rate: float = Field(DEFAULT_FEE_RATE, ge=0)
    slippage_rate: float = Field(DEFAULT_SLIPPAGE_RATE, ge=0)
    funding_rate_per_bar: float = Field(0.0, ge=0)


class WalkForwardPayload(BaseModel):
    enabled: bool = True
    folds: int = Field(3, ge=1, le=10)
    train_bars: int | None = Field(default=None, ge=50)
    test_bars: int | None = Field(default=None, ge=20)
    purge_bars: int = Field(24, ge=0)
    embargo_bars: int = Field(24, ge=0)
    min_train_trades: int = Field(5, ge=1)


class QuantBacktestRequest(BaseModel):
    inst_id: str = Field("BTC-USDT-SWAP", min_length=1)
    bar: str = "4H"
    strategy: Literal["bollinger_mr", "donchian"] = "bollinger_mr"
    params: dict[str, Any] = Field(default_factory=dict)
    risk_pct: float = Field(0.01, gt=0, le=0.5)
    initial_equity: float = Field(10_000.0, gt=0)
    cost: CostPayload = Field(default_factory=CostPayload)
    walk_forward: WalkForwardPayload = Field(default_factory=WalkForwardPayload)


def _resolve_ct_val(request: Request, inst_id: str) -> float:
    """合约面值只影响美元盈亏，不影响 R 指标；拿不到就退回 1.0。"""
    try:
        spec = request.app.state.instruments.get(inst_id)
        value = float(spec.ct_val)
        if value > 0:
            return value
    except Exception as exc:  # noqa: BLE001 - 规格未加载时回测仍应可用
        logger.debug("回测取合约面值失败，按 1.0 处理: %s", exc)
    return 1.0


@router.post("/quant/backtest")
def quant_backtest(request: Request, payload: QuantBacktestRequest) -> dict:
    inst_id = payload.inst_id.strip().upper()
    bar = payload.bar.strip()
    store = request.app.state.candle_store
    rows = store.load(inst_id, bar)
    if not rows:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "NO_DATA",
                "message": f"本地没有 {inst_id} {bar} 的 K 线，请先调用 POST /data/sync",
            },
        )

    bars = bars_from_rows(rows)
    try:
        params = QuantParams(strategy=payload.strategy, **payload.params)
        params.validate()
    except TypeError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "VALIDATION_ERROR", "message": f"未知参数: {exc}"},
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "VALIDATION_ERROR", "message": str(exc)}
        ) from exc

    cost = CostModel(
        fee_rate=payload.cost.fee_rate,
        slippage_rate=payload.cost.slippage_rate,
        funding_rate_per_bar=payload.cost.funding_rate_per_bar,
    )
    ct_val = _resolve_ct_val(request, inst_id)
    common = dict(
        cost=cost,
        ct_val=ct_val,
        risk_pct=payload.risk_pct,
        initial_equity=payload.initial_equity,
    )
    try:
        full = run_quant_backtest(bars, params, **common)
        sensitivity = cost_sensitivity(bars, params, **common)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "BACKTEST_FAILED", "message": str(exc)}
        ) from exc

    walk_forward: dict[str, Any] | None = None
    if payload.walk_forward.enabled:
        try:
            walk_forward = purged_walk_forward(
                bars,
                strategy=payload.strategy,
                folds=payload.walk_forward.folds,
                train_bars=payload.walk_forward.train_bars,
                test_bars=payload.walk_forward.test_bars,
                purge_bars=payload.walk_forward.purge_bars,
                embargo_bars=payload.walk_forward.embargo_bars,
                min_train_trades=payload.walk_forward.min_train_trades,
                **common,
            )
        except ValueError as exc:
            walk_forward = {"error": str(exc)}

    return {
        "inst_id": inst_id,
        "bar": bar,
        "candles": len(bars),
        "strategy": payload.strategy,
        "params": params.to_dict(),
        "cost": cost.to_dict(),
        "summary": full["metrics"],
        "equity": full["equity"],
        "trades": full["trades"],
        "cost_sensitivity": sensitivity,
        "walk_forward": walk_forward,
    }
