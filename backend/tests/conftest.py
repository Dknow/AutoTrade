"""测试公共夹具：OKX 客户端。"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.exchange.okx_client import create_okx_client_from_settings


@pytest.fixture
async def okx_client():
    client = create_okx_client_from_settings(get_settings())
    try:
        yield client
    finally:
        await client.aclose()
