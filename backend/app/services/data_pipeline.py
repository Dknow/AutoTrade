"""历史 K 线数据管道：多年回补 + 增量更新 + 断点续传。

工作方式（全部只追加，不覆盖已有数据）：
1. 从「最新」开始，用 history-candles 的 `after` 向历史方向翻页；
2. 每页最多 100 根，写库时按唯一键 (inst_id, bar, ts) 去重；
3. 当整页时间戳都不晚于库中最新时间戳时停止 —— 增量更新到此完成；
4. 当翻到目标起始时间（默认 5 年）时停止 —— 首次回补到此完成；
5. 中断后再次运行会从最新继续向前补齐，天然支持断点续传。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from app.services.candle_store import CandleStore

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 100  # OKX history-candles 单页上限
DEFAULT_YEARS = 5.0
MAX_PAGES = 5000


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class SeriesSpec:
    inst_id: str
    bar: str


@dataclass
class SyncResult:
    inst_id: str
    bar: str
    inserted: int = 0
    pages: int = 0
    first_ts: int | None = None
    last_ts: int | None = None
    truncated: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def sync_series(
    client: Any,
    store: CandleStore,
    inst_id: str,
    bar: str,
    *,
    years: float = DEFAULT_YEARS,
    now: int | None = None,
    page_size: int = MAX_PAGE_SIZE,
    max_pages: int = MAX_PAGES,
) -> SyncResult:
    """同步单个 (标的, 周期)。client 需要提供 get_history_candles。"""
    inst_id = inst_id.strip().upper()
    bar = bar.strip()
    if not inst_id or not bar:
        raise ValueError("inst_id 与 bar 不能为空")
    if years <= 0:
        raise ValueError("years 必须大于 0")
    page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    current_ms = now if now is not None else now_ms()
    start_ts = current_ms - int(years * 365 * 24 * 3600 * 1000)

    latest = store.latest_ts(inst_id, bar)
    inserted = 0
    pages = 0
    cursor: str | None = None
    result = SyncResult(inst_id=inst_id, bar=bar)

    while pages < max_pages:
        rows = await client.get_history_candles(inst_id, bar=bar, limit=page_size, after=cursor)
        pages += 1
        if not rows:
            break
        inserted += store.insert(inst_id, bar, rows)
        timestamps = [int(row[0]) for row in rows if row]
        if not timestamps:
            break
        page_min, page_max = min(timestamps), max(timestamps)
        # 整页都不晚于库中最新时间戳：增量目标已达成
        if latest is not None and page_max <= latest:
            break
        # 已翻到目标起始时间
        if page_min <= start_ts:
            break
        if len(rows) < page_size:
            break
        next_cursor = str(page_min)
        if next_cursor == cursor:
            break
        cursor = next_cursor
    else:
        result.truncated = True

    result.inserted = inserted
    result.pages = pages
    result.first_ts = store.earliest_ts(inst_id, bar)
    result.last_ts = store.latest_ts(inst_id, bar)
    return result


@dataclass
class DataSyncManager:
    """后台同步任务管理：同一时间只允许一个任务，状态可轮询。"""

    client: Any
    store: CandleStore
    default_years: float = DEFAULT_YEARS
    _task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _status: dict[str, Any] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._status = {
            "running": False,
            "started_at": None,
            "finished_at": None,
            "years": self.default_years,
            "done_series": 0,
            "total_series": 0,
            "current": None,
            "results": [],
        }

    def snapshot(self) -> dict[str, Any]:
        status = dict(self._status)
        status["results"] = [dict(item) for item in self._status.get("results", [])]
        return status

    async def start(
        self, specs: Iterable[SeriesSpec], *, years: float | None = None
    ) -> dict[str, Any]:
        if self._task is not None and not self._task.done():
            raise RuntimeError("已有数据同步任务在运行")
        items = [SeriesSpec(spec.inst_id.strip().upper(), spec.bar.strip()) for spec in specs]
        if not items:
            raise ValueError("同步列表不能为空")
        for spec in items:
            if not spec.inst_id or not spec.bar:
                raise ValueError("inst_id 与 bar 不能为空")
        target_years = self.default_years if years is None else float(years)
        if target_years <= 0:
            raise ValueError("years 必须大于 0")
        self._status = {
            "running": True,
            "started_at": now_ms(),
            "finished_at": None,
            "years": target_years,
            "done_series": 0,
            "total_series": len(items),
            "current": None,
            "results": [],
        }
        self._task = asyncio.create_task(self.run(items, years=target_years))
        return self.snapshot()

    async def stop(self) -> None:
        """应用关闭时取消仍在跑的后台任务，避免解释器退出告警。"""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - 关闭阶段只记录
                logger.warning("数据同步任务关闭异常: %s", exc)
        self._task = None

    async def run(self, specs: Iterable[SeriesSpec], *, years: float | None = None) -> dict[str, Any]:
        """直接执行同步（测试或脚本可用，不创建后台任务）。"""
        items = list(specs)
        target_years = self.default_years if years is None else float(years)
        results: list[dict[str, Any]] = []
        self._status.update(
            running=True,
            started_at=self._status.get("started_at") or now_ms(),
            total_series=len(items),
            years=target_years,
        )
        try:
            for index, spec in enumerate(items, start=1):
                self._status["current"] = f"{spec.inst_id} {spec.bar}"
                try:
                    result = await sync_series(
                        self.client, self.store, spec.inst_id, spec.bar, years=target_years
                    )
                    results.append(result.to_dict())
                except Exception as exc:  # noqa: BLE001 - 单个系列失败不阻塞其余
                    logger.warning("K 线同步失败 %s %s: %s", spec.inst_id, spec.bar, exc)
                    results.append(
                        SyncResult(
                            inst_id=spec.inst_id, bar=spec.bar, error=str(exc)
                        ).to_dict()
                    )
                self._status["done_series"] = index
                self._status["results"] = list(results)
        finally:
            self._status["current"] = None
            self._status["running"] = False
            self._status["finished_at"] = now_ms()
            self._status["results"] = list(results)
        return self.snapshot()
