"""ORM 模型。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Float, SmallInteger, String, Text, UniqueConstraint
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


class CandleRow(Base):
    """历史 K 线，只追加不覆盖：唯一键 (inst_id, bar, ts)，重复写入自动忽略。

    confirm=1 表示该根已收盘。回测只读取已收盘数据，避免未来函数。
    """

    __tablename__ = "candles"
    __table_args__ = (UniqueConstraint("inst_id", "bar", "ts", name="uq_candles_inst_bar_ts"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    inst_id: Mapped[str] = mapped_column(String(32), nullable=False)
    bar: Mapped[str] = mapped_column(String(8), nullable=False)
    ts: Mapped[int] = mapped_column(BigInteger, nullable=False)  # 毫秒时间戳
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    vol: Mapped[float] = mapped_column(Float, default=0.0)
    confirm: Mapped[int] = mapped_column(SmallInteger, default=1)
