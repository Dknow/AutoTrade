"""历史 K 线存储：只追加、唯一键去重、只读取已收盘数据。"""

from __future__ import annotations

from app.db.session import Base, create_db_engine, create_session_factory
from app.services.candle_store import CandleStore


def _store(tmp_path) -> CandleStore:
    engine = create_db_engine(f"sqlite:///{tmp_path}/candles.db")
    Base.metadata.create_all(engine)
    return CandleStore(create_session_factory(engine))


def _row(ts: int, close: float = 100.0, *, confirm: str = "1") -> list[str]:
    return [str(ts), "100", "101", "99", str(close), "1", "0", "0", confirm]


def test_insert_is_append_only_and_dedupes(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.insert("btc-usdt-swap", "4H", [_row(1000), _row(2000)]) == 2

    # 重复 ts 被忽略，原值不会被覆盖；新增 ts 正常写入
    assert store.insert("BTC-USDT-SWAP", "4H", [_row(2000, close=999.0), _row(3000)]) == 1

    rows = store.load("BTC-USDT-SWAP", "4H")
    assert [int(row[0]) for row in rows] == [1000, 2000, 3000]
    assert float(rows[1][4]) == 100.0, "重复写入不能覆盖已有 K 线"
    assert store.count("BTC-USDT-SWAP", "4H") == 3


def test_load_skips_unconfirmed_and_supports_limit(tmp_path) -> None:
    store = _store(tmp_path)
    store.insert("BTC-USDT-SWAP", "1H", [_row(1000), _row(2000, confirm="0"), _row(3000)])

    rows = store.load("BTC-USDT-SWAP", "1H")
    assert [int(row[0]) for row in rows] == [1000, 3000], "未收盘 K 线不得进入回测"

    recent = store.load("BTC-USDT-SWAP", "1H", limit=1)
    assert [int(row[0]) for row in recent] == [3000]

    with_unconfirmed = store.load("BTC-USDT-SWAP", "1H", only_confirmed=False)
    assert len(with_unconfirmed) == 3


def test_latest_earliest_and_coverage(tmp_path) -> None:
    store = _store(tmp_path)
    store.insert("BTC-USDT-SWAP", "1H", [_row(1000), _row(2000)])
    store.insert("ETH-USDT-SWAP", "1D", [_row(500)])

    assert store.latest_ts("BTC-USDT-SWAP", "1H") == 2000
    assert store.earliest_ts("BTC-USDT-SWAP", "1H") == 1000
    assert store.latest_ts("SOL-USDT-SWAP", "1H") is None

    coverage = {(item["inst_id"], item["bar"]): item for item in store.coverage()}
    assert coverage[("BTC-USDT-SWAP", "1H")]["count"] == 2
    assert coverage[("BTC-USDT-SWAP", "1H")]["first_ts"] == 1000
    assert coverage[("ETH-USDT-SWAP", "1D")]["last_ts"] == 500
