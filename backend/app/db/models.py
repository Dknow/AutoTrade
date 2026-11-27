"""ORM 模型。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StrategyStateRow(Base):
    """键值形式保存策略状态，单行即可（key 固定为 strategy）。"""

    __tablename__ = "strategy_state"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class EquitySnapshotRow(Base):
    """账户净值快照（每分钟最多一条），用于前端净值曲线。"""

    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[int] = mapped_column(BigInteger, index=True)  # 毫秒时间戳
    total_eq: Mapped[str] = mapped_column(String(40))
