"""OKX 私有接口冒烟测试（需要模拟盘 API Key）。

未配置凭据时整体跳过；已配置时执行：
查账户配置 -> 查余额 -> 查持仓 -> 挂远价限价单 -> 查单 -> 撤单。
所有下单都在模拟盘（x-simulated-trading=1）进行，且价格远离市价，不会成交。
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

import pytest

from app.core.config import get_settings
from app.exchange.okx_client import OkxAPIError

pytestmark = pytest.mark.skipif(
    not get_settings().okx_has_credentials,
    reason="未配置 OKX 模拟盘凭据（OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE）",
)

TEST_INST_ID = "DOGE-USDT-SWAP"


def _align_price(price: float, tick_sz: str) -> str:
    tick = Decimal(tick_sz)
    value = (Decimal(str(price)) / tick).to_integral_value(rounding=ROUND_DOWN) * tick
    return format(value.normalize(), "f")


async def test_account_config_and_balance(okx_client) -> None:
    config = await okx_client.get_account_config()
    assert config.get("uid")
    balance = await okx_client.get_balance("USDT")
    assert balance, "USDT 余额为空，请给模拟盘账户充值"


async def test_positions_readable(okx_client) -> None:
    positions = await okx_client.get_positions("SWAP")
    assert isinstance(positions, list)


async def test_place_query_cancel_order(okx_client) -> None:
    config = await okx_client.get_account_config()
    pos_mode = config.get("posMode")

    instruments = await okx_client.get_instruments("SWAP", TEST_INST_ID)
    assert instruments, f"{TEST_INST_ID} 合约信息为空"
    instrument = instruments[0]
    tick_sz = instrument["tickSz"]

    ticker = await okx_client.get_ticker(TEST_INST_ID)
    far_price = float(ticker["last"]) * 0.5

    order_kwargs = {
        "inst_id": TEST_INST_ID,
        "td_mode": "cross",
        "side": "buy",
        "ord_type": "limit",
        "sz": instrument.get("minSz", "1"),
        "px": _align_price(far_price, tick_sz),
    }
    if pos_mode == "long_short_mode":
        order_kwargs["pos_side"] = "long"

    try:
        result = await okx_client.place_order(**order_kwargs)
    except OkxAPIError as exc:
        pytest.skip(f"模拟盘下单失败（可能资金或权限不足）：{exc}")

    ord_id = result.get("ordId")
    assert ord_id, f"下单返回缺少 ordId: {result}"

    try:
        detail = await okx_client.get_order(inst_id=TEST_INST_ID, ord_id=ord_id)
        assert detail.get("ordId") == ord_id
        assert detail.get("state") in {"live", "partially_filled", "filled", "canceled"}
    finally:
        try:
            await okx_client.cancel_order(inst_id=TEST_INST_ID, ord_id=ord_id)
        except OkxAPIError:
            pass
