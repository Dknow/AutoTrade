"""合约规格服务：缓存 OKX 永续合约的面值、数量步长、价格精度与最小下单量。

下单前必须按交易所规则对齐数量与价格，否则会收到 `51008 / 51006` 类错误。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

from app.exchange.okx_client import OkxClient

logger = logging.getLogger(__name__)


class InstrumentNotFound(ValueError):
    """请求的合约不在已缓存的规格列表中。"""


@dataclass(frozen=True)
class InstrumentSpec:
    inst_id: str
    ct_val: Decimal          # 每张合约面值
    ct_val_ccy: str          # 面值币种
    lot_sz: Decimal          # 数量步长
    min_sz: Decimal          # 最小下单张数
    tick_sz: Decimal         # 价格最小变动
    max_lever: Decimal       # 最大杠杆

    def align_size(self, size: Decimal) -> Decimal:
        """向下对齐到 lotSz，并校验不低于 minSz。"""
        if size <= 0:
            raise ValueError(f"下单数量必须大于 0，当前 {size}")
        steps = (size / self.lot_sz).to_integral_value(rounding=ROUND_DOWN)
        aligned = steps * self.lot_sz
        if aligned < self.min_sz:
            raise ValueError(
                f"数量 {size} 小于最小下单量 {self.min_sz}（步长 {self.lot_sz}）"
            )
        return aligned.normalize()

    def align_price(self, price: Decimal) -> Decimal:
        """按 tickSz 四舍五入价格。"""
        if price <= 0:
            raise ValueError(f"价格必须大于 0，当前 {price}")
        ticks = (price / self.tick_sz).to_integral_value(rounding=ROUND_HALF_UP)
        return (ticks * self.tick_sz).normalize()


def plain(value: Decimal | str | float) -> str:
    """把 Decimal 转成不带科学计数法的字符串，供 OKX 接口使用。"""
    decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    return format(decimal.normalize(), "f")


def _to_decimal(value: object, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError(f"合约字段 {field} 非法: {value!r}") from exc


class InstrumentService:
    """一次加载、进程内缓存；支持按需刷新。"""

    def __init__(self, client: OkxClient, *, inst_type: str = "SWAP") -> None:
        self.client = client
        self.inst_type = inst_type
        self._specs: dict[str, InstrumentSpec] = {}

    @property
    def loaded(self) -> bool:
        return bool(self._specs)

    def inst_ids(self) -> list[str]:
        return sorted(self._specs)

    async def load(self) -> int:
        rows = await self.client.get_instruments(self.inst_type)
        specs: dict[str, InstrumentSpec] = {}
        for row in rows:
            inst_id = str(row.get("instId") or "")
            # 只缓存 USDT 本位永续（线性合约），币本位不做支持
            if not inst_id.endswith("-USDT-SWAP"):
                continue
            try:
                specs[inst_id] = InstrumentSpec(
                    inst_id=inst_id,
                    ct_val=_to_decimal(row.get("ctVal"), "ctVal"),
                    ct_val_ccy=str(row.get("ctValCcy") or ""),
                    lot_sz=_to_decimal(row.get("lotSz"), "lotSz"),
                    min_sz=_to_decimal(row.get("minSz"), "minSz"),
                    tick_sz=_to_decimal(row.get("tickSz"), "tickSz"),
                    max_lever=_to_decimal(row.get("lever") or 0, "lever"),
                )
            except ValueError as exc:
                logger.warning("跳过合约 %s: %s", inst_id, exc)
        self._specs = specs
        logger.info("合约规格加载完成: %d 个 USDT 本位永续", len(specs))
        return len(specs)

    def get(self, inst_id: str) -> InstrumentSpec:
        spec = self._specs.get(inst_id)
        if spec is None:
            raise InstrumentNotFound(f"未找到合约规格: {inst_id}（请确认是 USDT 本位永续）")
        return spec

    def notional(self, inst_id: str, size: Decimal, price: Decimal) -> Decimal:
        """估算名义价值 = 张数 × 面值 × 价格（USDT 本位）。"""
        return size * self.get(inst_id).ct_val * price
