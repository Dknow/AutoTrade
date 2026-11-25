"""交易接口：开仓、平仓、撤单、改单、杠杆与合约规格查询。"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.exchange.okx_client import OkxAPIError
from app.services.instrument_service import InstrumentNotFound
from app.services.trade_service import TradeValidationError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["trade"])


class OpenPositionRequest(BaseModel):
    inst_id: str = Field(..., min_length=3)
    pos_side: Literal["long", "short"]
    size: Decimal = Field(..., gt=0, description="张数")
    ord_type: Literal["limit", "market"] = "limit"
    price: Decimal | None = Field(None, gt=0, description="限价单必填")
    td_mode: Literal["cross", "isolated"] = "cross"
    lever: Decimal | None = Field(None, gt=0)
    tp_trigger_px: Decimal | None = Field(None, gt=0)
    sl_trigger_px: Decimal | None = Field(None, gt=0)


class ClosePositionRequest(BaseModel):
    inst_id: str = Field(..., min_length=3)
    pos_side: Literal["long", "short"]
    size: Decimal | None = Field(None, gt=0, description="留空表示全平")
    ord_type: Literal["limit", "market"] = "market"
    price: Decimal | None = Field(None, gt=0)
    td_mode: Literal["cross", "isolated"] = "cross"


class CancelOrderRequest(BaseModel):
    inst_id: str


class AmendOrderRequest(BaseModel):
    inst_id: str
    size: Decimal | None = Field(None, gt=0)
    price: Decimal | None = Field(None, gt=0)


class LeverageRequest(BaseModel):
    inst_id: str
    lever: Decimal = Field(..., gt=0)
    td_mode: Literal["cross", "isolated"] = "cross"
    pos_side: Literal["long", "short"] | None = None


def _fail(exc: Exception) -> HTTPException:
    if isinstance(exc, (TradeValidationError, InstrumentNotFound, ValueError)):
        return HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": str(exc)})
    if isinstance(exc, OkxAPIError):
        return HTTPException(status_code=502, detail={"code": exc.code, "message": exc.message})
    return HTTPException(status_code=500, detail={"code": "INTERNAL_ERROR", "message": str(exc)})


@router.get("/trade/instruments")
async def list_instruments(request: Request) -> dict:
    service = request.app.state.instruments
    return {
        "inst_ids": service.inst_ids(),
        "specs": {
            inst_id: {
                "ctVal": str(service.get(inst_id).ct_val),
                "ctValCcy": service.get(inst_id).ct_val_ccy,
                "lotSz": str(service.get(inst_id).lot_sz),
                "minSz": str(service.get(inst_id).min_sz),
                "tickSz": str(service.get(inst_id).tick_sz),
                "maxLever": str(service.get(inst_id).max_lever),
            }
            for inst_id in service.inst_ids()
        },
    }


@router.post("/trade/instruments/refresh")
async def refresh_instruments(request: Request) -> dict:
    count = await request.app.state.instruments.load()
    return {"loaded": count}


@router.get("/trade/orders/pending")
async def pending_orders(request: Request, inst_id: str | None = Query(None)) -> dict:
    client = request.app.state.okx_client
    try:
        rows = await client.get_orders(inst_type="SWAP", inst_id=inst_id)
    except OkxAPIError as exc:
        raise _fail(exc) from exc
    return {"orders": rows}


@router.post("/trade/orders")
async def open_position(request: Request, payload: OpenPositionRequest) -> dict:
    service = request.app.state.trade_service
    try:
        return await service.open_position(
            inst_id=payload.inst_id,
            pos_side=payload.pos_side,
            size=payload.size,
            ord_type=payload.ord_type,
            price=payload.price,
            td_mode=payload.td_mode,
            lever=payload.lever,
            tp_trigger_px=payload.tp_trigger_px,
            sl_trigger_px=payload.sl_trigger_px,
        )
    except Exception as exc:  # noqa: BLE001 - 统一转成结构化 HTTP 错误
        logger.warning("开仓失败: %s", exc)
        raise _fail(exc) from exc


@router.post("/trade/orders/{ord_id}/cancel")
async def cancel_order(request: Request, ord_id: str, payload: CancelOrderRequest) -> dict:
    service = request.app.state.trade_service
    try:
        return await service.cancel_order(inst_id=payload.inst_id, ord_id=ord_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("撤单失败: %s", exc)
        raise _fail(exc) from exc


@router.post("/trade/orders/{ord_id}/amend")
async def amend_order(request: Request, ord_id: str, payload: AmendOrderRequest) -> dict:
    service = request.app.state.trade_service
    try:
        return await service.amend_order(
            inst_id=payload.inst_id, ord_id=ord_id, size=payload.size, price=payload.price
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("改单失败: %s", exc)
        raise _fail(exc) from exc


@router.post("/trade/positions/close")
async def close_position(request: Request, payload: ClosePositionRequest) -> dict:
    service = request.app.state.trade_service
    try:
        return await service.close_position(
            inst_id=payload.inst_id,
            pos_side=payload.pos_side,
            size=payload.size,
            ord_type=payload.ord_type,
            price=payload.price,
            td_mode=payload.td_mode,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("平仓失败: %s", exc)
        raise _fail(exc) from exc


@router.post("/trade/leverage")
async def set_leverage(request: Request, payload: LeverageRequest) -> dict:
    service = request.app.state.trade_service
    try:
        return await service.set_leverage(
            inst_id=payload.inst_id,
            lever=payload.lever,
            td_mode=payload.td_mode,
            pos_side=payload.pos_side,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("杠杆设置失败: %s", exc)
        raise _fail(exc) from exc
