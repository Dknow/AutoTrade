"""交易执行服务：开仓、平仓、撤单、改单、杠杆设置。

双向持仓（long_short_mode）的核心约束：
- 开多 = side=buy  + posSide=long
- 开空 = side=sell + posSide=short
- 平仓 = 反向 side + 原 posSide + reduceOnly，或调用 close-position 全平

任何一步参数写错都可能变成"反向开仓"，因此这里集中做校验，路由层不再拼参数。
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from app.exchange.okx_client import OkxClient
from app.services.account_service import AccountService
from app.services.instrument_service import InstrumentService, plain

logger = logging.getLogger(__name__)

POS_SIDES = ("long", "short")
ORDER_TYPES = ("limit", "market")


class TradeValidationError(ValueError):
    """入参不满足交易前置条件。"""


class TradeService:
    def __init__(
        self,
        client: OkxClient,
        instruments: InstrumentService,
        account_service: AccountService | None = None,
    ) -> None:
        self.client = client
        self.instruments = instruments
        self.account_service = account_service

    # ------------------------------------------------------------------ 开仓
    async def open_position(
        self,
        *,
        inst_id: str,
        pos_side: str,
        size: Decimal | str | float,
        ord_type: str = "limit",
        price: Decimal | str | float | None = None,
        td_mode: str = "cross",
        lever: Decimal | str | float | None = None,
        tp_trigger_px: Decimal | str | float | None = None,
        sl_trigger_px: Decimal | str | float | None = None,
    ) -> dict[str, Any]:
        pos_side = _check_pos_side(pos_side)
        ord_type = _check_order_type(ord_type)
        spec = self.instruments.get(inst_id)
        sz = _align_size(spec, size)

        px: str | None = None
        if ord_type == "limit":
            if price is None:
                raise TradeValidationError("限价单必须提供价格")
            px = plain(_align_price(spec, price))
        elif price is not None:
            logger.info("市价单忽略传入价格: %s", price)

        if lever is not None:
            await self.set_leverage(inst_id=inst_id, lever=lever, td_mode=td_mode, pos_side=pos_side)

        side = "buy" if pos_side == "long" else "sell"
        order = await self.client.place_order(
            inst_id=inst_id,
            td_mode=td_mode,
            side=side,
            ord_type=ord_type,
            sz=plain(sz),
            pos_side=pos_side,
            px=px,
            tp_trigger_px=_optional_price(spec, tp_trigger_px),
            sl_trigger_px=_optional_price(spec, sl_trigger_px),
        )
        logger.info(
            "开仓已提交 | %s %s %s张 %s | ordId=%s",
            inst_id, pos_side, plain(sz), ord_type, order.get("ordId"),
        )
        return {"order": order, "request": {"side": side, "posSide": pos_side, "sz": plain(sz), "px": px}}

    # ------------------------------------------------------------------ 平仓
    async def close_position(
        self,
        *,
        inst_id: str,
        pos_side: str,
        size: Decimal | str | float | None = None,
        ord_type: str = "market",
        price: Decimal | str | float | None = None,
        td_mode: str = "cross",
    ) -> dict[str, Any]:
        pos_side = _check_pos_side(pos_side)
        ord_type = _check_order_type(ord_type)
        spec = self.instruments.get(inst_id)
        held = self._held_size(inst_id, pos_side)

        # 全平：交给 OKX 的 close-position，同时自动撤销该仓位的止盈止损
        if size is None:
            if held is not None and held <= 0:
                raise TradeValidationError(f"{inst_id} {pos_side} 方向当前无持仓，无法平仓")
            data = await self.client.close_position(inst_id=inst_id, mgn_mode=td_mode, pos_side=pos_side)
            logger.info("全平已提交 | %s %s", inst_id, pos_side)
            return {"closed": data, "mode": "full", "posSide": pos_side}

        sz = _align_size(spec, size)
        if held is not None and sz > held:
            raise TradeValidationError(f"平仓数量 {sz} 超过当前持仓 {held}")
        px: str | None = None
        if ord_type == "limit":
            if price is None:
                raise TradeValidationError("限价平仓必须提供价格")
            px = plain(_align_price(spec, price))

        # 平多 = sell + long，平空 = buy + short，必须带 reduceOnly
        side = "sell" if pos_side == "long" else "buy"
        order = await self.client.place_order(
            inst_id=inst_id,
            td_mode=td_mode,
            side=side,
            ord_type=ord_type,
            sz=plain(sz),
            pos_side=pos_side,
            px=px,
            reduce_only=True,
        )
        logger.info("部分平仓已提交 | %s %s %s张 | ordId=%s", inst_id, pos_side, plain(sz), order.get("ordId"))
        return {"order": order, "mode": "partial", "request": {"side": side, "posSide": pos_side, "sz": plain(sz)}}

    # ------------------------------------------------------------------ 撤改
    async def cancel_order(self, *, inst_id: str, ord_id: str) -> dict[str, Any]:
        if not ord_id:
            raise TradeValidationError("撤单必须提供 ordId")
        result = await self.client.cancel_order(inst_id=inst_id, ord_id=ord_id)
        logger.info("撤单已提交 | %s ordId=%s", inst_id, ord_id)
        return {"order": result}

    async def amend_order(
        self,
        *,
        inst_id: str,
        ord_id: str,
        size: Decimal | str | float | None = None,
        price: Decimal | str | float | None = None,
    ) -> dict[str, Any]:
        if not ord_id:
            raise TradeValidationError("改单必须提供 ordId")
        if size is None and price is None:
            raise TradeValidationError("改单至少要修改数量或价格")
        spec = self.instruments.get(inst_id)
        new_sz = plain(_align_size(spec, size)) if size is not None else None
        new_px = plain(_align_price(spec, price)) if price is not None else None
        result = await self.client.amend_order(
            inst_id=inst_id, ord_id=ord_id, new_sz=new_sz, new_px=new_px
        )
        logger.info("改单已提交 | %s ordId=%s sz=%s px=%s", inst_id, ord_id, new_sz, new_px)
        return {"order": result}

    # ------------------------------------------------------------------ 杠杆
    async def set_leverage(
        self,
        *,
        inst_id: str,
        lever: Decimal | str | float,
        td_mode: str = "cross",
        pos_side: str | None = None,
    ) -> dict[str, Any]:
        spec = self.instruments.get(inst_id)
        lever_value = Decimal(str(lever))
        if lever_value <= 0:
            raise TradeValidationError("杠杆必须大于 0")
        if spec.max_lever > 0 and lever_value > spec.max_lever:
            raise TradeValidationError(f"杠杆 {lever_value} 超过该合约上限 {spec.max_lever}")
        if pos_side is not None:
            pos_side = _check_pos_side(pos_side)
        result = await self.client.set_leverage(
            inst_id=inst_id, lever=plain(lever_value), mgn_mode=td_mode, pos_side=pos_side
        )
        logger.info("杠杆设置完成 | %s %sx %s posSide=%s", inst_id, plain(lever_value), td_mode, pos_side)
        return {"leverage": result}

    # ------------------------------------------------------------------ 止损
    async def place_stop_loss(
        self,
        *,
        inst_id: str,
        pos_side: str,
        size: Decimal | str | float,
        trigger_px: Decimal | str | float,
        td_mode: str = "cross",
    ) -> dict[str, Any]:
        """挂独立止损条件单，返回 algoId（用于后续移动止损时撤销）。"""
        pos_side = _check_pos_side(pos_side)
        spec = self.instruments.get(inst_id)
        sz = plain(_align_size(spec, size))
        px = plain(_align_price(spec, trigger_px))
        side = "sell" if pos_side == "long" else "buy"
        algo = await self.client.place_algo_order(
            inst_id=inst_id,
            td_mode=td_mode,
            side=side,
            pos_side=pos_side,
            sz=sz,
            sl_trigger_px=px,
        )
        logger.info("止损条件单已提交 | %s %s %s张 触发价=%s algoId=%s", inst_id, pos_side, sz, px, algo.get("algoId"))
        return {"algo": algo, "triggerPx": px, "sz": sz}

    async def place_trailing_stop(
        self,
        *,
        inst_id: str,
        pos_side: str,
        size: Decimal | str | float,
        callback_spread: Decimal | str | float,
        td_mode: str = "cross",
    ) -> dict[str, Any]:
        """挂交易所原生移动止损（move_order_stop），由交易所负责跟踪最优价。"""
        pos_side = _check_pos_side(pos_side)
        spec = self.instruments.get(inst_id)
        sz = plain(_align_size(spec, size))
        spread = plain(_align_price(spec, callback_spread))
        side = "sell" if pos_side == "long" else "buy"
        algo = await self.client.place_trailing_stop(
            inst_id=inst_id,
            td_mode=td_mode,
            side=side,
            pos_side=pos_side,
            sz=sz,
            callback_spread=spread,
        )
        logger.info(
            "移动止损已提交 | %s %s %s张 回调价差=%s algoId=%s",
            inst_id, pos_side, sz, spread, algo.get("algoId"),
        )
        return {"algo": algo, "callbackSpread": spread, "sz": sz}

    async def cancel_stop_loss(self, *, inst_id: str, algo_ids: list[str]) -> dict[str, Any]:
        if not algo_ids:
            return {"cancelled": []}
        result = await self.client.cancel_algo_orders(inst_id=inst_id, algo_ids=algo_ids)
        logger.info("止损条件单已撤销 | %s %s", inst_id, algo_ids)
        return {"cancelled": result}

    async def list_stop_losses(self, *, inst_id: str, pos_side: str | None = None) -> list[dict[str, Any]]:
        orders = await self.client.get_algo_orders(inst_id=inst_id)
        if pos_side is not None:
            orders = [item for item in orders if item.get("posSide") == pos_side]
        return orders

    # ------------------------------------------------------------------ 内部
    def _held_size(self, inst_id: str, pos_side: str) -> Decimal | None:
        """从账户缓存读取该方向持仓张数。

        缓存未就绪（REST 快照未完成 / 私有频道未推送）时返回 None 跳过本地校验，
        否则刚开仓就平仓会因为缓存滞后被误判为"无持仓"。
        """
        if self.account_service is None or not self.account_service.ready:
            return None
        for position in self.account_service.active_positions():
            if position.get("instId") == inst_id and position.get("posSide") == pos_side:
                return abs(Decimal(str(position.get("pos") or 0)))
        return Decimal(0)


def _check_pos_side(pos_side: str) -> str:
    if pos_side not in POS_SIDES:
        raise TradeValidationError(f"posSide 必须是 {POS_SIDES} 之一，当前 {pos_side!r}")
    return pos_side


def _check_order_type(ord_type: str) -> str:
    if ord_type not in ORDER_TYPES:
        raise TradeValidationError(f"ordType 必须是 {ORDER_TYPES} 之一，当前 {ord_type!r}")
    return ord_type


def _align_size(spec: Any, value: Decimal | str | float) -> Decimal:
    """把规格层的 ValueError 统一成交易校验异常，便于路由层映射成 400。"""
    try:
        return spec.align_size(Decimal(str(value)))
    except ValueError as exc:
        raise TradeValidationError(str(exc)) from exc


def _align_price(spec: Any, value: Decimal | str | float) -> Decimal:
    try:
        return spec.align_price(Decimal(str(value)))
    except ValueError as exc:
        raise TradeValidationError(str(exc)) from exc


def _optional_price(spec: Any, value: Decimal | str | float | None) -> str | None:
    if value is None or str(value) == "":
        return None
    return plain(_align_price(spec, value))
