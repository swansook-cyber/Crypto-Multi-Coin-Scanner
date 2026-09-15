# -*- coding: utf-8 -*-
"""Fixed-route USD-M read client. Error output never contains remote free text."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import math
import time
from typing import Any, Mapping
from urllib.parse import urlencode

import requests

BASE_URL = "https://fapi.binance.com"
GET_ALLOWLIST = frozenset({
    "/fapi/v1/time", "/fapi/v3/account", "/fapi/v1/userTrades",
    "/fapi/v1/allOrders", "/fapi/v1/income", "/fapi/v1/commissionRate",
    "/fapi/v3/positionRisk",
})
_PARAMS = {
    "/fapi/v1/time": frozenset(),
    "/fapi/v3/account": frozenset({"recvWindow"}),
    "/fapi/v1/userTrades": frozenset({"symbol", "orderId", "startTime", "endTime", "fromId", "limit", "recvWindow"}),
    "/fapi/v1/allOrders": frozenset({"symbol", "orderId", "startTime", "endTime", "limit", "recvWindow"}),
    "/fapi/v1/income": frozenset({"symbol", "incomeType", "startTime", "endTime", "page", "limit", "recvWindow"}),
    "/fapi/v1/commissionRate": frozenset({"symbol", "recvWindow"}),
    "/fapi/v3/positionRisk": frozenset({"symbol", "recvWindow"}),
}


def redact_error(_value: object) -> str:
    """Full suppression is intentional: echoed secrets/URLs/headers are arbitrary."""
    return "remote or local error details suppressed"


class BinanceReadOnlyError(RuntimeError):
    def __init__(self, status: int | None, code: int | None, message: object = None):
        self.status = status if type(status) is int else None
        self.code = code if type(code) is int else None
        self.message = redact_error(message)
        super().__init__(f"Binance read failed: HTTP={self.status}, code={self.code}; {self.message}")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class BinanceExecutionTruthClient:
    def __init__(self, api_key: str, api_secret: str, *, timeout: float = 15.0,
                 max_attempts: int = 4, session=None, sleep=time.sleep):
        if not api_key or not api_secret:
            raise BinanceReadOnlyError(None, None)
        self._key = api_key
        self._secret = api_secret.encode("utf-8")
        self._timeout = timeout
        self._max_attempts = max(1, min(max_attempts, 5))
        self._session = session or requests.Session()
        self._session.trust_env = False
        self._sleep = sleep
        self._time_offset_ms = 0

    def server_now(self) -> datetime:
        return datetime.fromtimestamp(time.time() + self._time_offset_ms / 1000, timezone.utc)

    def sync_server_time(self) -> int:
        started = time.time() * 1000
        data = self._get("/fapi/v1/time", {})
        finished = time.time() * 1000
        if not isinstance(data, dict) or type(data.get("serverTime")) is not int:
            raise BinanceReadOnlyError(200, None)
        self._time_offset_ms = int(data["serverTime"] - (started + finished) / 2)
        return self._time_offset_ms

    def account(self):
        return self._get("/fapi/v3/account", {})

    def user_trades(self, *, symbol: str, **params):
        return self._get("/fapi/v1/userTrades", {"symbol": symbol, **params})

    def order_history(self, *, symbol: str | None = None, **params):
        return self._get("/fapi/v1/allOrders", {"symbol": symbol, **params})

    def income_history(self, **params):
        return self._get("/fapi/v1/income", params)

    def commission_rate(self, *, symbol: str):
        return self._get("/fapi/v1/commissionRate", {"symbol": symbol})

    def positions(self, *, symbol: str | None = None):
        return self._get("/fapi/v3/positionRisk", {"symbol": symbol})

    def _get(self, path: str, params: Mapping[str, Any]):
        if path not in GET_ALLOWLIST or set(params) - _PARAMS[path]:
            raise BinanceReadOnlyError(None, None)
        clean = {k: v for k, v in params.items() if v is not None}
        if path == "/fapi/v1/userTrades" and "fromId" in clean and {"startTime", "endTime"} & clean.keys():
            raise BinanceReadOnlyError(None, None)
        signed = path != "/fapi/v1/time"
        for attempt in range(self._max_attempts):
            query = dict(clean)
            headers = {}
            if signed:
                query.setdefault("recvWindow", 5000)
                query["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
                encoded = urlencode(query)
                query["signature"] = hmac.new(self._secret, encoded.encode(), hashlib.sha256).hexdigest()
                headers["X-MBX-APIKEY"] = self._key
            url = BASE_URL + path + ("?" + urlencode(query) if query else "")
            try:
                response = self._session.get(url, headers=headers, timeout=self._timeout, allow_redirects=False)
            except Exception:
                if attempt + 1 < self._max_attempts:
                    self._sleep(min(2 ** attempt, 30))
                    continue
                raise BinanceReadOnlyError(None, None) from None
            try:
                data = response.json()
            except Exception:
                data = None
            if response.status_code == 200:
                if not isinstance(data, (dict, list)):
                    raise BinanceReadOnlyError(200, None)
                return data
            code = data.get("code") if isinstance(data, dict) else None
            if code == -1021 and signed and attempt == 0:
                self.sync_server_time()
                continue
            if response.status_code in {418, 429} or response.status_code >= 500:
                if attempt + 1 < self._max_attempts:
                    try:
                        delay = float(response.headers.get("Retry-After", 2 ** attempt))
                    except (ValueError, TypeError):
                        delay = 2 ** attempt
                    # A long ban is surfaced; it is never retried before Retry-After.
                    if not math.isfinite(delay) or delay > 60:
                        raise BinanceReadOnlyError(response.status_code, code)
                    self._sleep(max(0, delay))
                    continue
            raise BinanceReadOnlyError(response.status_code, code) from None
        raise BinanceReadOnlyError(None, None)
