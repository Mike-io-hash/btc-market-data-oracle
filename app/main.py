from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse

from . import config, db, db_reports, market_data, plans
from .l402 import (
    L402Error,
    make_macaroon,
    parse_and_verify_macaroon,
    parse_authorization_header,
    verify_preimage_matches_payment_hash,
)
from .mock_wallet import MockWallet
from .rate_limit import RateLimiter
from .wallet_lnaddr import LightningAddressWallet

VERSION = "0.1.0"
TITLE = "BTC Market Data Oracle"
DESCRIPTION = (
    "Prepaid market snapshots for autonomous trading agents (spot, perps, arbitrage). "
    "Top up once, then query low-latency endpoints."
)

app = FastAPI(title=TITLE, version=VERSION, description=DESCRIPTION)


# --- Rate limiting ---

_ANON_RL = RateLimiter(window_seconds=config.RL_WINDOW_SECONDS, max_requests=config.RL_MAX_ANON)
_AUTH_RL = RateLimiter(window_seconds=config.RL_WINDOW_SECONDS, max_requests=config.RL_MAX_AUTH)


def _rate_limit(request: Request, *, is_auth: bool) -> JSONResponse | None:
    if not config.RL_ENABLED:
        return None

    host = None
    try:
        host = request.client.host  # type: ignore[union-attr]
    except Exception:
        host = None

    key = host or "unknown"
    limiter = _AUTH_RL if is_auth else _ANON_RL
    allowed, retry_after = limiter.allow(key)
    if allowed:
        return None

    return JSONResponse(
        status_code=429,
        headers={"Retry-After": str(retry_after)},
        content={"ok": False, "error": "rate_limited", "retry_after_seconds": retry_after},
    )


# --- Wallet ---


def _make_wallet():
    mode = config.WALLET_MODE
    if mode == "mock":
        return MockWallet()
    if mode == "lnaddr":
        if not config.LIGHTNING_ADDRESS:
            raise RuntimeError("ORACLE_LIGHTNING_ADDRESS is required when ORACLE_WALLET_MODE=lnaddr")
        return LightningAddressWallet(config.LIGHTNING_ADDRESS)
    raise RuntimeError(f"Invalid ORACLE_WALLET_MODE: {mode}")


WALLET = _make_wallet()


@app.on_event("startup")
def _startup() -> None:
    db.init_db(config.DB_PATH)
    market_data.start_background_fetchers()


# --- Helpers ---


def _get_client_from_api_key(x_api_key: str | None) -> db.Client | None:
    if not x_api_key:
        return None
    return db.get_client_by_api_key(config.DB_PATH, x_api_key)


def _require_client(x_api_key: str | None) -> db.Client | JSONResponse:
    client = _get_client_from_api_key(x_api_key)
    if not client:
        return JSONResponse(status_code=401, content={"ok": False, "error": "invalid_api_key"})
    return client


def _spend_or_402(
    *,
    client: db.Client,
    endpoint: str,
    cost: int,
    request_id: str | None,
) -> tuple[int, int] | JSONResponse:
    try:
        charged, new_balance = db.spend_credits_once(
            config.DB_PATH,
            client_id=client.id,
            cost=int(cost),
            endpoint=endpoint,
            request_id=request_id,
        )
        return charged, new_balance
    except ValueError as e:
        if "insufficient" in str(e).lower():
            return JSONResponse(
                status_code=402,
                content={
                    "ok": False,
                    "error": "insufficient_balance",
                    "hint": "Top up verifications: GET /v1/plans then /v1/topup/{plan_id}",
                },
            )
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})


def _staleness_ms(entry: market_data.CacheEntry) -> int:
    return int((time.time() - entry.fetched_at) * 1000)


# --- Core endpoints ---


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "service": config.SERVICE_NAME,
        "version": VERSION,
        "asset": config.ASSET,
        "quote": config.QUOTE,
        "receive_lightning_address": config.LIGHTNING_ADDRESS if config.WALLET_MODE == "lnaddr" else None,
        "market": market_data.CACHE.snapshot_status(),
    }


@app.get("/v1/plans")
def v1_plans(request: Request) -> Any:
    rl = _rate_limit(request, is_auth=False)
    if rl:
        return rl
    return {"ok": True, "plans": plans.list_plans()}


@app.get("/v1/topup/{plan_id}")
def v1_topup(
    plan_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    """Top up prepaid payment verifications (credits) by plan.

    - If there is NO Authorization => 402 + invoice + macaroon
    - If a valid L402 Authorization is provided => verifications are added and balance is returned (+ API key if new)

    Note: payment is verified *only* via the preimage (L402). We do not depend on wallet webhooks.
    """

    rl = _rate_limit(request, is_auth=bool(x_api_key))
    if rl:
        return rl

    try:
        plan = plans.get_plan(plan_id)
    except KeyError as e:
        return JSONResponse(status_code=404, content={"ok": False, "error": str(e)})

    resource = f"v1/topup/{plan.id}"

    client = _get_client_from_api_key(x_api_key)

    # Finalize (if Authorization present)
    if authorization:
        try:
            macaroon_b64, preimage_hex = parse_authorization_header(authorization)
            payload = parse_and_verify_macaroon(
                secret=config.MACAROON_SECRET,
                macaroon_b64=macaroon_b64,
                resource=resource,
            )
            payment_hash = payload["ph"]
            verify_preimage_matches_payment_hash(preimage_hex=preimage_hex, payment_hash=payment_hash)

            topup = db.get_topup(config.DB_PATH, payment_hash)
            if not topup:
                return JSONResponse(status_code=404, content={"ok": False, "error": "topup_not_found"})

            # If the topup is already linked to a client, respect it.
            topup_client_id = topup["client_id"]

            api_key_out: str | None = None
            if topup_client_id is not None:
                client_id = int(topup_client_id)
            elif client is not None:
                client_id = client.id
            else:
                api_key_out, new_client = db.create_client(config.DB_PATH)
                client_id = new_client.id

            res = db.settle_topup_and_credit(config.DB_PATH, payment_hash=payment_hash, client_id=client_id)

            out = {
                "ok": True,
                "plan": plan.to_dict(),
                "payment_hash": payment_hash,
                "verifications_added": res["credits_added"],
                "verification_balance": res["new_balance"],
                "client_id": client_id,
            }
            if api_key_out:
                out["api_key"] = api_key_out
                out["note"] = "Save this API key. We will not show it again."
            return out

        except L402Error as e:
            return JSONResponse(status_code=401, content={"ok": False, "error": str(e)})
        except ValueError as e:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})

    # Create challenge (402)
    memo = f"btc-market-data-oracle topup {plan.id}"
    inv = WALLET.create_invoice(amount_sats=plan.price_sats, memo=memo)

    # Persist pending topup
    try:
        db.add_topup(
            config.DB_PATH,
            payment_hash=inv.payment_hash,
            invoice=inv.invoice,
            sats=plan.price_sats,
            credits=plan.verifications,
            client_id=client.id if client else None,
        )
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"ok": False, "error": f"db_error: {e}"})

    macaroon = make_macaroon(
        secret=config.MACAROON_SECRET,
        payment_hash=inv.payment_hash,
        resource=resource,
        ttl_seconds=config.TOKEN_TTL_SECONDS,
    )

    www_auth = f'L402 macaroon="{macaroon}", invoice="{inv.invoice}"'

    return JSONResponse(
        status_code=402,
        headers={"WWW-Authenticate": www_auth},
        content={
            "ok": False,
            "error": "payment_required",
            "plan": plan.to_dict(),
            "macaroon": macaroon,
            "invoice": inv.invoice,
            "payment_hash": inv.payment_hash,
            "expires_at": getattr(inv, "expires_at", None),
            "hint": "Pay the invoice, then retry with Authorization: L402 <macaroon>:<preimage>",
        },
    )


@app.get("/v1/balance")
def v1_balance(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    rl = _rate_limit(request, is_auth=True)
    if rl:
        return rl

    client = _require_client(x_api_key)
    if isinstance(client, JSONResponse):
        return client

    return {
        "ok": True,
        "client_id": client.id,
        "verification_balance": int(client.credits),
    }


@app.get("/v1/ledger")
def v1_ledger(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    limit: int = Query(default=50, ge=1, le=200),
    before_id: int | None = Query(default=None),
):
    rl = _rate_limit(request, is_auth=True)
    if rl:
        return rl

    client = _require_client(x_api_key)
    if isinstance(client, JSONResponse):
        return client

    entries = db_reports.list_ledger(config.DB_PATH, client_id=client.id, limit=int(limit), before_id=before_id)

    # Rename fields for external API (verifications wording)
    for e in entries:
        e["delta_verifications"] = e.pop("delta_credits")

    return {"ok": True, "client_id": client.id, "entries": entries}


@app.get("/v1/usage/summary")
def v1_usage_summary(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    since_hours: int = Query(default=24, ge=1, le=24 * 30),
):
    rl = _rate_limit(request, is_auth=True)
    if rl:
        return rl

    client = _require_client(x_api_key)
    if isinstance(client, JSONResponse):
        return client

    since_ts = int(time.time()) - int(since_hours) * 3600
    out = db_reports.usage_summary(config.DB_PATH, client_id=client.id, since_ts=since_ts)

    return {"ok": True, "client_id": client.id, "summary": out}


@app.get("/v1/usage/daily")
def v1_usage_daily(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    days: int = Query(default=30, ge=1, le=366),
):
    rl = _rate_limit(request, is_auth=True)
    if rl:
        return rl

    client = _require_client(x_api_key)
    if isinstance(client, JSONResponse):
        return client

    out = db_reports.usage_daily(config.DB_PATH, client_id=client.id, days=int(days))
    return {"ok": True, "client_id": client.id, "daily": out}


@app.get("/v1/usage/forecast")
def v1_usage_forecast(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    lookback_hours: int = Query(default=24, ge=1, le=24 * 30),
):
    rl = _rate_limit(request, is_auth=True)
    if rl:
        return rl

    client = _require_client(x_api_key)
    if isinstance(client, JSONResponse):
        return client

    out = db_reports.usage_forecast(
        config.DB_PATH,
        client_id=client.id,
        current_balance_credits=int(client.credits),
        lookback_hours=int(lookback_hours),
    )

    return {"ok": True, "client_id": client.id, "forecast": out}


@app.get("/v1/usage/by-endpoint")
def v1_usage_by_endpoint(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    since_hours: int = Query(default=24, ge=1, le=24 * 30),
    limit: int = Query(default=50, ge=1, le=200),
):
    """Summarize spend grouped by endpoint.

    Helps agents/operators understand where verifications are being spent.
    """

    rl = _rate_limit(request, is_auth=True)
    if rl:
        return rl

    client = _require_client(x_api_key)
    if isinstance(client, JSONResponse):
        return client

    since_ts = int(time.time()) - int(since_hours) * 3600
    out = db_reports.usage_by_endpoint(
        config.DB_PATH,
        client_id=client.id,
        since_ts=since_ts,
        limit=int(limit),
    )

    return {
        "ok": True,
        "client_id": client.id,
        "since_hours": int(since_hours),
        **out,
    }


@app.get("/v1/recommendation/topup")
def v1_recommendation_topup(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    lookback_hours: int = Query(default=24, ge=1, le=24 * 30),
    target_days: int = Query(default=3, ge=1, le=365),
    buffer_hours: int = Query(default=12, ge=0, le=24 * 30),
    max_topups: int = Query(default=3, ge=1, le=50),
):
    """Recommend a top-up plan based on recent usage.

    This is agent-native sugar over:
    - `/v1/usage/forecast` (consumption rate)
    - `plans.recommend_purchase` (choose plan + quantity)

    Returns a recommendation only when recent usage suggests you'll run out.
    """

    from math import ceil

    rl = _rate_limit(request, is_auth=True)
    if rl:
        return rl

    client = _require_client(x_api_key)
    if isinstance(client, JSONResponse):
        return client

    current_balance = int(client.credits)

    forecast = db_reports.usage_forecast(
        config.DB_PATH,
        client_id=client.id,
        current_balance_credits=current_balance,
        lookback_hours=int(lookback_hours),
    )

    rate_per_hour = float(forecast.get("rate_credits_per_hour") or 0.0)
    horizon_hours = int(target_days) * 24 + int(buffer_hours)

    desired_verifications = int(ceil(rate_per_hour * float(horizon_hours))) if rate_per_hour > 0 else 0
    additional_needed = max(0, desired_verifications - current_balance)

    recommendation: dict | None = None
    if additional_needed > 0:
        recommendation = plans.recommend_purchase(additional_needed, max_topups=int(max_topups))
        if recommendation:
            recommendation["topup_path"] = f"/v1/topup/{recommendation['plan_id']}"
            recommendation["note"] = "Start the top-up by calling GET topup_path (expects 402 + invoice + macaroon)."

    status = forecast.get("status")
    if additional_needed == 0 and status in ("insufficient_data", "low_sample"):
        note = "Not enough usage history to recommend a top-up yet. Call again after you have more traffic."
    elif additional_needed == 0:
        note = "Current balance looks sufficient for the requested horizon."
    else:
        note = "Recommendation is based on your recent consumption rate."

    return {
        "ok": True,
        "client_id": client.id,
        "current_verification_balance": current_balance,
        "lookback_hours": int(lookback_hours),
        "target_days": int(target_days),
        "buffer_hours": int(buffer_hours),
        "horizon_hours": int(horizon_hours),
        "desired_verifications": int(desired_verifications),
        "additional_verifications_needed": int(additional_needed),
        "forecast": forecast,
        "recommendation": recommendation,
        "note": note,
    }


# --- Market endpoints (prepaid) ---


def _require_market_entry(key: str) -> market_data.CacheEntry | JSONResponse:
    entry = market_data.CACHE.get(key)
    if not entry:
        return JSONResponse(status_code=503, content={"ok": False, "error": "market_data_unavailable", "key": key})
    return entry


def _liquidity_metrics(depth: dict, *, window_bps: int = 10) -> dict:
    bids: list[tuple[float, float]] = depth.get("bids") or []
    asks: list[tuple[float, float]] = depth.get("asks") or []

    if not bids or not asks:
        return {"ok": False, "error": "empty_orderbook"}

    bid_p, bid_q = bids[0]
    ask_p, ask_q = asks[0]

    mid = (bid_p + ask_p) / 2.0
    spread = ask_p - bid_p
    spread_bps = (spread / mid) * 10_000.0 if mid > 0 else None

    lower = mid * (1.0 - (window_bps / 10_000.0))
    upper = mid * (1.0 + (window_bps / 10_000.0))

    bid_qty = 0.0
    bid_notional = 0.0
    bid_levels = 0
    for p, q in bids:
        if p < lower:
            break
        bid_levels += 1
        bid_qty += q
        bid_notional += p * q

    ask_qty = 0.0
    ask_notional = 0.0
    ask_levels = 0
    for p, q in asks:
        if p > upper:
            break
        ask_levels += 1
        ask_qty += q
        ask_notional += p * q

    return {
        "mid_price": mid,
        "top_of_book": {
            "best_bid_price": bid_p,
            "best_bid_qty": bid_q,
            "best_ask_price": ask_p,
            "best_ask_qty": ask_q,
            "spread": spread,
            "spread_bps": spread_bps,
        },
        "depth_window_bps": int(window_bps),
        "bid_depth": {"qty_btc": bid_qty, "notional_quote": bid_notional, "levels": bid_levels},
        "ask_depth": {"qty_btc": ask_qty, "notional_quote": ask_notional, "levels": ask_levels},
    }


@app.get("/v1/price/btcusd")
def v1_price_btcusd(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
):
    rl = _rate_limit(request, is_auth=True)
    if rl:
        return rl

    client = _require_client(x_api_key)
    if isinstance(client, JSONResponse):
        return client

    spend = _spend_or_402(client=client, endpoint="price_btcusd", cost=1, request_id=x_request_id)
    if isinstance(spend, JSONResponse):
        return spend
    charged, new_balance = spend

    ticker = _require_market_entry(market_data.KEY_BINANCE_TICKER_24H)
    if isinstance(ticker, JSONResponse):
        return ticker

    deribit = market_data.CACHE.get(market_data.KEY_DERIBIT_TICKER)

    price = float(ticker.data["lastPrice"])

    out = {
        "ok": True,
        "asset": config.ASSET,
        "quote": config.QUOTE,
        "price": price,
        "staleness_ms": _staleness_ms(ticker),
        "sources": {
            "binance_spot": {
                "symbol": ticker.data.get("symbol"),
                "lastPrice": ticker.data.get("lastPrice"),
                "bidPrice": ticker.data.get("bidPrice"),
                "askPrice": ticker.data.get("askPrice"),
                "closeTime": ticker.data.get("closeTime"),
                "staleness_ms": _staleness_ms(ticker),
            },
        },
        "verifications_spent": charged,
        "verification_balance": new_balance,
        "request_id": x_request_id,
    }

    if deribit:
        try:
            idx = float(deribit.data.get("index_price"))
            dev_bps = ((price - idx) / idx) * 10_000.0 if idx else None
        except Exception:
            dev_bps = None

        out["sources"]["deribit"] = {
            "instrument": deribit.data.get("instrument_name"),
            "index_price": deribit.data.get("index_price"),
            "mark_price": deribit.data.get("mark_price"),
            "timestamp": deribit.data.get("timestamp"),
            "staleness_ms": _staleness_ms(deribit),
        }
        out["deviation_bps_binance_vs_deribit_index"] = dev_bps

    return out


@app.get("/v1/volume/btcusd_24h")
def v1_volume_btcusd_24h(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
):
    rl = _rate_limit(request, is_auth=True)
    if rl:
        return rl

    client = _require_client(x_api_key)
    if isinstance(client, JSONResponse):
        return client

    spend = _spend_or_402(client=client, endpoint="volume_btcusd_24h", cost=1, request_id=x_request_id)
    if isinstance(spend, JSONResponse):
        return spend
    charged, new_balance = spend

    ticker = _require_market_entry(market_data.KEY_BINANCE_TICKER_24H)
    if isinstance(ticker, JSONResponse):
        return ticker

    return {
        "ok": True,
        "asset": config.ASSET,
        "quote": config.QUOTE,
        "volume_24h_base": float(ticker.data["volume"]),
        "volume_24h_quote": float(ticker.data["quoteVolume"]),
        "staleness_ms": _staleness_ms(ticker),
        "sources": {
            "binance_spot_24h": {
                "symbol": ticker.data.get("symbol"),
                "volume": ticker.data.get("volume"),
                "quoteVolume": ticker.data.get("quoteVolume"),
                "closeTime": ticker.data.get("closeTime"),
                "staleness_ms": _staleness_ms(ticker),
            }
        },
        "verifications_spent": charged,
        "verification_balance": new_balance,
        "request_id": x_request_id,
    }


@app.get("/v1/liquidity/btcusd")
def v1_liquidity_btcusd(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
):
    rl = _rate_limit(request, is_auth=True)
    if rl:
        return rl

    client = _require_client(x_api_key)
    if isinstance(client, JSONResponse):
        return client

    spend = _spend_or_402(client=client, endpoint="liquidity_btcusd", cost=1, request_id=x_request_id)
    if isinstance(spend, JSONResponse):
        return spend
    charged, new_balance = spend

    depth = _require_market_entry(market_data.KEY_BINANCE_DEPTH)
    if isinstance(depth, JSONResponse):
        return depth

    metrics = _liquidity_metrics(depth.data, window_bps=10)

    return {
        "ok": True,
        "asset": config.ASSET,
        "quote": config.QUOTE,
        "liquidity": metrics,
        "staleness_ms": _staleness_ms(depth),
        "sources": {
            "binance_orderbook": {
                "symbol": config.BINANCE_SYMBOL,
                "limit": int(config.BINANCE_DEPTH_LIMIT),
                "lastUpdateId": depth.data.get("lastUpdateId"),
                "staleness_ms": _staleness_ms(depth),
            }
        },
        "verifications_spent": charged,
        "verification_balance": new_balance,
        "request_id": x_request_id,
    }


@app.get("/v1/perps/funding")
def v1_perps_funding(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
):
    rl = _rate_limit(request, is_auth=True)
    if rl:
        return rl

    client = _require_client(x_api_key)
    if isinstance(client, JSONResponse):
        return client

    spend = _spend_or_402(client=client, endpoint="perps_funding", cost=1, request_id=x_request_id)
    if isinstance(spend, JSONResponse):
        return spend
    charged, new_balance = spend

    t = _require_market_entry(market_data.KEY_DERIBIT_TICKER)
    if isinstance(t, JSONResponse):
        return t

    return {
        "ok": True,
        "venue": "deribit",
        "instrument": t.data.get("instrument_name"),
        "index_price": t.data.get("index_price"),
        "mark_price": t.data.get("mark_price"),
        "current_funding": t.data.get("current_funding"),
        "funding_8h": t.data.get("funding_8h"),
        "open_interest": t.data.get("open_interest"),
        "timestamp": t.data.get("timestamp"),
        "staleness_ms": _staleness_ms(t),
        "sources": {
            "deribit_ticker": {
                "instrument": t.data.get("instrument_name"),
                "timestamp": t.data.get("timestamp"),
                "staleness_ms": _staleness_ms(t),
            }
        },
        "verifications_spent": charged,
        "verification_balance": new_balance,
        "request_id": x_request_id,
    }


@app.get("/v1/snapshot/btc")
def v1_snapshot_btc(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
):
    """Combined snapshot.

    Costs 2 verifications.
    """

    rl = _rate_limit(request, is_auth=True)
    if rl:
        return rl

    client = _require_client(x_api_key)
    if isinstance(client, JSONResponse):
        return client

    spend = _spend_or_402(client=client, endpoint="snapshot_btc", cost=2, request_id=x_request_id)
    if isinstance(spend, JSONResponse):
        return spend
    charged, new_balance = spend

    ticker = _require_market_entry(market_data.KEY_BINANCE_TICKER_24H)
    if isinstance(ticker, JSONResponse):
        return ticker

    depth = _require_market_entry(market_data.KEY_BINANCE_DEPTH)
    if isinstance(depth, JSONResponse):
        return depth

    deribit = _require_market_entry(market_data.KEY_DERIBIT_TICKER)
    if isinstance(deribit, JSONResponse):
        return deribit

    price = float(ticker.data["lastPrice"])
    idx = float(deribit.data.get("index_price"))
    dev_bps = ((price - idx) / idx) * 10_000.0 if idx else None

    liquidity = _liquidity_metrics(depth.data, window_bps=10)

    return {
        "ok": True,
        "asset": config.ASSET,
        "quote": config.QUOTE,
        "snapshot": {
            "price": {
                "price": price,
                "staleness_ms": _staleness_ms(ticker),
            },
            "volume_24h": {
                "volume_24h_base": float(ticker.data["volume"]),
                "volume_24h_quote": float(ticker.data["quoteVolume"]),
                "staleness_ms": _staleness_ms(ticker),
            },
            "liquidity": {
                "data": liquidity,
                "staleness_ms": _staleness_ms(depth),
            },
            "perps": {
                "venue": "deribit",
                "instrument": deribit.data.get("instrument_name"),
                "index_price": deribit.data.get("index_price"),
                "mark_price": deribit.data.get("mark_price"),
                "current_funding": deribit.data.get("current_funding"),
                "funding_8h": deribit.data.get("funding_8h"),
                "open_interest": deribit.data.get("open_interest"),
                "timestamp": deribit.data.get("timestamp"),
                "staleness_ms": _staleness_ms(deribit),
            },
            "deviation_bps_binance_vs_deribit_index": dev_bps,
        },
        "verifications_spent": charged,
        "verification_balance": new_balance,
        "request_id": x_request_id,
    }


# --- DEV helpers (mock wallet) ---


@app.get("/dev/mock/pay/{payment_hash}")
def dev_mock_pay(payment_hash: str):
    """DEV ONLY: if the wallet backend is mock, return the preimage for payment_hash."""
    if not config.DEV_MODE:
        return JSONResponse(status_code=404, content={"ok": False, "error": "not_found"})

    if not hasattr(WALLET, "dev_get_preimage"):
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": "wallet_backend_does_not_support_mock_pay",
                "hint": "Use ORACLE_WALLET_MODE=mock for this endpoint.",
            },
        )

    preimage_hex = WALLET.dev_get_preimage(payment_hash)
    if not preimage_hex:
        return JSONResponse(status_code=404, content={"ok": False, "error": "payment_hash_not_found_or_expired"})

    return {"ok": True, "payment_hash": payment_hash, "preimage": preimage_hex}
