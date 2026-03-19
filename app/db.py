from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Client:
    id: int
    credits: int
    payee_lightning_address: str | None = None


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Enable foreign keys in SQLite
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str) -> None:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    with _connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS clients (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              api_key_hash TEXT NOT NULL UNIQUE,
              credits INTEGER NOT NULL DEFAULT 0,
              created_at INTEGER NOT NULL
            );
            """
        )

        # Migration (idempotent): add payee_lightning_address
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(clients)").fetchall()]
        if "payee_lightning_address" not in cols:
            conn.execute("ALTER TABLE clients ADD COLUMN payee_lightning_address TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ledger (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              client_id INTEGER NOT NULL,
              delta_credits INTEGER NOT NULL,
              reason TEXT NOT NULL,
              ref TEXT,
              created_at INTEGER NOT NULL,
              FOREIGN KEY(client_id) REFERENCES clients(id)
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS topups (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              payment_hash TEXT NOT NULL UNIQUE,
              invoice TEXT NOT NULL,
              sats INTEGER NOT NULL,
              credits INTEGER NOT NULL,
              status TEXT NOT NULL,
              client_id INTEGER,
              created_at INTEGER NOT NULL,
              settled_at INTEGER,
              FOREIGN KEY(client_id) REFERENCES clients(id)
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS requests (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              client_id INTEGER NOT NULL,
              request_id TEXT NOT NULL,
              endpoint TEXT NOT NULL,
              cost INTEGER NOT NULL,
              created_at INTEGER NOT NULL,
              UNIQUE(client_id, request_id),
              FOREIGN KEY(client_id) REFERENCES clients(id)
            );
            """
        )

        # Indexes (improve dashboard/reporting performance)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_client_id ON ledger(client_id, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_client_created ON ledger(client_id, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_topups_client_created ON topups(client_id, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_client_created ON requests(client_id, created_at)")


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def new_api_key() -> str:
    """Generate a new API key.

    Prefix is for identification only (not secret). Default: ORACLE_API_KEY_PREFIX=bmd_
    """

    prefix = os.environ.get("ORACLE_API_KEY_PREFIX", "bmd_")
    # Basic sanitation: keep it short and avoid whitespace
    prefix = (prefix or "bmd_").strip()
    if len(prefix) > 16:
        prefix = prefix[:16]

    return prefix + secrets.token_urlsafe(32)


def create_client(db_path: str) -> tuple[str, Client]:
    api_key = new_api_key()
    api_key_hash = hash_api_key(api_key)
    now = int(time.time())

    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO clients(api_key_hash, credits, created_at) VALUES(?, 0, ?)",
            (api_key_hash, now),
        )
        client_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO ledger(client_id, delta_credits, reason, ref, created_at) VALUES(?, ?, ?, ?, ?)",
            (client_id, 0, "client_created", None, now),
        )

    return api_key, Client(id=client_id, credits=0, payee_lightning_address=None)


def get_client_by_api_key(db_path: str, api_key: str) -> Client | None:
    if not api_key:
        return None
    h = hash_api_key(api_key)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, credits, payee_lightning_address FROM clients WHERE api_key_hash = ?",
            (h,),
        ).fetchone()
        if not row:
            return None
        return Client(
            id=int(row["id"]),
            credits=int(row["credits"]),
            payee_lightning_address=row["payee_lightning_address"],
        )


def add_topup(
    db_path: str,
    *,
    payment_hash: str,
    invoice: str,
    sats: int,
    credits: int,
    client_id: int | None,
) -> None:
    now = int(time.time())
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO topups(payment_hash, invoice, sats, credits, status, client_id, created_at)
            VALUES(?, ?, ?, ?, 'pending', ?, ?)
            """,
            (payment_hash, invoice, int(sats), int(credits), client_id, now),
        )


def get_topup(db_path: str, payment_hash: str) -> sqlite3.Row | None:
    with _connect(db_path) as conn:
        return conn.execute("SELECT * FROM topups WHERE payment_hash = ?", (payment_hash,)).fetchone()


def settle_topup_and_credit(
    db_path: str,
    *,
    payment_hash: str,
    client_id: int,
) -> dict:
    """Mark a topup as settled and add credits atomically.

    Returns: {credits_added, new_balance}
    """
    now = int(time.time())

    with _connect(db_path) as conn:
        # IMMEDIATE: take a write lock early (reduces double-credit races)
        conn.execute("BEGIN IMMEDIATE")

        topup = conn.execute(
            "SELECT * FROM topups WHERE payment_hash = ?",
            (payment_hash,),
        ).fetchone()

        if not topup:
            conn.execute("ROLLBACK")
            raise ValueError("topup not found")

        if topup["status"] == "settled":
            # idempotent: do not credit twice
            bal = conn.execute("SELECT credits FROM clients WHERE id = ?", (client_id,)).fetchone()
            conn.execute("COMMIT")
            return {"credits_added": 0, "new_balance": int(bal[0]) if bal else 0}

        credits_added = int(topup["credits"])

        # Link topup to the client if it was null
        existing_client_id = topup["client_id"]
        if existing_client_id is not None and int(existing_client_id) != int(client_id):
            conn.execute("ROLLBACK")
            raise ValueError("this topup belongs to a different client")

        conn.execute(
            "UPDATE topups SET status='settled', client_id = ?, settled_at = ? WHERE payment_hash = ?",
            (int(client_id), now, payment_hash),
        )

        conn.execute(
            "UPDATE clients SET credits = credits + ? WHERE id = ?",
            (credits_added, int(client_id)),
        )

        conn.execute(
            "INSERT INTO ledger(client_id, delta_credits, reason, ref, created_at) VALUES(?, ?, ?, ?, ?)",
            (int(client_id), credits_added, "topup_settled", payment_hash, now),
        )

        bal_row = conn.execute("SELECT credits FROM clients WHERE id = ?", (int(client_id),)).fetchone()
        new_balance = int(bal_row[0]) if bal_row else 0

        conn.execute("COMMIT")

    return {"credits_added": credits_added, "new_balance": new_balance}


def spend_credits(db_path: str, *, client_id: int, cost: int, reason: str, ref: str | None = None) -> int:
    """Spend credits if there is sufficient balance. Returns new balance."""
    now = int(time.time())
    cost = int(cost)
    if cost <= 0:
        return get_balance(db_path, client_id=client_id)

    with _connect(db_path) as conn:
        # IMMEDIATE: lock early so two requests can't spend the same balance concurrently
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT credits FROM clients WHERE id = ?", (int(client_id),)).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            raise ValueError("client does not exist")

        current = int(row[0])
        if current < cost:
            conn.execute("ROLLBACK")
            raise ValueError("insufficient balance")

        conn.execute(
            "UPDATE clients SET credits = credits - ? WHERE id = ?",
            (cost, int(client_id)),
        )
        conn.execute(
            "INSERT INTO ledger(client_id, delta_credits, reason, ref, created_at) VALUES(?, ?, ?, ?, ?)",
            (int(client_id), -cost, reason, ref, now),
        )
        new_balance_row = conn.execute("SELECT credits FROM clients WHERE id = ?", (int(client_id),)).fetchone()
        new_balance = int(new_balance_row[0]) if new_balance_row else 0
        conn.execute("COMMIT")
        return new_balance


def spend_credits_once(
    db_path: str,
    *,
    client_id: int,
    cost: int,
    endpoint: str,
    request_id: str | None,
) -> tuple[int, int]:
    """Spend credits for an API call.

    If `request_id` is provided, spending is idempotent per (client_id, request_id).

    Returns (charged, new_balance).
    """

    cost = int(cost)
    if cost <= 0:
        bal = get_balance(db_path, client_id=client_id)
        return 0, bal

    if not request_id:
        new_bal = spend_credits(db_path, client_id=client_id, cost=cost, reason=endpoint, ref=None)
        return cost, new_bal

    request_id = str(request_id).strip()
    if len(request_id) > 200:
        request_id = request_id[:200]

    now = int(time.time())

    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")

        existing = conn.execute(
            "SELECT id FROM requests WHERE client_id = ? AND request_id = ?",
            (int(client_id), request_id),
        ).fetchone()
        if existing:
            bal_row = conn.execute("SELECT credits FROM clients WHERE id = ?", (int(client_id),)).fetchone()
            bal = int(bal_row[0]) if bal_row else 0
            conn.execute("COMMIT")
            return 0, bal

        row = conn.execute("SELECT credits FROM clients WHERE id = ?", (int(client_id),)).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            raise ValueError("client does not exist")

        current = int(row[0])
        if current < cost:
            conn.execute("ROLLBACK")
            raise ValueError("insufficient balance")

        conn.execute(
            "UPDATE clients SET credits = credits - ? WHERE id = ?",
            (cost, int(client_id)),
        )
        conn.execute(
            "INSERT INTO ledger(client_id, delta_credits, reason, ref, created_at) VALUES(?, ?, ?, ?, ?)",
            (int(client_id), -cost, endpoint, request_id, now),
        )
        conn.execute(
            "INSERT INTO requests(client_id, request_id, endpoint, cost, created_at) VALUES(?, ?, ?, ?, ?)",
            (int(client_id), request_id, endpoint, cost, now),
        )

        new_balance_row = conn.execute("SELECT credits FROM clients WHERE id = ?", (int(client_id),)).fetchone()
        new_balance = int(new_balance_row[0]) if new_balance_row else 0

        conn.execute("COMMIT")
        return cost, new_balance


def get_balance(db_path: str, *, client_id: int) -> int:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT credits FROM clients WHERE id = ?", (int(client_id),)).fetchone()
        return int(row[0]) if row else 0
