# BTC Market Data Oracle

**Prepaid market snapshots for autonomous trading agents (spot, perps, arbitrage).**

BTC Market Data Oracle is a hosted + self-hostable FastAPI service that sells low-latency BTC market data via **prepaid payment verifications** (a predictable budget agents can reason about).

## What you get (v1)

5 endpoints (all prepaid):

- `GET /v1/price/btcusd` → spot price (Binance BTCUSDT) (**cost: 1 verification**)
- `GET /v1/volume/btcusd_24h` → 24h volume (Binance) (**cost: 1**)
- `GET /v1/liquidity/btcusd` → order book depth snapshot (Binance) (**cost: 1**)
- `GET /v1/perps/funding` → funding + open interest (Deribit BTC-PERPETUAL) (**cost: 1**)
- `GET /v1/snapshot/btc` → combined snapshot (**cost: 2**)

Every response includes:
- `staleness_ms` (freshness)
- `sources` (where data came from)
- `verification_balance` + `verifications_spent` (so an agent can budget)

## Pricing model (prepaid verifications)

Hosted pricing is prepaid **payment verifications** (stored as `credits` internally). They **do not expire**.

- Minimum top-up: **1000 sats → 200 verifications** (anti-abuse)
- Each successful API call consumes verifications:
  - most endpoints: **1**
  - `/v1/snapshot/btc`: **2**

Tip: send `X-Request-Id` for idempotent spending (safe retries).

## Agent-native operator endpoints

These endpoints help an agent/operator reason about budget and spend:

- `GET /v1/usage/forecast` → when will I run out at the current pace?
- `GET /v1/recommendation/topup` → which plan should I buy to cover the next N days?
- `GET /v1/usage/by-endpoint` → where am I spending verifications?

## API docs

- OpenAPI UI: `GET /docs`
- OpenAPI JSON: `GET /openapi.json`

## Quickstart (local)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

cp .env.example .env
# for local dev:
# ORACLE_WALLET_MODE=mock
# ORACLE_MARKET_MODE=mock

uvicorn app.main:app --reload --port 8000
```

## Getting an API key (top-up)

Top-ups use an L402-style flow:

1) `GET /v1/topup/trial` → returns **402** with `invoice + macaroon`
2) payer pays the invoice and obtains the **preimage** (NWC wallets can do this programmatically)
   - dev-only (mock wallet): `GET /dev/mock/pay/{payment_hash}` returns the preimage
3) retry with: `Authorization: L402 <macaroon>:<preimage>` → you receive an `api_key`

### Fastest path (NWC): Node demo client

If you have an NWC-capable wallet, you can run a working top-up + snapshot demo:

```bash
cd clients/node
npm install
cp .env.example .env
# set NWC_URL
npm run demo
```

## Running in production

Use Docker Compose + Caddy (TLS):

```bash
cp .env.example .env
# set ORACLE_HOSTNAME, ORACLE_WALLET_MODE=lnaddr, ORACLE_LIGHTNING_ADDRESS, ORACLE_MACAROON_SECRET, ORACLE_MARKET_MODE=live

docker compose up -d --build
```

## Data sources (v1)

- Binance Global spot (BTCUSDT) for price + 24h volume + depth
- Deribit (BTC-PERPETUAL) for funding + open interest

## Billing implementation

Billing (prepaid verifications, ledger, top-ups) is **powered by satsgate (open-source)**.

---

License: Apache-2.0
