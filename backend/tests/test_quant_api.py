"""量化接口测试：数据覆盖度、回测响应结构与错误分支。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import data, quant
from app.core.config import Settings
from app.db.session import Base, create_db_engine, create_session_factory
from app.services.candle_store import CandleStore
from app.services.data_pipeline import SeriesSpec

BASE_TS = 1_700_000_000_000
STEP_MS = 4 * 3600 * 1000


def _rows(count: int = 60) -> list[list[str]]:
    rows: list[list[str]] = []
    for index in range(count):
        price = 100.0 + index * 0.2
        rows.append(
            [
                str(BASE_TS + index * STEP_MS),
                f"{price:.4f}",
                f"{price + 1:.4f}",
                f"{price - 1:.4f}",
                f"{price + 0.3:.4f}",
                "1",
                "0",
                "0",
                "1",
            ]
        )
    return rows


class FakeInstruments:
    def get(self, inst_id: str) -> Any:
        class _Spec:
            ct_val = Decimal("0.01")

        return _Spec()


class FakeSyncManager:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def start(self, specs: list[SeriesSpec], *, years: float | None = None) -> dict:
        if self.error is not None:
            raise self.error
        return {"running": True, "total_series": len(specs)}

    def snapshot(self) -> dict:
        return {"running": False, "results": []}


def _client(
    tmp_path,
    *,
    rows: list[list[str]] | None = None,
    settings: Settings | None = None,
    sync_manager: FakeSyncManager | None = None,
) -> TestClient:
    engine = create_db_engine(f"sqlite:///{tmp_path}/quant.db")
    Base.metadata.create_all(engine)
    store = CandleStore(create_session_factory(engine))
    if rows:
        store.insert("BTC-USDT-SWAP", "4H", rows)

    app = FastAPI()
    app.include_router(data.router, prefix="/api/v1")
    app.include_router(quant.router, prefix="/api/v1")
    app.state.candle_store = store
    app.state.instruments = FakeInstruments()
    app.state.settings = settings or Settings(
        quant_inst_ids="BTC-USDT-SWAP", quant_bars="4H"
    )
    app.state.data_sync = sync_manager or FakeSyncManager()
    return TestClient(app)


def test_data_coverage_route(tmp_path) -> None:
    client = _client(tmp_path, rows=_rows())
    response = client.get("/api/v1/data/coverage")
    assert response.status_code == 200
    series = response.json()["series"]
    assert len(series) == 1
    assert series[0]["inst_id"] == "BTC-USDT-SWAP"
    assert series[0]["count"] == 60


def test_quant_backtest_route_returns_metrics(tmp_path) -> None:
    client = _client(tmp_path, rows=_rows())
    response = client.post(
        "/api/v1/quant/backtest",
        json={
            "inst_id": "BTC-USDT-SWAP",
            "bar": "4H",
            "strategy": "donchian",
            "params": {"period": 5, "atr_period": 14},
            "walk_forward": {"enabled": False},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["candles"] == 60
    assert set(body["summary"]) >= {
        "expectancy_r",
        "sharpe",
        "max_drawdown_pct",
        "profit_factor",
        "trades",
    }
    assert len(body["cost_sensitivity"]) == 5
    assert body["walk_forward"] is None


def test_quant_backtest_requires_local_data(tmp_path) -> None:
    client = _client(tmp_path, rows=_rows())
    response = client.post(
        "/api/v1/quant/backtest",
        json={"inst_id": "ETH-USDT-SWAP", "bar": "4H", "walk_forward": {"enabled": False}},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "NO_DATA"


def test_quant_backtest_rejects_unknown_params(tmp_path) -> None:
    client = _client(tmp_path, rows=_rows())
    response = client.post(
        "/api/v1/quant/backtest",
        json={
            "inst_id": "BTC-USDT-SWAP",
            "bar": "4H",
            "params": {"not_a_param": 1},
            "walk_forward": {"enabled": False},
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "VALIDATION_ERROR"


def test_data_sync_returns_409_when_running(tmp_path) -> None:
    manager = FakeSyncManager(error=RuntimeError("已有数据同步任务在运行"))
    client = _client(tmp_path, rows=_rows(), sync_manager=manager)
    response = client.post("/api/v1/data/sync", json={"years": 3})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SYNC_RUNNING"


def test_data_sync_rejects_empty_specs(tmp_path) -> None:
    settings = Settings(quant_inst_ids="", market_inst_ids="", quant_bars="4H")
    client = _client(tmp_path, rows=_rows(), settings=settings)
    response = client.post("/api/v1/data/sync", json={})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "VALIDATION_ERROR"
