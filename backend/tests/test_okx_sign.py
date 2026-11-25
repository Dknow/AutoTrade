"""签名与请求串构建的离线单元测试。"""

from __future__ import annotations

import base64
import hashlib
import hmac

from app.exchange.okx_sign import build_query, build_request_path, iso_timestamp, sign


def _reference_sign(secret: str, timestamp: str, method: str, path: str, body: str) -> str:
    message = f"{timestamp}{method.upper()}{path}{body}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def test_sign_matches_reference_implementation() -> None:
    args = ("secret-key", "2020-12-08T09:08:57.715Z", "GET", "/api/v5/account/balance?ccy=BTC", "")
    assert sign(*args) == _reference_sign(*args)


def test_sign_is_base64_of_sha256() -> None:
    value = sign("s", "2020-12-08T09:08:57.715Z", "GET", "/api/v5/public/time")
    assert len(value) == 44
    assert value.endswith("=")


def test_sign_method_is_case_insensitive() -> None:
    ts, path = "2020-12-08T09:08:57.715Z", "/api/v5/trade/order"
    assert sign("s", ts, "post", path, "{}") == sign("s", ts, "POST", path, "{}")


def test_sign_changes_with_body_and_path() -> None:
    base = sign("s", "ts", "POST", "/api/v5/trade/order", '{"sz":"1"}')
    assert base != sign("s", "ts", "POST", "/api/v5/trade/order", '{"sz":"2"}')
    assert base != sign("s", "ts", "POST", "/api/v5/trade/cancel-order", '{"sz":"1"}')


def test_build_query_skips_none_and_lowercases_bool() -> None:
    query = build_query({"instType": "SWAP", "instId": None, "reduceOnly": True, "flag": False})
    assert query == "instType=SWAP&reduceOnly=true&flag=false"


def test_build_request_path_without_params() -> None:
    assert build_request_path("/api/v5/public/time") == "/api/v5/public/time"
    assert build_request_path("/api/v5/public/time", {}) == "/api/v5/public/time"
    assert build_request_path("/a", {"x": 1}) == "/a?x=1"


def test_iso_timestamp_format() -> None:
    assert iso_timestamp(1607504937715) == "2020-12-09T09:08:57.715Z"
    assert iso_timestamp(1607504937000) == "2020-12-09T09:08:57.000Z"
