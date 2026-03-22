# Agent Quickstart (hosted or self-hosted)

This guide is for **autonomous trading agents** integrating BTC Market Data Oracle.

## 0) Requirements

- You can make HTTP requests.
- You can pay Lightning invoices and obtain the **preimage** (recommended: an **NWC-capable** wallet).

### Optional (recommended): Oracle Autopilot (Plug & Play)

If you have an NWC wallet, the fastest way to top-up + query is the Oracle Autopilot reference client:

```bash
cd clients/node
npm install
cp .env.example .env
# set NWC_URL
npm run plug
```

### Python demo client (query + reasoning)

If your agent is in Python, `clients/python` provides a simple query + reasoning demo.
(It assumes you already have an API key, e.g. from the Node NWC demo.)

## 1) Top up and get an API key

Pick a plan:

```bash
curl -sS http://127.0.0.1:8000/v1/plans | jq
```

Buy the `trial` plan (1000 sats → 200 verifications, no expiry):

```bash
curl -i http://127.0.0.1:8000/v1/topup/trial
```

You’ll get a **402** with:
- `invoice`
- `macaroon`
- `payment_hash`

Pay the invoice (via your wallet/NWC) and obtain the **preimage**, then finalize.

Dev-only (mock wallet):

```bash
curl -sS http://127.0.0.1:8000/dev/mock/pay/<payment_hash> | jq
```

Then finalize:

```bash
curl -sS -H 'Authorization: L402 <macaroon>:<preimage>' http://127.0.0.1:8000/v1/topup/trial | jq
```

If this is your first top-up, the response includes your `api_key`. Save it.

## 2) Call endpoints (spends verifications)

```bash
API_KEY='bmd_...'

curl -sS -H "X-Api-Key: $API_KEY" http://127.0.0.1:8000/v1/price/btcusd | jq
curl -sS -H "X-Api-Key: $API_KEY" http://127.0.0.1:8000/v1/snapshot/btc | jq
```

### Safe retries (recommended)

Include a unique `X-Request-Id` so retries don’t double-charge:

```bash
curl -sS \
  -H "X-Api-Key: $API_KEY" \
  -H "X-Request-Id: my-agent-req-123" \
  http://127.0.0.1:8000/v1/snapshot/btc | jq
```

## Notes

- `/v1/snapshot/btc` costs **2 verifications**.
- Everything else costs **1 verification**.
- For low latency, the service fetches exchange data in the background and serves snapshots from cache.

## 3) Budget reasoning (agent-native)

Forecast (when will I run out?):

```bash
curl -sS -H "X-Api-Key: $API_KEY" http://127.0.0.1:8000/v1/usage/forecast | jq
```

Recommendation (what to buy to cover the next N days?):

```bash
curl -sS -H "X-Api-Key: $API_KEY" \
  "http://127.0.0.1:8000/v1/recommendation/topup?target_days=3&buffer_hours=12" | jq
```

Spend by endpoint (what am I spending on?):

```bash
curl -sS -H "X-Api-Key: $API_KEY" \
  "http://127.0.0.1:8000/v1/usage/by-endpoint?since_hours=24" | jq
```
