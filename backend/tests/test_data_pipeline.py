"""数据管道：多年回补、增量停止、断点续传与任务状态。"""

from __future__ import annotations

from typing import Any

from app.db.session import Base, create_db_engine, create_session_factory
from app.services.candle_store import CandleStore
from app.services.data_pipeline import DataSyncManager, SeriesSpec, sync_series

STEP_MS = 4 * 3600 * 1000  # 4H
BASE_TS = 1_700_000_000_000


def _store(tmp_path) -> CandleStore:
    engine = create_db_engine(f"sqlite:///{tmp_path}/candles.db")
    Base.metadata.create_all(engine)
    return CandleStore(create_session_factory(engine))


def _row(ts: int, close: float = 100.0) -> list[str]:
    return [str(ts), "100", "101", "99", str(close), "1", "0", "0", "1"]


def _pages(count: int, page_size: int = 100) -> list[list[list[str]]]:
    """按「最新页在前」构造分页数据，模拟 OKX history-candles 的返回顺序。

    最新一页始终是满页，不足 page_size 的短页只会出现在最早的一页。
    """
    timestamps = [BASE_TS + index * STEP_MS for index in range(count)]
    pages: list[list[int]] = []
    for end in range(count, 0, -page_size):
        start = max(0, end - page_size)
        pages.append(timestamps[start:end])
    return [[_row(ts) for ts in page] for page in pages]


class FakeHistoryClient:
    def __init__(self, pages: list[list[list[str]]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    async def get_history_candles(
        self, inst_id: str, *, bar: str = "1m", limit: int = 100, after=None, before=None
    ) -> list[list[str]]:
        self.calls.append({"inst_id": inst_id, "bar": bar, "limit": limit, "after": after})
        index = len(self.calls) - 1
        return self.pages[index] if index < len(self.pages) else []


async def test_sync_series_backfills_until_short_page(tmp_path) -> None:
    store = _store(tmp_path)
    client = FakeHistoryClient(_pages(250))

    result = await sync_series(client, store, "btc-usdt-swap", "4H", years=5, now=BASE_TS)

    assert result.inserted == 250
    assert result.pages == 3
    assert result.first_ts == BASE_TS
    assert result.last_ts == BASE_TS + 249 * STEP_MS
    # 最后一页不足 100 根，直接停止，不再多打一次空请求
    assert len(client.calls) == 3
    assert client.calls[0]["inst_id"] == "BTC-USDT-SWAP"


async def test_sync_series_is_incremental_and_append_only(tmp_path) -> None:
    store = _store(tmp_path)
    pages = _pages(300)
    oldest_page = pages[-1]
    store.insert("BTC-USDT-SWAP", "4H", oldest_page)
    stored_close = store.load("BTC-USDT-SWAP", "4H")[0][4]

    client = FakeHistoryClient(pages)
    result = await sync_series(client, store, "BTC-USDT-SWAP", "4H", years=5, now=BASE_TS)

    assert result.inserted == 200, "只应补齐缺失的两页"
    assert store.count("BTC-USDT-SWAP", "4H") == 300
    assert store.load("BTC-USDT-SWAP", "4H")[0][4] == stored_close
    # 第三页整页都不晚于库中最新时间戳，增量目标达成后立即停止
    assert len(client.calls) == 3


async def test_sync_series_stops_at_target_start(tmp_path) -> None:
    store = _store(tmp_path)
    pages = _pages(500)  # 5 页
    last_ts = BASE_TS + 499 * STEP_MS
    cutoff_ts = BASE_TS + 250 * STEP_MS
    years = (last_ts - cutoff_ts) / (365 * 24 * 3600 * 1000)

    client = FakeHistoryClient(pages)
    result = await sync_series(client, store, "BTC-USDT-SWAP", "4H", years=years, now=last_ts)

    assert result.first_ts <= cutoff_ts
    assert result.first_ts < BASE_TS + 250 * STEP_MS
    assert store.count("BTC-USDT-SWAP", "4H") == 300
    assert len(client.calls) == 3


async def test_manager_reports_progress_and_finishes(tmp_path) -> None:
    store = _store(tmp_path)
    manager = DataSyncManager(client=FakeHistoryClient(_pages(120)), store=store, default_years=5)

    snapshot = await manager.run([SeriesSpec("BTC-USDT-SWAP", "4H")], years=5)

    assert snapshot["running"] is False
    assert snapshot["total_series"] == 1
    assert snapshot["done_series"] == 1
    assert snapshot["finished_at"] is not None
    assert snapshot["results"][0]["inserted"] == 120
    assert store.count("BTC-USDT-SWAP", "4H") == 120


async def test_manager_rejects_empty_specs(tmp_path) -> None:
    store = _store(tmp_path)
    manager = DataSyncManager(client=FakeHistoryClient([]), store=store)
    try:
        await manager.start([])
    except ValueError as exc:
        assert "不能为空" in str(exc)
    else:  # pragma: no cover - 兜底
        raise AssertionError("空列表必须报错")
