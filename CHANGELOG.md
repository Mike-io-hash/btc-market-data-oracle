# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-03-19

### Added
- Prepaid payment verifications (credits): plans + top-ups + balance + ledger + usage + forecast
- Market endpoints:
  - `GET /v1/price/btcusd`
  - `GET /v1/volume/btcusd_24h`
  - `GET /v1/liquidity/btcusd`
  - `GET /v1/perps/funding`
  - `GET /v1/snapshot/btc`
- Background fetchers + in-memory cache (Binance + Deribit)
- Optional idempotency via `X-Request-Id` (safe retries without double-charging)
