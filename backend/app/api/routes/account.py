"""账户接口：净值历史查询。"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from app.db.models import EquitySnapshotRow

router = APIRouter(tags=["account"])


@router.get("/account/equity-history")
async def equity_history(request: Request, limit: int = Query(300, ge=1, le=1000)) -> dict:
    """返回升序净值点 [{ts, eq}]，前端净值曲线使用。"""
    factory = request.app.state.session_factory
    with factory() as session:
        rows = list(
            session.scalars(
                select(EquitySnapshotRow).order_by(EquitySnapshotRow.ts.desc()).limit(limit)
            )
        )
    rows.reverse()
    return {"points": [{"ts": row.ts, "eq": row.total_eq} for row in rows]}
