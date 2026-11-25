"""策略状态持久化：把配置与运行痕迹存进 SQLite，重启后恢复参数。

只持久化"可恢复且不危险"的状态：
- 配置参数与标的；
- 上次信号与动作时间（避免重启后同一个信号立刻重复开仓）；
- 不恢复 running 与止损单号，避免与交易所实际挂单不一致。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import sessionmaker

from app.db.models import StrategyStateRow

logger = logging.getLogger(__name__)

STATE_KEY = "strategy"


class StrategyStateStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self.session_factory = session_factory

    def load(self) -> dict[str, Any]:
        try:
            with self.session_factory() as session:
                row = session.get(StrategyStateRow, STATE_KEY)
                if row is None:
                    return {}
                return json.loads(row.payload or "{}")
        except Exception as exc:  # noqa: BLE001 - 读取失败不应阻塞启动
            logger.warning("策略状态读取失败: %s", exc)
            return {}

    def save(self, payload: dict[str, Any]) -> None:
        try:
            with self.session_factory() as session:
                row = session.get(StrategyStateRow, STATE_KEY)
                text = json.dumps(payload, ensure_ascii=False)
                if row is None:
                    session.add(StrategyStateRow(key=STATE_KEY, payload=text))
                else:
                    row.payload = text
                session.commit()
        except Exception as exc:  # noqa: BLE001 - 写入失败只告警
            logger.warning("策略状态保存失败: %s", exc)
