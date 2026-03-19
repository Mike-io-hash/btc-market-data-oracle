import os
import sys
import tempfile
import uuid
from pathlib import Path

# Ensure the project root is importable no matter where pytest is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Set env BEFORE importing the app
DB_PATH = os.path.join(tempfile.gettempdir(), f"btc-oracle-test-{uuid.uuid4().hex}.sqlite3")
os.environ.setdefault("ORACLE_DB_PATH", DB_PATH)
os.environ.setdefault("ORACLE_WALLET_MODE", "mock")
os.environ.setdefault("ORACLE_MARKET_MODE", "mock")
os.environ.setdefault("ORACLE_MACAROON_SECRET", "test-secret")
os.environ.setdefault("ORACLE_TOKEN_TTL_SECONDS", "600")
os.environ.setdefault("ORACLE_API_KEY_PREFIX", "bmd_")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import WALLET, app  # noqa: E402


def test_health_and_plans():
    c = TestClient(app)

    r = c.get("/health")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["service"] == "btc-market-data-oracle"

    r = c.get("/v1/plans")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert isinstance(j["plans"], list)


def test_mock_topup_and_query_spends_verifications():
    c = TestClient(app)

    # Request topup challenge
    r = c.get("/v1/topup/trial")
    assert r.status_code == 402
    j = r.json()
    assert j["error"] == "payment_required"
    payment_hash = j["payment_hash"]
    macaroon = j["macaroon"]

    # Simulate payment (mock wallet)
    preimage = WALLET.dev_get_preimage(payment_hash)
    assert preimage is not None

    # Finalize topup
    r2 = c.get(
        "/v1/topup/trial",
        headers={"Authorization": f"L402 {macaroon}:{preimage}"},
    )
    assert r2.status_code == 200
    j2 = r2.json()
    assert j2["ok"] is True
    assert j2["verifications_added"] == 200
    api_key = j2["api_key"]

    # Call an endpoint (should spend 1)
    r3 = c.get("/v1/price/btcusd", headers={"X-Api-Key": api_key, "X-Request-Id": "req-1"})
    assert r3.status_code == 200
    j3 = r3.json()
    assert j3["ok"] is True
    assert j3["verifications_spent"] in (0, 1)

    # Same request id should not double-charge
    r4 = c.get("/v1/price/btcusd", headers={"X-Api-Key": api_key, "X-Request-Id": "req-1"})
    assert r4.status_code == 200
    j4 = r4.json()
    assert j4["ok"] is True
    assert j4["verifications_spent"] == 0

    # Agent-native endpoints
    r_usage = c.get("/v1/usage/by-endpoint?since_hours=24", headers={"X-Api-Key": api_key})
    assert r_usage.status_code == 200
    j_usage = r_usage.json()
    assert j_usage["ok"] is True
    assert isinstance(j_usage.get("endpoints"), list)

    r_rec = c.get("/v1/recommendation/topup?target_days=3", headers={"X-Api-Key": api_key})
    assert r_rec.status_code == 200
    j_rec = r_rec.json()
    assert j_rec["ok"] is True
    assert "forecast" in j_rec
