"""健康检查与交易所连接自检。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from app.core.config import get_settings
from app.exchange.okx_client import OkxAPIError, create_okx_client_from_settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app_name,
        "env": settings.app_env,
        "simulated": settings.okx_simulated,
        "okx_credentials_configured": settings.okx_has_credentials,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/exchange/selfcheck")
async def exchange_selfcheck() -> dict:
    """探测 OKX 连通性、时钟偏移与账户配置（不产生任何交易）。"""
    settings = get_settings()
    client = create_okx_client_from_settings(settings)
    try:
        offset_ms = await client.sync_time()
        result: dict = {
            "reachable": True,
            "base_url": settings.okx_base_url,
            "simulated": settings.okx_simulated,
            "time_offset_ms": offset_ms,
            "credentials_configured": settings.okx_has_credentials,
        }
        if settings.okx_has_credentials:
            config = await client.get_account_config()
            result["account"] = {
                "uid": config.get("uid"),
                "acct_lv": config.get("acctLv"),
                "pos_mode": config.get("posMode"),
                "mgn_mode": config.get("mgnMode"),
            }
        return result
    except OkxAPIError as exc:
        logger.warning("OKX 自检失败: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={"code": exc.code, "message": exc.message, "endpoint": exc.endpoint},
        ) from exc
    finally:
        await client.aclose()
