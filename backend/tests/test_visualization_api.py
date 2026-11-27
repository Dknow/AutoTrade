"""P4 可视化接口测试：净值快照落库/查询、条件单列表与撤单路由。"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import account, trade
from app.core.config import Settings
from app.db.session import Base, create_db_engine, create_session_factory
from app.services.account_service import AccountService


def _app_with_state(**state: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(account.router)
    app.include_router(trade.router)
    for key, value in state.items():
        setattr(app.state, key, value)
    return app


def test_equity_snapshot_throttle_and_history(tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path}/equity.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)

    service = AccountService(Settings(), session_factory=factory)
    service.account = {"totalEq": "1000.5"}
    service._record_equity()
    # 60 秒节流窗口内重复调用不应再写
    service.account = {"totalEq": "1001"}
    service._record_equity()
    # 回拨时间戳，模拟一分钟后的第二次快照
    service._last_equity_ts -= 61_000
    service._record_equity()

    client = TestClient(_app_with_state(session_factory=factory))
    response = client.get("/account/equity-history")
    assert response.status_code == 200
    points = response.json()["points"]
    assert len(points) == 2
    assert points[0]["ts"] < points[1]["ts"]  # 升序返回
    assert points[0]["eq"] == "1000.5"
    assert points[1]["eq"] == "1001"


class FakeOkxClient:
    def __init__(self) -> None:
        self.queried_types: list[str] = []

    async def get_algo_orders(
        self, *, inst_id: str | None = None, ord_type: str = "conditional", inst_type: str = "SWAP"
    ) -> list[dict]:
        self.queried_types.append(ord_type)
        return [{"algoId": f"{ord_type}-1", "ordType": ord_type, "instId": inst_id}]


class FakeTradeService:
    def __init__(self) -> None:
        self.cancelled: list[dict] = []

    async def cancel_stop_loss(self, **kwargs: Any) -> dict:
        self.cancelled.append(kwargs)
        return {"cancelled": kwargs["algo_ids"]}


def test_algo_orders_route_merges_conditional_and_trailing() -> None:
    client = FakeOkxClient()
    http = TestClient(_app_with_state(okx_client=client))
    response = http.get("/trade/algo-orders", params={"instId": "BTC-USDT-SWAP"})
    assert response.status_code == 200
    orders = response.json()["orders"]
    assert [item["ordType"] for item in orders] == ["conditional", "move_order_stop"]
    assert client.queried_types == ["conditional", "move_order_stop"]


def test_cancel_algo_order_route() -> None:
    service = FakeTradeService()
    http = TestClient(_app_with_state(trade_service=service))
    response = http.post(
        "/trade/algo-orders/algo-1/cancel", json={"inst_id": "BTC-USDT-SWAP"}
    )
    assert response.status_code == 200
    assert service.cancelled == [{"inst_id": "BTC-USDT-SWAP", "algo_ids": ["algo-1"]}]
