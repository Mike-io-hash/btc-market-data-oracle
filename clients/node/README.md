# Oracle Autopilot (NWC Plug & Play)

This folder contains a **production-usable reference client** for BTC Market Data Oracle.

It is designed for **autonomous trading agents** that have an NWC-capable wallet.

What it does:

- provisions an API key automatically (first run):
  - requests a top-up challenge (402 invoice+macaroon)
  - pays via **Nostr Wallet Connect (NWC)**
  - finalizes and saves `ORACLE_API_KEY`
- queries `/v1/snapshot/btc` on an interval
- periodically calls the agent-native reasoning endpoints:
  - `/v1/usage/forecast`
  - `/v1/recommendation/topup`
  - `/v1/usage/by-endpoint`
- optionally auto-top-ups with guardrails (caps + cooldown)

## Setup

```bash
cd clients/node
npm install
cp .env.example .env
# Edit .env and set NWC_URL
```

## Run

### Plug mode (loops)

```bash
npm run plug
```

### One-shot (quick test)

```bash
npm run once
```

## Safety / guardrails

This script can automatically spend sats via your wallet.

Defaults (editable in `.env`):
- `MAX_SINGLE_TOPUP_SATS=1000000`
- `MAX_TOPUP_SATS_PER_DAY=10000000`
- `TOPUP_COOLDOWN_SECONDS=300`

Tip: use a dedicated wallet (or a dedicated NWC budget) for oracle payments.

## Output

The autopilot prints JSON to stdout. You can parse it from any agent/runtime (Python/Go/etc)
by reading logs or piping stdout.

---

Do not paste `NWC_URL` or API keys into public issues.
