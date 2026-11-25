"""行情快照、历史 K 线与前端实时推送端点。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect

from app.core.config import get_settings
from app.exchange.okx_client import OkxAPIError, create_okx_client_from_settings

router = APIRouter(tags=["market"])


@router.get("/market/snapshot")
async def market_snapshot(request: Request) -> dict:
    return request.app.state.market_service.snapshot()


@router.get("/market/candles")
async def market_candles(
    request: Request,
    inst_id: str | None = Query(None, alias="instId"),
    bar: str = Query("1m"),
    limit: int = Query(200, ge=1, le=300),
) -> dict:
    """从 OKX REST 拉取历史 K 线（前端首次加载用）。"""
    service = request.app.state.market_service
    target = (inst_id or service.inst_id).strip().upper()
    client = create_okx_client_from_settings(get_settings())
    try:
        rows = await client.get_candles(target, bar=bar, limit=limit)
    except OkxAPIError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    finally:
        await client.aclose()
    return {"inst_id": target, "bar": bar, "candles": rows}


@router.websocket("/ws/stream")
async def stream(websocket: WebSocket) -> None:
    """前端实时通道：首帧下发快照，之后增量推送 ticker/candle/positions/orders。"""
    await websocket.accept()
    market = websocket.app.state.market_service
    account = websocket.app.state.account_service
    market.hub.add(websocket)
    account.hub.add(websocket)
    try:
        await websocket.send_json(
            {
                "type": "snapshot",
                "market": market.snapshot(),
                "account": account.snapshot(),
            }
        )
        while True:
            # 前端定时发送心跳文本，保持连接活跃
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        market.hub.remove(websocket)
        account.hub.remove(websocket)
