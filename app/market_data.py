from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import httpx

from . import config


@dataclass(frozen=True)
class CacheEntry:
    data: dict
    fetched_at: float
    source: str


class MarketDataCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, CacheEntry] = {}
        self._errors: dict[str, str] = {}

    def set(self, key: str, *, data: dict, source: str) -> None:
        with self._lock:
            self._entries[key] = CacheEntry(data=data, fetched_at=time.time(), source=source)
            self._errors.pop(key, None)

    def set_error(self, key: str, err: str) -> None:
        with self._lock:
            self._errors[key] = str(err)

    def get(self, key: str) -> CacheEntry | None:
        with self._lock:
            return self._entries.get(key)

    def snapshot_status(self) -> dict:
        with self._lock:
            out = {
                "mode": config.MARKET_MODE,
                "keys": {},
                "errors": dict(self._errors),
            }
            for k, e in self._entries.items():
                out["keys"][k] = {
                    "source": e.source,
                    "fetched_at": e.fetched_at,
                    "staleness_ms": int((time.time() - e.fetched_at) * 1000),
                }
            return out


CACHE = MarketDataCache()

KEY_BINANCE_TICKER_24H = "binance_ticker_24h"
KEY_BINANCE_DEPTH = "binance_depth"
KEY_DERIBIT_TICKER = "deribit_ticker"


def _http() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(5.0, connect=5.0))


def _fetch_binance_ticker_24h(client: httpx.Client) -> dict:
    r = client.get(
        f"{config.BINANCE_BASE_URL}/api/v3/ticker/24hr",
        params={"symbol": config.BINANCE_SYMBOL},
    )
    r.raise_for_status()
    j = r.json()

    # normalize types
    out = {
        "symbol": j.get("symbol"),
        "lastPrice": float(j.get("lastPrice")),
        "bidPrice": float(j.get("bidPrice")),
        "askPrice": float(j.get("askPrice")),
        "highPrice": float(j.get("highPrice")),
        "lowPrice": float(j.get("lowPrice")),
        "volume": float(j.get("volume")),
        "quoteVolume": float(j.get("quoteVolume")),
        "openTime": int(j.get("openTime")),
        "closeTime": int(j.get("closeTime")),
        "count": int(j.get("count")),
    }
    return out


def _fetch_binance_depth(client: httpx.Client) -> dict:
    r = client.get(
        f"{config.BINANCE_BASE_URL}/api/v3/depth",
        params={"symbol": config.BINANCE_SYMBOL, "limit": int(config.BINANCE_DEPTH_LIMIT)},
    )
    r.raise_for_status()
    j = r.json()

    bids_raw = j.get("bids") or []
    asks_raw = j.get("asks") or []

    def _levels(raw) -> list[tuple[float, float]]:
        out = []
        for p, q in raw:
            out.append((float(p), float(q)))
        return out

    return {
        "lastUpdateId": int(j.get("lastUpdateId")),
        "bids": _levels(bids_raw),
        "asks": _levels(asks_raw),
    }


def _fetch_deribit_ticker(client: httpx.Client) -> dict:
    r = client.get(
        f"{config.DERIBIT_BASE_URL}/api/v2/public/ticker",
        params={"instrument_name": config.DERIBIT_INSTRUMENT},
    )
    r.raise_for_status()
    j = r.json()
    res = j.get("result") or {}

    out = {
        "timestamp": int(res.get("timestamp")),
        "instrument_name": res.get("instrument_name"),
        "index_price": float(res.get("index_price")),
        "mark_price": float(res.get("mark_price")),
        "last_price": float(res.get("last_price")),
        "open_interest": float(res.get("open_interest")),
        "current_funding": float(res.get("current_funding")),
        "funding_8h": float(res.get("funding_8h")),
        "best_bid_price": float(res.get("best_bid_price")),
        "best_ask_price": float(res.get("best_ask_price")),
    }
    return out


def _loop(key: str, interval_seconds: float, fn) -> None:
    interval_seconds = max(0.2, float(interval_seconds))

    with _http() as client:
        while True:
            try:
                data = fn(client)
                CACHE.set(key, data=data, source=key.split("_")[0])
            except Exception as e:  # noqa: BLE001
                CACHE.set_error(key, str(e))
            time.sleep(interval_seconds)


_started = False


def start_background_fetchers() -> None:
    """Start background fetchers (idempotent)."""

    global _started
    if _started:
        return
    _started = True

    if config.MARKET_MODE == "mock":
        # Static values for tests/local dev.
        CACHE.set(
            KEY_BINANCE_TICKER_24H,
            data={
                "symbol": config.BINANCE_SYMBOL,
                "lastPrice": 70_000.0,
                "bidPrice": 69_999.5,
                "askPrice": 70_000.5,
                "highPrice": 72_000.0,
                "lowPrice": 68_000.0,
                "volume": 12_345.67,
                "quoteVolume": 900_000_000.0,
                "openTime": int(time.time() * 1000) - 86_400_000,
                "closeTime": int(time.time() * 1000),
                "count": 123456,
            },
            source="mock",
        )
        CACHE.set(
            KEY_BINANCE_DEPTH,
            data={
                "lastUpdateId": 1,
                "bids": [(69_999.0, 1.0), (69_998.0, 2.0), (69_990.0, 5.0)],
                "asks": [(70_001.0, 1.1), (70_002.0, 2.2), (70_010.0, 4.4)],
            },
            source="mock",
        )
        CACHE.set(
            KEY_DERIBIT_TICKER,
            data={
                "timestamp": int(time.time() * 1000),
                "instrument_name": config.DERIBIT_INSTRUMENT,
                "index_price": 70_000.0,
                "mark_price": 70_000.0,
                "last_price": 70_000.0,
                "open_interest": 1_000_000_000.0,
                "current_funding": 0.0,
                "funding_8h": 0.0,
                "best_bid_price": 69_999.0,
                "best_ask_price": 70_001.0,
            },
            source="mock",
        )
        return

    threads = [
        threading.Thread(
            target=_loop,
            args=(KEY_BINANCE_TICKER_24H, config.BINANCE_TICKER_INTERVAL_SECONDS, _fetch_binance_ticker_24h),
            daemon=True,
            name="fetch-binance-ticker",
        ),
        threading.Thread(
            target=_loop,
            args=(KEY_BINANCE_DEPTH, config.BINANCE_DEPTH_INTERVAL_SECONDS, _fetch_binance_depth),
            daemon=True,
            name="fetch-binance-depth",
        ),
        threading.Thread(
            target=_loop,
            args=(KEY_DERIBIT_TICKER, config.DERIBIT_TICKER_INTERVAL_SECONDS, _fetch_deribit_ticker),
            daemon=True,
            name="fetch-deribit-ticker",
        ),
    ]

    for t in threads:
        t.start()
