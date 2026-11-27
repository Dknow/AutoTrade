"""历史数据接口：覆盖度查询与后台同步任务。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core.config import Settings
from app.services.data_pipeline import DataSyncManager, SeriesSpec

router = APIRouter(tags=["data"])


class SyncRequest(BaseModel):
    inst_ids: list[str] | None = None
    bars: list[str] | None = None
    years: float | None = Field(default=None, gt=0, le=10)


def _build_specs(
    settings: Settings, inst_ids: list[str] | None, bars: list[str] | None
) -> list[SeriesSpec]:
    inst_list = [
        item.strip().upper() for item in (inst_ids or settings.quant_inst_id_list) if item.strip()
    ]
    bar_list = [item.strip() for item in (bars or settings.quant_bar_list) if item.strip()]
    specs: list[SeriesSpec] = []
    seen: set[tuple[str, str]] = set()
    for inst_id in inst_list:
        for bar in bar_list:
            key = (inst_id, bar)
            if key in seen:
                continue
            seen.add(key)
            specs.append(SeriesSpec(inst_id=inst_id, bar=bar))
    return specs


@router.get("/data/coverage")
def data_coverage(request: Request) -> dict:
    """各 (标的, 周期) 已入库的 K 线条数与首尾时间戳。"""
    return {"series": request.app.state.candle_store.coverage()}


@router.get("/data/sync/status")
def data_sync_status(request: Request) -> dict:
    """查看后台同步任务进度。"""
    return request.app.state.data_sync.snapshot()


@router.post("/data/sync", status_code=status.HTTP_202_ACCEPTED)
async def data_sync(request: Request, payload: SyncRequest | None = None) -> dict:
    """启动后台同步：多年回补 + 增量更新，重复写入自动去重。"""
    settings: Settings = request.app.state.settings
    manager: DataSyncManager = request.app.state.data_sync
    body = payload or SyncRequest()
    specs = _build_specs(settings, body.inst_ids, body.bars)
    if not specs:
        raise HTTPException(
            status_code=400,
            detail={"code": "VALIDATION_ERROR", "message": "同步列表为空"},
        )
    try:
        return await manager.start(specs, years=body.years)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "SYNC_RUNNING", "message": str(exc)}
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "VALIDATION_ERROR", "message": str(exc)}
        ) from exc
