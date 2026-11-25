"""账户服务：私有频道推送持仓/订单/账户，并与 REST 初始快照对齐。"""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import Settings
from app.exchange.okx_client import create_okx_client_from_settings
from app.exchange.okx_ws import PRIVATE_SIMULATED_WS_URL, PRIVATE_WS_URL, OkxWsClient
from app.services.broadcaster import ClientHub

logger = logging.getLogger(__name__)


class AccountService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.enabled = settings.okx_has_credentials
        self.positions: dict[str, dict[str, Any]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.account: dict[str, Any] = {}
        self.hub = ClientHub()
        self.ws: OkxWsClient | None = None
        if self.enabled:
            url = PRIVATE_SIMULATED_WS_URL if settings.okx_simulated else PRIVATE_WS_URL
            self.ws = OkxWsClient(
                url,
                api_key=settings.okx_api_key,
                secret_key=settings.okx_secret_key,
                passphrase=settings.okx_passphrase,
                login=True,
                name="private",
            )

    async def start(self) -> None:
        if not self.enabled or self.ws is None:
            logger.info("未配置 OKX 凭据，账户私有频道不启动")
            return
        await self._load_initial_snapshot()
        self.ws.set_handler(self._on_message)
        await self.ws.start()
        await self.ws.subscribe(
            [
                {"channel": "positions", "instType": "SWAP"},
                {"channel": "orders", "instType": "SWAP"},
                {"channel": "account"},
            ]
        )
        logger.info("账户服务已启动（模拟盘=%s）", self.settings.okx_simulated)

    async def stop(self) -> None:
        if self.ws is not None:
            await self.ws.stop()

    async def _load_initial_snapshot(self) -> None:
        client = create_okx_client_from_settings(self.settings)
        try:
            for position in await client.get_positions("SWAP"):
                self._upsert_position(position)
            balance = await client.get_balance("USDT")
            if balance:
                self.account = balance[0]
        except Exception as exc:  # noqa: BLE001 - 快照失败不应阻塞启动
            logger.warning("账户初始快照拉取失败: %s", exc)
        finally:
            await client.aclose()

    # ------------------------------------------------------------ 读取缓存
    def active_positions(self) -> list[dict[str, Any]]:
        return [p for p in self.positions.values() if float(p.get("pos") or 0) != 0]

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "positions": self.active_positions(),
            "orders": list(self.orders.values()),
            "account": self.account,
            "ws_connected": bool(self.ws and self.ws.connected),
            "ws_logged_in": bool(self.ws and self.ws.logged_in),
        }

    # ------------------------------------------------------------ 消息处理
    def _upsert_position(self, position: dict[str, Any]) -> None:
        key = f"{position.get('instId', '')}:{position.get('posSide') or 'net'}"
        self.positions[key] = position

    async def _on_message(self, message: dict[str, Any]) -> None:
        channel = str((message.get("arg") or {}).get("channel", ""))
        data = message.get("data") or []

        if channel == "positions":
            for position in data:
                self._upsert_position(position)
            await self.hub.broadcast({"type": "positions", "data": self.active_positions()})
            return

        if channel == "orders":
            for order in data:
                ord_id = order.get("ordId")
                if ord_id:
                    self.orders[ord_id] = order
            await self.hub.broadcast({"type": "orders", "data": list(self.orders.values())[-50:]})
            return

        if channel == "account" and data:
            self.account = data[0]
            await self.hub.broadcast({"type": "account", "data": self.account})
