# BTC Market Data Oracle — Node client (NWC demo)

This folder contains a small Node.js demo client that:

- requests a `trial` top-up (402 with invoice+macaroon)
- pays the invoice via **Nostr Wallet Connect (NWC)**
- finalizes the top-up (returns an `api_key`)
- queries `/v1/snapshot/btc`

## Requirements

- Node.js 18+ (Node 20/22 recommended)
- An NWC-capable wallet (CoinOS, Alby Hub, etc.)

## Setup

```bash
cd clients/node
npm install
cp .env.example .env
# Edit .env and set NWC_URL
```

## Run

```bash
npm run demo
```

Notes:
- This script will pay the `trial` invoice (1000 sats).
- Do not paste your `NWC_URL` or API keys in public issues.
