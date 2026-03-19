from __future__ import annotations

import os

from dotenv import load_dotenv

# Load env vars from .env if present
load_dotenv()

SERVICE_NAME = "btc-market-data-oracle"

# --- Billing (prepaid payment verifications / credits) ---

# Secret used to sign L402 macaroons for top-ups.
# Change in production.
MACAROON_SECRET = os.environ.get("ORACLE_MACAROON_SECRET", "dev-change-me")

# Token TTL (seconds) for L402 challenges (top-ups).
TOKEN_TTL_SECONDS = int(os.environ.get("ORACLE_TOKEN_TTL_SECONDS", "600"))

# API key prefix (identification only; not secret)
API_KEY_PREFIX = os.environ.get("ORACLE_API_KEY_PREFIX", "bmd_").strip()

# Wallet backend
# - mock: simulated wallet (local testing)
# - lnaddr: generate invoices via Lightning Address (LNURL-pay)
WALLET_MODE = os.environ.get("ORACLE_WALLET_MODE", "mock").strip().lower()
LIGHTNING_ADDRESS = os.environ.get("ORACLE_LIGHTNING_ADDRESS", "").strip()

# Database (SQLite)
# If ORACLE_DB_PATH is empty, fall back to a local file.
_db_path_env = os.environ.get("ORACLE_DB_PATH", "").strip()
DB_PATH = _db_path_env or os.path.join(os.path.dirname(__file__), "..", "btc_market_data_oracle.sqlite3")

# Rate limit (simple in-memory MVP)
RL_ENABLED = os.environ.get("ORACLE_RL_ENABLED", "1") == "1"
RL_WINDOW_SECONDS = int(os.environ.get("ORACLE_RL_WINDOW_SECONDS", "60"))
RL_MAX_ANON = int(os.environ.get("ORACLE_RL_MAX_ANON", "60"))
RL_MAX_AUTH = int(os.environ.get("ORACLE_RL_MAX_AUTH", "2000"))

# Dev mode enables /dev/* endpoints (if any)
DEV_MODE = os.environ.get("ORACLE_DEV_MODE", "1") == "1"

# Optional operator/admin token.
# When set, enables /v1/admin/* endpoints (protected via X-Admin-Token header).
ADMIN_TOKEN = os.environ.get("ORACLE_ADMIN_TOKEN", "").strip()

# --- Market data sources ---

# For v1 we treat BTC/USD as BTC/USDT from Binance spot.
ASSET = "BTC"
QUOTE = "USDT"

MARKET_MODE = os.environ.get("ORACLE_MARKET_MODE", "live").strip().lower()

BINANCE_BASE_URL = os.environ.get("ORACLE_BINANCE_BASE_URL", "https://api.binance.com").rstrip("/")
BINANCE_SYMBOL = os.environ.get("ORACLE_BINANCE_SYMBOL", "BTCUSDT").strip().upper()
BINANCE_DEPTH_LIMIT = int(os.environ.get("ORACLE_BINANCE_DEPTH_LIMIT", "50"))

DERIBIT_BASE_URL = os.environ.get("ORACLE_DERIBIT_BASE_URL", "https://www.deribit.com").rstrip("/")
DERIBIT_INSTRUMENT = os.environ.get("ORACLE_DERIBIT_INSTRUMENT", "BTC-PERPETUAL").strip().upper()

# Fetch intervals (seconds)
BINANCE_TICKER_INTERVAL_SECONDS = float(os.environ.get("ORACLE_BINANCE_TICKER_INTERVAL_SECONDS", "1"))
BINANCE_DEPTH_INTERVAL_SECONDS = float(os.environ.get("ORACLE_BINANCE_DEPTH_INTERVAL_SECONDS", "1"))
DERIBIT_TICKER_INTERVAL_SECONDS = float(os.environ.get("ORACLE_DERIBIT_TICKER_INTERVAL_SECONDS", "2"))
