# BTC Market Data Oracle

**Prepaid market snapshots for autonomous trading agents (spot, perps, arbitrage).**

This is a hosted + open-source FastAPI service that sells low-latency BTC market data via **prepaid payment verifications** (a predictable budget agents can reason about).

- Hosted API: **https://oracle.satsgate.org**
- OpenAPI docs: **https://oracle.satsgate.org/docs**

## Hosted-first quickstart (recommended)

If you have an **NWC-capable wallet** (CoinOS, Alby Hub, etc.), the fastest path is the **Oracle Autopilot**.

It will:
- provision an API key automatically (first run)
- query `/v1/snapshot/btc` on an interval
- call the reasoning endpoints periodically
- optionally auto-top-up with guardrails

```bash
cd clients/node
npm install
cp .env.example .env
# set NWC_URL
npm run plug
```

This is the “plug & play” path for agent operators.

## 5 endpoints (v1) + costs

All endpoints require `X-Api-Key` and spend **verifications** (stored as `credits` internally).

- `GET /v1/price/btcusd` → spot price (Binance BTCUSDT) (**cost: 1 verification**)
- `GET /v1/volume/btcusd_24h` → 24h volume (Binance) (**cost: 1**)
- `GET /v1/liquidity/btcusd` → order book depth snapshot (Binance) (**cost: 1**)
- `GET /v1/perps/funding` → funding + open interest (Deribit BTC-PERPETUAL) (**cost: 1**)
- `GET /v1/snapshot/btc` → combined snapshot (**cost: 2**)

Every response includes:
- `staleness_ms` (freshness)
- `sources` (where data came from)
- `quote: "USDT"` (we treat BTC/USD as BTC/USDT in v1)
- `verifications_spent` + `verification_balance`

Tip: send `X-Request-Id` for idempotent spending (safe retries).

## Agent-native reasoning endpoints

These endpoints help an agent/operator reason about budget and spend:

- `GET /v1/usage/forecast` → when will I run out at the current pace?
- `GET /v1/recommendation/topup` → which plan should I buy to cover the next N days?
- `GET /v1/usage/by-endpoint` → where am I spending verifications?

## Pricing model (prepaid verifications)

Hosted pricing is prepaid **payment verifications** (stored as `credits` internally). They **do not expire**.

- Minimum top-up: **1000 sats → 200 verifications** (anti-abuse)
- Most endpoints cost **1 verification**
- `/v1/snapshot/btc` costs **2 verifications**

## Plug snippets (after you have an API key)

Python:

```py
import os, uuid, requests

BASE = "https://oracle.satsgate.org"
API_KEY = os.environ["ORACLE_API_KEY"]

r = requests.get(
    f"{BASE}/v1/snapshot/btc",
    headers={"X-Api-Key": API_KEY, "X-Request-Id": str(uuid.uuid4())},
    timeout=10,
)
print(r.json()["snapshot"]["price"]["price"], r.json()["quote"])
```

Node:

```js
const BASE = "https://oracle.satsgate.org";
const apiKey = process.env.ORACLE_API_KEY;

const r = await fetch(`${BASE}/v1/snapshot/btc`, {
  headers: { "X-Api-Key": apiKey, "X-Request-Id": crypto.randomUUID() },
});
const data = await r.json();
console.log(data.snapshot.price.price, data.quote);
```

## Billing implementation

Billing (prepaid verifications, ledger, top-ups) is **powered by satsgate (open-source)**.

## Advanced: Self-host (optional)

Self-hosting is mainly for **auditing**, custom infra, or special requirements.
Most operators should use the hosted API.

If you do want to self-host, Docker Compose + Caddy is included:

```bash
cp .env.example .env
# set ORACLE_HOSTNAME, ORACLE_WALLET_MODE=lnaddr, ORACLE_LIGHTNING_ADDRESS, ORACLE_MACAROON_SECRET, ORACLE_MARKET_MODE=live

docker compose up -d --build
```

---

License: Apache-2.0
