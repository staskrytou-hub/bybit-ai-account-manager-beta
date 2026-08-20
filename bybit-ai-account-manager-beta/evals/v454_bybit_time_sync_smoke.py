from __future__ import annotations

import json
import time
from unittest.mock import patch

from bybit_client import BybitClient


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        return False
    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def _header(req, name: str) -> str:
    value = req.get_header(name)
    if value is not None:
        return str(value)
    lname = name.lower()
    for key, value in req.header_items():
        if key.lower() == lname:
            return str(value)
    return ""


def main() -> None:
    BybitClient._clock_offsets_ms.clear()
    BybitClient._clock_synced_at.clear()
    seen_private_ts: list[int] = []

    def drifted_urlopen(req, timeout=0):
        url = req.full_url
        now_ms = int(time.time() * 1000)
        if "/v5/market/time" in url:
            return FakeResponse({"retCode": 0, "retMsg": "OK", "result": {"timeSecond": str((now_ms + 15000)//1000)}, "time": now_ms + 15000})
        ts = int(_header(req, "X-BAPI-TIMESTAMP"))
        seen_private_ts.append(ts)
        assert abs(ts - (now_ms + 15000)) < 1000, (ts, now_ms)
        return FakeResponse({"retCode": 0, "retMsg": "OK", "result": {"list": []}, "time": now_ms + 15000})

    client = BybitClient(authenticated=True, api_key="key", api_secret="secret")
    with patch("bybit_client.request.urlopen", side_effect=drifted_urlopen):
        client.get_unified_wallet("USDT")
    assert seen_private_ts, "private request was not signed"
    offset = BybitClient._clock_offsets_ms.get(client.base_url, 0.0)
    assert 14000 <= offset <= 16000, offset

    # Simulate a clock jump after the cache was established. retCode 10002 must force
    # a fresh server-time sync and retry exactly once with the new corrected timestamp.
    BybitClient._clock_offsets_ms.clear()
    BybitClient._clock_synced_at.clear()
    market_calls = 0
    private_calls = 0
    retry_timestamps: list[int] = []

    def retry_urlopen(req, timeout=0):
        nonlocal market_calls, private_calls
        url = req.full_url
        now_ms = int(time.time() * 1000)
        if "/v5/market/time" in url:
            market_calls += 1
            drift = 0 if market_calls == 1 else 12000
            return FakeResponse({"retCode": 0, "retMsg": "OK", "result": {"timeSecond": str((now_ms + drift)//1000)}, "time": now_ms + drift})
        private_calls += 1
        retry_timestamps.append(int(_header(req, "X-BAPI-TIMESTAMP")))
        if private_calls == 1:
            return FakeResponse({"retCode": 10002, "retMsg": "invalid request, please check your server timestamp or recv_window param", "result": {}, "time": now_ms + 12000})
        assert abs(retry_timestamps[-1] - (now_ms + 12000)) < 1000, retry_timestamps
        return FakeResponse({"retCode": 0, "retMsg": "OK", "result": {"list": []}, "time": now_ms + 12000})

    client2 = BybitClient(authenticated=True, api_key="key", api_secret="secret")
    with patch("bybit_client.request.urlopen", side_effect=retry_urlopen):
        client2.get_unified_wallet("USDT")
    assert private_calls == 2, private_calls
    assert market_calls == 2, market_calls

    print("v4.5.4 Bybit time-sync / retCode10002 recovery smoke: PASS")


if __name__ == "__main__":
    main()
