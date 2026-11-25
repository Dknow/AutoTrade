"""策略与风控接口：启停、参数调整、状态查询、Kill Switch。"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.services.strategy_service import StrategyConfig

logger = logging.getLogger(__name__)

router = APIRouter(tags=["strategy"])


class StrategyUpdate(BaseModel):
    fast: int | None = Field(None, gt=0)
    slow: int | None = Field(None, gt=0)
    atr_period: int | None = Field(None, gt=0)
    atr_multiple: float | None = Field(None, gt=0)
    size: Decimal | None = Field(None, gt=0)
    lever: int | None = Field(None, gt=0)
    cooldown_bars: int | None = Field(None, ge=0)


class KillSwitchRequest(BaseModel):
    close_all: bool = False
    reason: str = "人工触发"


@router.get("/strategy")
async def strategy_status(request: Request) -> dict:
    return request.app.state.strategy_service.snapshot()


@router.post("/strategy/start")
async def strategy_start(request: Request) -> dict:
    service = request.app.state.strategy_service
    try:
        return await service.start()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": str(exc)}) from exc


@router.post("/strategy/stop")
async def strategy_stop(request: Request) -> dict:
    return await request.app.state.strategy_service.stop()


@router.patch("/strategy/config")
async def strategy_config(request: Request, payload: StrategyUpdate) -> dict:
    service = request.app.state.strategy_service
    if service.running:
        raise HTTPException(
            status_code=409,
            detail={"code": "STRATEGY_RUNNING", "message": "策略运行中，请先停止再修改参数"},
        )
    current = service.config
    updated = StrategyConfig(
        fast=payload.fast if payload.fast is not None else current.fast,
        slow=payload.slow if payload.slow is not None else current.slow,
        atr_period=payload.atr_period if payload.atr_period is not None else current.atr_period,
        atr_multiple=payload.atr_multiple if payload.atr_multiple is not None else current.atr_multiple,
        size=payload.size if payload.size is not None else current.size,
        lever=payload.lever if payload.lever is not None else current.lever,
        td_mode=current.td_mode,
        cooldown_bars=payload.cooldown_bars if payload.cooldown_bars is not None else current.cooldown_bars,
    )
    try:
        return service.update_config(updated)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": str(exc)}) from exc


@router.get("/risk")
async def risk_status(request: Request) -> dict:
    return request.app.state.risk_service.snapshot()


@router.post("/risk/kill-switch")
async def kill_switch(request: Request, payload: KillSwitchRequest) -> dict:
    """一键熔断：停止策略，可选市价全平所有持仓。"""
    risk = request.app.state.risk_service
    strategy = request.app.state.strategy_service
    account = request.app.state.account_service
    trade = request.app.state.trade_service

    risk.trip(payload.reason)
    await strategy.stop()

    closed: list[dict[str, Any]] = []
    if payload.close_all:
        for position in account.active_positions():
            inst_id = str(position.get("instId") or "")
            pos_side = str(position.get("posSide") or "")
            try:
                result = await trade.close_position(inst_id=inst_id, pos_side=pos_side)
                closed.append({"instId": inst_id, "posSide": pos_side, "result": result})
            except Exception as exc:  # noqa: BLE001 - 单个仓位平仓失败要继续处理其余仓位
                logger.warning("Kill Switch 平仓失败 %s %s: %s", inst_id, pos_side, exc)
                closed.append({"instId": inst_id, "posSide": pos_side, "error": str(exc)})

    return {"risk": risk.snapshot(), "closed": closed}


@router.post("/risk/reset")
async def risk_reset(request: Request) -> dict:
    risk = request.app.state.risk_service
    risk.reset()
    return risk.snapshot()
