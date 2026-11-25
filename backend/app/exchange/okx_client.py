"""OKX v5 REST 客户端：签名、时间同步、限频重试、模拟盘开关。

设计原则：
- 只依赖 httpx，不引入第三方交易所 SDK，接口行为完全可控；
- 所有错误统一抛 OkxAPIError，便于上层风控与日志处理；
- 模拟盘通过请求头 x-simulated-trading: 1 切换，默认开启。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

from app.exchange.okx_sign import build_request_path, iso_timestamp, now_ms, sign
from app.exchange.rate_limiter import RateLimiter

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class OkxAPIError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        endpoint: str = "",
        http_status: int | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.endpoint = endpoint
        self.http_status = http_status
        super().__init__(f"OKX API 错误 [{code}] {message} (endpoint={endpoint}, http={http_status})")


class DnsOverrideTransport(httpx.AsyncBaseTransport):
    """DNS 被污染时的兜底：把指定域名的 TCP 目标换成固定 IP。

    URL 的 Host 头与 TLS SNI 仍使用原域名，因此证书校验行为不变。
    """

    def __init__(self, transport: httpx.AsyncBaseTransport, overrides: dict[str, str]) -> None:
        self._transport = transport
        self._overrides = overrides

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        target = self._overrides.get(host)
        if target:
            headers = httpx.Headers(request.headers)
            headers["host"] = host
            extensions = dict(request.extensions)
            extensions["sni_hostname"] = host
            request = httpx.Request(
                method=request.method,
                url=request.url.copy_with(host=target),
                headers=headers,
                content=request.content,
                extensions=extensions,
            )
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self._transport.aclose()


class OkxClient:
    def __init__(
        self,
        *,
        api_key: str = "",
        secret_key: str = "",
        passphrase: str = "",
        base_url: str = "https://www.okx.com",
        simulated: bool = True,
        timeout: float = 10.0,
        proxy: str = "",
        max_calls: int = 10,
        period: float = 2.0,
        transport: httpx.AsyncBaseTransport | None = None,
        resolve_ip: str = "",
    ) -> None:
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.base_url = base_url.rstrip("/")
        self.simulated = simulated
        self._time_offset_ms = 0
        self._time_synced = False
        self._limiter = RateLimiter(max_calls=max_calls, period=period)
        if transport is None and resolve_ip and not proxy:
            host = urlsplit(self.base_url).hostname or ""
            transport = DnsOverrideTransport(httpx.AsyncHTTPTransport(), {host: resolve_ip})
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout),
            proxy=proxy or None,
            headers={"Accept": "application/json"},
            transport=transport,
        )

    # ------------------------------------------------------------------ 基础
    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.secret_key and self.passphrase)

    @property
    def time_offset_ms(self) -> int:
        return self._time_offset_ms

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "OkxClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    async def sync_time(self) -> int:
        """以服务器时间为准计算本地时钟偏移，避免签名 timestamp 过期。"""
        data = await self._request("GET", "/api/v5/public/time", auth=False)
        server_ms = int(data[0]["ts"])
        self._time_offset_ms = server_ms - now_ms()
        self._time_synced = True
        return self._time_offset_ms

    def _current_ms(self) -> int:
        return now_ms() + self._time_offset_ms

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        auth: bool = False,
        retries: int = 2,
        use_rate_limit: bool = True,
    ) -> list[Any]:
        method = method.upper()
        if auth:
            if not self.has_credentials:
                raise OkxAPIError("MISSING_CREDENTIALS", "未配置 OKX API Key/Secret/Passphrase")
            if not self._time_synced:
                await self.sync_time()

        request_path = build_request_path(path, params)
        body = json.dumps(json_body, separators=(",", ":")) if json_body is not None else ""
        headers: dict[str, str] = {}
        if body:
            headers["Content-Type"] = "application/json"
        if auth:
            timestamp = iso_timestamp(self._current_ms())
            headers.update(
                {
                    "OK-ACCESS-KEY": self.api_key,
                    "OK-ACCESS-SIGN": sign(self.secret_key, timestamp, method, request_path, body),
                    "OK-ACCESS-TIMESTAMP": timestamp,
                    "OK-ACCESS-PASSPHRASE": self.passphrase,
                }
            )
            if self.simulated:
                headers["x-simulated-trading"] = "1"

        last_error: Exception | None = None
        for attempt in range(retries + 1):
            if use_rate_limit:
                await self._limiter.acquire()
            try:
                response = await self._client.request(
                    method,
                    request_path,
                    content=body or None,
                    headers=headers,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt >= retries:
                    raise OkxAPIError("NETWORK_ERROR", str(exc), endpoint=request_path) from exc
                await asyncio.sleep(0.5 * (2**attempt))
                continue

            if response.status_code in _RETRYABLE_STATUS and attempt < retries:
                await asyncio.sleep(0.5 * (2**attempt))
                continue

            try:
                payload = response.json()
            except ValueError as exc:
                raise OkxAPIError(
                    "INVALID_RESPONSE",
                    response.text[:200],
                    endpoint=request_path,
                    http_status=response.status_code,
                ) from exc

            code = str(payload.get("code", ""))
            if code != "0":
                raise OkxAPIError(
                    code,
                    str(payload.get("msg", "")),
                    endpoint=request_path,
                    http_status=response.status_code,
                )
            data = payload.get("data")
            return data if isinstance(data, list) else []

        raise OkxAPIError("RETRY_EXHAUSTED", str(last_error), endpoint=request_path)

    # ------------------------------------------------------------ 公共行情
    async def get_server_time(self) -> int:
        data = await self._request("GET", "/api/v5/public/time", auth=False)
        return int(data[0]["ts"])

    async def get_instruments(self, inst_type: str = "SWAP", inst_id: str | None = None) -> list[dict]:
        data = await self._request(
            "GET",
            "/api/v5/public/instruments",
            params={"instType": inst_type, "instId": inst_id or ""},
            auth=False,
        )
        return [item for item in data if isinstance(item, dict)]

    async def get_ticker(self, inst_id: str) -> dict:
        data = await self._request(
            "GET",
            "/api/v5/market/ticker",
            params={"instId": inst_id},
            auth=False,
        )
        return data[0] if data else {}

    async def get_candles(
        self,
        inst_id: str,
        *,
        bar: str = "1m",
        limit: int = 100,
        after: str | None = None,
        before: str | None = None,
    ) -> list[list[str]]:
        data = await self._request(
            "GET",
            "/api/v5/market/candles",
            params={"instId": inst_id, "bar": bar, "limit": limit, "after": after, "before": before},
            auth=False,
        )
        return [row for row in data if isinstance(row, list)]

    # ------------------------------------------------------------ 账户私有
    async def get_balance(self, ccy: str | None = None) -> list[dict]:
        data = await self._request(
            "GET",
            "/api/v5/account/balance",
            params={"ccy": ccy or ""},
            auth=True,
        )
        return [item for item in data if isinstance(item, dict)]

    async def get_positions(self, inst_type: str = "SWAP", inst_id: str | None = None) -> list[dict]:
        data = await self._request(
            "GET",
            "/api/v5/account/positions",
            params={"instType": inst_type, "instId": inst_id or ""},
            auth=True,
        )
        return [item for item in data if isinstance(item, dict)]

    async def get_account_config(self) -> dict:
        data = await self._request("GET", "/api/v5/account/config", auth=True)
        return data[0] if data else {}

    # ------------------------------------------------------------ 交易私有
    async def place_order(
        self,
        *,
        inst_id: str,
        td_mode: str,
        side: str,
        ord_type: str,
        sz: str,
        pos_side: str | None = None,
        px: str | None = None,
        reduce_only: bool = False,
        cl_ord_id: str | None = None,
        tp_trigger_px: str | None = None,
        tp_ord_px: str | None = None,
        sl_trigger_px: str | None = None,
        sl_ord_px: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side,
            "ordType": ord_type,
            "sz": str(sz),
        }
        if pos_side:
            body["posSide"] = pos_side
        if px is not None:
            body["px"] = str(px)
        if reduce_only:
            body["reduceOnly"] = True
        if cl_ord_id:
            body["clOrdId"] = cl_ord_id
        if tp_trigger_px is not None:
            body["tpTriggerPx"] = str(tp_trigger_px)
            body["tpOrdPx"] = str(tp_ord_px) if tp_ord_px is not None else "-1"
        if sl_trigger_px is not None:
            body["slTriggerPx"] = str(sl_trigger_px)
            body["slOrdPx"] = str(sl_ord_px) if sl_ord_px is not None else "-1"

        data = await self._request("POST", "/api/v5/trade/order", json_body=body, auth=True)
        return data[0] if data else {}

    async def cancel_order(
        self,
        *,
        inst_id: str,
        ord_id: str | None = None,
        cl_ord_id: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {"instId": inst_id}
        if ord_id:
            body["ordId"] = ord_id
        elif cl_ord_id:
            body["clOrdId"] = cl_ord_id
        else:
            raise ValueError("撤单必须提供 ord_id 或 cl_ord_id")
        data = await self._request("POST", "/api/v5/trade/cancel-order", json_body=body, auth=True)
        return data[0] if data else {}

    async def get_order(
        self,
        *,
        inst_id: str,
        ord_id: str | None = None,
        cl_ord_id: str | None = None,
    ) -> dict:
        data = await self._request(
            "GET",
            "/api/v5/trade/order",
            params={"instId": inst_id, "ordId": ord_id or "", "clOrdId": cl_ord_id or ""},
            auth=True,
        )
        return data[0] if data else {}

    async def close_position(
        self,
        *,
        inst_id: str,
        mgn_mode: str,
        pos_side: str | None = None,
        auto_cancel: bool = True,
    ) -> list[dict]:
        body: dict[str, Any] = {
            "instId": inst_id,
            "mgnMode": mgn_mode,
            "autoCxl": auto_cancel,
        }
        if pos_side:
            body["posSide"] = pos_side
        return await self._request("POST", "/api/v5/trade/close-position", json_body=body, auth=True)


def create_okx_client_from_settings(settings: Any) -> OkxClient:
    """按配置创建客户端。"""
    return OkxClient(
        api_key=settings.okx_api_key,
        secret_key=settings.okx_secret_key,
        passphrase=settings.okx_passphrase,
        base_url=settings.okx_base_url,
        simulated=settings.okx_simulated,
        timeout=settings.okx_timeout,
        proxy=settings.okx_proxy,
        resolve_ip=settings.okx_resolve_ip,
    )
