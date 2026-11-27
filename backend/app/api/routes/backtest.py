"""回测接口：拉取历史 K 线并运行双均线策略回测。"""

from __future__ import annotations

import logging
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.services.backtest_service import (
    MAX_HISTORY_LIMIT,
    BacktestParams,
    fetch_history_candles,
    run_backtest,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["backtest"])


class BacktestRequest(BaseModel):
    inst_id: str = Field("BTC-USDT-SWAP", min_length=1)
    bar: str = "1m"
    limit: int = Field(500, ge=100, le=MAX_HISTORY_LIMIT)
    fast: int = Field(9, gt=0)
    slow: int = Field(21, gt=0)
    atr_period: int = Field(14, gt=0)
    atr_multiple: float = Field(1.5, gt=0)
    size: Decimal = Field(Decimal("0.01"), gt=0)
    cooldown_bars: int = Field(3, ge=0)
    trailing: bool = True
    fee_rate: float = Field(0.0005, ge=0)
    slippage_rate: float = Field(0.0, ge=0)
    initial_equity: float = Field(1000.0, gt=0)


@router.post("/backtest/run")
async def backtest_run(request: Request, payload: BacktestRequest) -> dict:
    """运行回测。数据来自 OKX 公共 K 线接口，不触碰账户。"""
    instruments = request.app.state.instruments
    try:
        spec = instruments.get(payload.inst_id.strip().upper())
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "INSTRUMENT_NOT_FOUND", "message": str(exc)}
        ) from exc

    params = BacktestParams(
        inst_id=payload.inst_id.strip().upper(),
        bar=payload.bar,
        limit=payload.limit,
        fast=payload.fast,
        slow=payload.slow,
        atr_period=payload.atr_period,
        atr_multiple=payload.atr_multiple,
        size=payload.size,
        cooldown_bars=payload.cooldown_bars,
        trailing=payload.trailing,
        fee_rate=payload.fee_rate,
        slippage_rate=payload.slippage_rate,
        initial_equity=payload.initial_equity,
    )
    try:
        params.validate()
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "VALIDATION_ERROR", "message": str(exc)}
        ) from exc

    client = request.app.state.okx_client
    try:
        rows = await fetch_history_candles(client, params.inst_id, params.bar, params.limit)
    except Exception as exc:  # noqa: BLE001 - 上游异常统一转 503
        logger.warning("回测拉取 K 线失败: %s", exc)
        raise HTTPException(
            status_code=503, detail={"code": "MARKET_UNAVAILABLE", "message": str(exc)}
        ) from exc

    try:
        return run_backtest(rows, ct_val=float(spec.ct_val), params=params)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "BACKTEST_FAILED", "message": str(exc)}
        ) from exc
