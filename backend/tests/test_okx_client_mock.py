"""使用 MockTransport 验证请求头、签名与请求路径（不联网）。"""

from __future__ import annotations

import httpx

from app.exchange.okx_client import OkxClient
from app.exchange.okx_sign import sign


def _make_transport(record: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        record.append(request)
        if request.url.path == "/api/v5/public/time":
            return httpx.Response(200, json={"code": "0", "msg": "", "data": [{"ts": "1795603850807"}]})
        return httpx.Response(200, json={"code": "0", "msg": "", "data": [{"ordId": "12345"}]})

    return httpx.MockTransport(handler)


def _make_client(record: list[httpx.Request], *, simulated: bool = True) -> OkxClient:
    return OkxClient(
        api_key="api-key",
        secret_key="secret-key",
        passphrase="passphrase",
        simulated=simulated,
        transport=_make_transport(record),
    )


async def test_private_request_headers_and_signature() -> None:
    record: list[httpx.Request] = []
    async with _make_client(record) as client:
        await client.get_balance(ccy="BTC")

    request = record[-1]
    raw_path = request.url.raw_path.decode()
    assert raw_path == "/api/v5/account/balance?ccy=BTC"
    assert request.headers["OK-ACCESS-KEY"] == "api-key"
    assert request.headers["OK-ACCESS-PASSPHRASE"] == "passphrase"
    assert request.headers["x-simulated-trading"] == "1"
    expected = sign(
        "secret-key",
        request.headers["OK-ACCESS-TIMESTAMP"],
        request.method,
        raw_path,
        request.content.decode(),
    )
    assert request.headers["OK-ACCESS-SIGN"] == expected


async def test_post_body_is_compact_json_and_signed() -> None:
    record: list[httpx.Request] = []
    async with _make_client(record) as client:
        await client.place_order(
            inst_id="BTC-USDT-SWAP",
            td_mode="cross",
            side="buy",
            ord_type="limit",
            sz="1",
            px="10000",
            pos_side="long",
            cl_ord_id="at-test-001",
        )

    request = record[-1]
    assert request.method == "POST"
    assert request.headers["Content-Type"] == "application/json"
    body = request.content.decode()
    assert '"posSide":"long"' in body
    assert " " not in body
    expected = sign(
        "secret-key",
        request.headers["OK-ACCESS-TIMESTAMP"],
        "POST",
        request.url.raw_path.decode(),
        body,
    )
    assert request.headers["OK-ACCESS-SIGN"] == expected


async def test_simulated_header_absent_when_disabled() -> None:
    record: list[httpx.Request] = []
    async with _make_client(record, simulated=False) as client:
        await client.get_positions()

    assert "x-simulated-trading" not in record[-1].headers


async def test_time_sync_before_first_private_request() -> None:
    record: list[httpx.Request] = []
    async with _make_client(record) as client:
        await client.sync_time()
        await client.get_account_config()

    paths = [r.url.path for r in record]
    assert paths[0] == "/api/v5/public/time"
    assert paths[1] == "/api/v5/account/config"
    assert client.time_offset_ms != 0
