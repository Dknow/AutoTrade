"""OKX v5 签名与请求串构建。

签名规则（官方文档）：
    prehash = timestamp + method + requestPath + body
    sign    = base64(hmac_sha256(secret_key, prehash))
其中 timestamp 为 ISO8601 UTC 毫秒格式，requestPath 包含 query string。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlencode


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_timestamp(ts_ms: int) -> str:
    """毫秒时间戳 -> OKX 要求的 ISO8601 格式，例如 2020-12-08T09:08:57.715Z。"""
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{ts_ms % 1000:03d}Z"


def build_query(params: Mapping[str, Any] | None) -> str:
    """构建 query string：忽略空值，布尔值按小写字符串输出。"""
    if not params:
        return ""
    items: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        items.append((key, str(value)))
    return urlencode(items)


def build_request_path(path: str, params: Mapping[str, Any] | None = None) -> str:
    query = build_query(params)
    return f"{path}?{query}" if query else path


def sign(secret_key: str, timestamp: str, method: str, request_path: str, body: str = "") -> str:
    prehash = f"{timestamp}{method.upper()}{request_path}{body}"
    digest = hmac.new(secret_key.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")
