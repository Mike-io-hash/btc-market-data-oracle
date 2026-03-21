#!/usr/bin/env python3

import argparse
import os
import sys
import uuid

import requests
from dotenv import load_dotenv


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    if v is None:
        return default
    v = v.strip()
    return v or default


def _req(base: str, api_key: str, path: str):
    rid = f"py-demo-{uuid.uuid4().hex}"
    r = requests.get(
        base + path,
        headers={
            "X-Api-Key": api_key,
            "X-Request-Id": rid,
        },
        timeout=10,
    )
    try:
        j = r.json()
    except Exception:
        j = {"raw": r.text}

    if r.status_code >= 400:
        raise RuntimeError(f"GET {path} -> {r.status_code}: {j}")

    return j


def main():
    ap = argparse.ArgumentParser(
        description="BTC Market Data Oracle — Python demo client (query + reasoning)."
    )
    ap.add_argument(
        "--base-url",
        default=None,
        help="Oracle base URL (default from ORACLE_BASE_URL or https://oracle.satsgate.org)",
    )
    ap.add_argument(
        "--api-key",
        default=None,
        help="API key (default from ORACLE_API_KEY)",
    )
    ap.add_argument("--snapshot", action="store_true", help="Call /v1/snapshot/btc")
    ap.add_argument("--price", action="store_true", help="Call /v1/price/btcusd")
    ap.add_argument("--reasoning", action="store_true", help="Call forecast + recommendation + by-endpoint")

    args = ap.parse_args()

    load_dotenv()

    base = (args.base_url or _env("ORACLE_BASE_URL") or "https://oracle.satsgate.org").rstrip("/")
    api_key = args.api_key or _env("ORACLE_API_KEY")

    if not api_key:
        print("Missing ORACLE_API_KEY.\n")
        print("Fastest path:")
        print("- Use the Node NWC demo client to top up and get an API key:")
        print("  cd clients/node && npm install && cp .env.example .env && npm run demo")
        print("- Then set ORACLE_API_KEY in clients/python/.env and rerun this script.")
        sys.exit(2)

    do_any = args.snapshot or args.price or args.reasoning
    if not do_any:
        # sensible default
        args.snapshot = True
        args.reasoning = True

    print(f"BASE={base}")

    if args.snapshot:
        snap = _req(base, api_key, "/v1/snapshot/btc")
        print("\nSNAPSHOT:")
        print(
            f"  verifications_spent={snap.get('verifications_spent')} balance={snap.get('verification_balance')} quote={snap.get('quote')}"
        )
        # show key fields
        s = (snap.get("snapshot") or {})
        price = (s.get("price") or {}).get("price")
        funding_8h = (s.get("perps") or {}).get("funding_8h")
        oi = (s.get("perps") or {}).get("open_interest")
        print(f"  price={price} funding_8h={funding_8h} open_interest={oi}")

    if args.price:
        j = _req(base, api_key, "/v1/price/btcusd")
        print("\nPRICE:")
        print(
            f"  price={j.get('price')} quote={j.get('quote')} spent={j.get('verifications_spent')} balance={j.get('verification_balance')}"
        )

    if args.reasoning:
        bal = _req(base, api_key, "/v1/balance")
        print("\nBALANCE:")
        print(f"  {bal}")

        by = _req(base, api_key, "/v1/usage/by-endpoint?since_hours=24")
        print("\nBY_ENDPOINT (top 10):")
        for e in (by.get("endpoints") or [])[:10]:
            print(
                f"  {e.get('endpoint')}: spent={e.get('verifications_spent')} events={e.get('spend_events')}"
            )

        fc = _req(base, api_key, "/v1/usage/forecast?lookback_hours=24")
        print("\nFORECAST:")
        print(f"  status={fc.get('forecast',{}).get('status')} est_depletion={fc.get('forecast',{}).get('estimated_depletion_iso')}")

        rec = _req(base, api_key, "/v1/recommendation/topup?target_days=3&buffer_hours=12")
        print("\nRECOMMENDATION:")
        print(f"  {rec.get('recommendation')}")


if __name__ == "__main__":
    main()
