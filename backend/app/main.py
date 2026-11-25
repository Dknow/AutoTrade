"""FastAPI 应用入口。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.routes import health, market, trade
from app.core.config import Settings, get_settings
from app.db.session import Base, create_db_engine, create_session_factory
from app.exchange.okx_client import create_okx_client_from_settings
from app.services.account_service import AccountService
from app.services.instrument_service import InstrumentService
from app.services.market_service import MarketService
from app.services.trade_service import TradeService

logger = logging.getLogger(__name__)


async def _warmup_candles(market_service: MarketService, settings: Settings) -> None:
    client = create_okx_client_from_settings(settings)
    try:
        rows = await client.get_candles(market_service.inst_id, bar=market_service.bar, limit=200)
        market_service.seed_candles(rows)
        logger.info("历史 K 线预热完成: %d 条", len(rows))
    except Exception as exc:  # noqa: BLE001 - 预热失败不应阻塞启动
        logger.warning("历史 K 线预热失败（不影响启动）: %s", exc)
    finally:
        await client.aclose()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    engine = create_db_engine(settings.database_url)
    Base.metadata.create_all(engine)
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)

    market_service = MarketService()
    account_service = AccountService(settings)
    okx_client = create_okx_client_from_settings(settings)
    instruments = InstrumentService(okx_client)
    trade_service = TradeService(okx_client, instruments, account_service)

    app.state.market_service = market_service
    app.state.account_service = account_service
    app.state.okx_client = okx_client
    app.state.instruments = instruments
    app.state.trade_service = trade_service

    await _warmup_candles(market_service, settings)
    try:
        await instruments.load()
    except Exception as exc:  # noqa: BLE001 - 规格加载失败不应阻塞行情展示
        logger.warning("合约规格加载失败（交易接口暂不可用）: %s", exc)
    await market_service.start()
    await account_service.start()
    logger.info("AutoTrade 启动 | env=%s | simulated=%s | base_url=%s", settings.app_env, settings.okx_simulated, settings.okx_base_url)
    try:
        yield
    finally:
        await account_service.stop()
        await market_service.stop()
        await okx_client.aclose()
        engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router, prefix=settings.api_prefix)
    app.include_router(market.router, prefix=settings.api_prefix)
    app.include_router(trade.router, prefix=settings.api_prefix)
    return app


app = create_app()
