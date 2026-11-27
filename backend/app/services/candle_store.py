"""历史 K 线存储：SQLite 只追加，唯一键 (inst_id, bar, ts) 去重。

设计约束（对应 README 的数据安全约定）：
- 只插入，不更新、不删除，重复写入按唯一键忽略；
- 只保存 OKX 原生字段，不做复权/补全，保证可追溯；
- 回测读取时只返回 `confirm=1`（已收盘）的 K 线。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import sessionmaker

from app.db.models import CandleRow


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_row(candle: CandleRow) -> list[str]:
    """转成与 OKX K 线接口一致的字符串数组，方便上层复用。"""
    return [
        str(candle.ts),
        str(candle.open),
        str(candle.high),
        str(candle.low),
        str(candle.close),
        str(candle.vol),
        "0",
        "0",
        str(candle.confirm),
    ]


class CandleStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self.session_factory = session_factory

    # ------------------------------------------------------------------ 写入
    def insert(self, inst_id: str, bar: str, rows: Iterable[Sequence[Any]]) -> int:
        """批量写入 K 线，返回实际新增条数；已存在的 (inst_id, bar, ts) 直接忽略。"""
        inst_id = inst_id.strip().upper()
        bar = bar.strip()
        values: list[dict[str, Any]] = []
        seen: set[int] = set()
        for row in rows:
            if not row:
                continue
            try:
                ts = int(row[0])
            except (TypeError, ValueError):
                continue
            if ts in seen:
                continue
            seen.add(ts)
            confirm = 1
            if len(row) > 8 and str(row[8]).strip() != "":
                confirm = 1 if str(row[8]).strip() == "1" else 0
            values.append(
                {
                    "inst_id": inst_id,
                    "bar": bar,
                    "ts": ts,
                    "open": _to_float(row[1]),
                    "high": _to_float(row[2]),
                    "low": _to_float(row[3]),
                    "close": _to_float(row[4]),
                    "vol": _to_float(row[5]) if len(row) > 5 else 0.0,
                    "confirm": confirm,
                }
            )
        if not values:
            return 0
        statement = (
            sqlite_insert(CandleRow)
            .values(values)
            .on_conflict_do_nothing(index_elements=["inst_id", "bar", "ts"])
        )
        with self.session_factory() as session:
            result = session.execute(statement)
            session.commit()
            return int(result.rowcount or 0)

    # ------------------------------------------------------------------ 读取
    def load(
        self,
        inst_id: str,
        bar: str,
        *,
        start_ts: int | None = None,
        end_ts: int | None = None,
        limit: int | None = None,
        only_confirmed: bool = True,
    ) -> list[list[str]]:
        """按时间升序返回 K 线；limit 表示取最近的 N 根。"""
        inst_id = inst_id.strip().upper()
        bar = bar.strip()
        statement = select(CandleRow).where(CandleRow.inst_id == inst_id, CandleRow.bar == bar)
        if start_ts is not None:
            statement = statement.where(CandleRow.ts >= int(start_ts))
        if end_ts is not None:
            statement = statement.where(CandleRow.ts <= int(end_ts))
        if only_confirmed:
            statement = statement.where(CandleRow.confirm == 1)
        if limit is not None and limit > 0:
            statement = statement.order_by(CandleRow.ts.desc()).limit(limit)
            with self.session_factory() as session:
                rows = list(session.scalars(statement))
            rows.reverse()
            return [_to_row(row) for row in rows]
        statement = statement.order_by(CandleRow.ts.asc())
        with self.session_factory() as session:
            return [_to_row(row) for row in session.scalars(statement)]

    def latest_ts(self, inst_id: str, bar: str) -> int | None:
        statement = select(func.max(CandleRow.ts)).where(
            CandleRow.inst_id == inst_id.strip().upper(), CandleRow.bar == bar.strip()
        )
        with self.session_factory() as session:
            return session.scalar(statement)

    def earliest_ts(self, inst_id: str, bar: str) -> int | None:
        statement = select(func.min(CandleRow.ts)).where(
            CandleRow.inst_id == inst_id.strip().upper(), CandleRow.bar == bar.strip()
        )
        with self.session_factory() as session:
            return session.scalar(statement)

    def count(self, inst_id: str, bar: str) -> int:
        statement = (
            select(func.count())
            .select_from(CandleRow)
            .where(CandleRow.inst_id == inst_id.strip().upper(), CandleRow.bar == bar.strip())
        )
        with self.session_factory() as session:
            return int(session.scalar(statement) or 0)

    def coverage(self) -> list[dict[str, Any]]:
        """返回每个 (inst_id, bar) 的条数与首尾时间戳，供前端/接口检查数据完整性。"""
        statement = (
            select(
                CandleRow.inst_id,
                CandleRow.bar,
                func.count().label("count"),
                func.min(CandleRow.ts).label("first_ts"),
                func.max(CandleRow.ts).label("last_ts"),
            )
            .group_by(CandleRow.inst_id, CandleRow.bar)
            .order_by(CandleRow.inst_id, CandleRow.bar)
        )
        with self.session_factory() as session:
            return [
                {
                    "inst_id": row.inst_id,
                    "bar": row.bar,
                    "count": int(row.count),
                    "first_ts": int(row.first_ts) if row.first_ts is not None else None,
                    "last_ts": int(row.last_ts) if row.last_ts is not None else None,
                }
                for row in session.execute(statement)
            ]
