# Agent Quickstart (hosted)

This guide is for **autonomous trading agents** integrating BTC Market Data Oracle.

Hosted API: https://oracle.satsgate.org

## 0) Fastest path (recommended): Oracle Autopilot (NWC Plug & Play)

If your agent has an NWC-capable wallet (CoinOS, Alby Hub, etc.), run the Oracle Autopilot reference client.
It provisions an API key automatically and can keep your balance topped up with guardrails.

```bash
cd clients/node
npm install
cp .env.example .env
# set NWC_URL
npm run plug
```

## 1) Manual top-up flow (L402 style)

If you want to integrate without the autopilot, top-ups work like this:

1) `GET /v1/topup/trial` → returns **402** with `invoice + macaroon + payment_hash`
2) pay the invoice and obtain the **preimage** (NWC wallets can do this programmatically)
3) retry with: `Authorization: L402 <macaroon>:<preimage>` → you receive an `api_key`

## 2) Call endpoints (spends verifications)

```bash
API_KEY='bmd_...'

curl -sS -H "X-Api-Key: $API_KEY" https://oracle.satsgate.org/v1/price/btcusd | jq
curl -sS -H "X-Api-Key: $API_KEY" https://oracle.satsgate.org/v1/snapshot/btc | jq
```

### Safe retries (recommended)

Include a unique `X-Request-Id` so retries don’t double-charge:

```bash
curl -sS \
  -H "X-Api-Key: $API_KEY" \
  -H "X-Request-Id: my-agent-req-123" \
  https://oracle.satsgate.org/v1/snapshot/btc | jq
```

## 3) Budget reasoning (agent-native)

Forecast (when will I run out?):

```bash
curl -sS -H "X-Api-Key: $API_KEY" https://oracle.satsgate.org/v1/usage/forecast | jq
```

Recommendation (what to buy to cover the next N days?):

```bash
curl -sS -H "X-Api-Key: $API_KEY" \
  "https://oracle.satsgate.org/v1/recommendation/topup?target_days=3&buffer_hours=12" | jq
```

Spend by endpoint (what am I spending on?):

```bash
curl -sS -H "X-Api-Key: $API_KEY" \
  "https://oracle.satsgate.org/v1/usage/by-endpoint?since_hours=24" | jq
```
