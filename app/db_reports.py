from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .db import _connect


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def list_ledger(
    db_path: str,
    *,
    client_id: int,
    limit: int = 50,
    before_id: int | None = None,
) -> list[dict]:
    """Return ledger entries for a client."""

    limit = max(1, min(int(limit), 200))

    sql = "SELECT id, delta_credits, reason, ref, created_at FROM ledger WHERE client_id = ? "
    params: list = [int(client_id)]

    if before_id is not None:
        sql += "AND id < ? "
        params.append(int(before_id))

    sql += "ORDER BY id DESC LIMIT ?"
    params.append(limit)

    with _connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()

    out: list[dict] = []
    for r in rows:
        ts = int(r["created_at"])
        out.append(
            {
                "id": int(r["id"]),
                "delta_credits": int(r["delta_credits"]),
                "reason": r["reason"],
                "ref": r["ref"],
                "created_at": ts,
                "created_at_iso": _iso(ts),
            }
        )
    return out


def usage_summary(
    db_path: str,
    *,
    client_id: int,
    since_ts: int,
) -> dict:
    """Usage summary for dashboards.

    We treat **any negative delta** as consumption (payment verifications spent).
    """

    since_ts = int(since_ts)

    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN delta_credits > 0 THEN delta_credits ELSE 0 END), 0) AS credits_in,
              COALESCE(SUM(CASE WHEN delta_credits < 0 THEN -delta_credits ELSE 0 END), 0) AS credits_out,
              COALESCE(SUM(delta_credits), 0) AS net_credits,
              COALESCE(SUM(CASE WHEN delta_credits < 0 THEN 1 ELSE 0 END), 0) AS spend_events,
              COALESCE(SUM(CASE WHEN reason = 'topup_settled' THEN 1 ELSE 0 END), 0) AS topup_events
            FROM ledger
            WHERE client_id = ? AND created_at >= ?
            """,
            (int(client_id), since_ts),
        ).fetchone()

        rows = conn.execute(
            """
            SELECT
              reason,
              COUNT(*) AS events,
              COALESCE(SUM(delta_credits), 0) AS net_credits,
              COALESCE(SUM(CASE WHEN delta_credits > 0 THEN delta_credits ELSE 0 END), 0) AS credits_in,
              COALESCE(SUM(CASE WHEN delta_credits < 0 THEN -delta_credits ELSE 0 END), 0) AS credits_out
            FROM ledger
            WHERE client_id = ? AND created_at >= ?
            GROUP BY reason
            ORDER BY events DESC
            """,
            (int(client_id), since_ts),
        ).fetchall()

    by_reason = []
    for r in rows:
        by_reason.append(
            {
                "reason": r["reason"],
                "events": int(r["events"]),
                "net_credits": int(r["net_credits"]),
                "credits_in": int(r["credits_in"]),
                "credits_out": int(r["credits_out"]),
            }
        )

    return {
        "since_ts": since_ts,
        "since_iso": _iso(since_ts),
        "credits_in": int(row["credits_in"]),
        "credits_out": int(row["credits_out"]),
        "net_credits": int(row["net_credits"]),
        "spend_events": int(row["spend_events"]),
        "topup_events": int(row["topup_events"]),
        "by_reason": by_reason,
    }


def usage_daily(
    db_path: str,
    *,
    client_id: int,
    days: int,
    now_ts: int | None = None,
) -> dict:
    """Daily (UTC) series for dashboards."""

    days = max(1, min(int(days), 366))

    if now_ts is None:
        now_dt = datetime.now(timezone.utc)
    else:
        now_dt = datetime.fromtimestamp(int(now_ts), tz=timezone.utc)

    today = now_dt.date()
    start_day = today - timedelta(days=days - 1)

    def midnight_ts(day) -> int:
        return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())

    start_ts = midnight_ts(start_day)
    end_ts = midnight_ts(today + timedelta(days=1))

    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
              date(created_at, 'unixepoch') AS day,
              COALESCE(SUM(CASE WHEN delta_credits > 0 THEN delta_credits ELSE 0 END), 0) AS credits_in,
              COALESCE(SUM(CASE WHEN delta_credits < 0 THEN -delta_credits ELSE 0 END), 0) AS credits_out,
              COALESCE(SUM(delta_credits), 0) AS net_credits,
              COALESCE(SUM(CASE WHEN delta_credits < 0 THEN 1 ELSE 0 END), 0) AS spend_events,
              COALESCE(SUM(CASE WHEN reason = 'topup_settled' THEN 1 ELSE 0 END), 0) AS topup_events
            FROM ledger
            WHERE client_id = ? AND created_at >= ? AND created_at < ?
            GROUP BY day
            ORDER BY day ASC
            """,
            (int(client_id), start_ts, end_ts),
        ).fetchall()

    by_day: dict[str, dict] = {}
    for r in rows:
        by_day[str(r["day"])] = {
            "credits_in": int(r["credits_in"]),
            "credits_out": int(r["credits_out"]),
            "net_credits": int(r["net_credits"]),
            "spend_events": int(r["spend_events"]),
            "topup_events": int(r["topup_events"]),
        }

    series: list[dict] = []
    for i in range(days):
        day = start_day + timedelta(days=i)
        day_str = day.isoformat()
        day_start = midnight_ts(day)
        day_end = midnight_ts(day + timedelta(days=1))

        m = by_day.get(
            day_str,
            {
                "credits_in": 0,
                "credits_out": 0,
                "net_credits": 0,
                "spend_events": 0,
                "topup_events": 0,
            },
        )

        series.append(
            {
                "day": day_str,
                "day_start_ts": day_start,
                "day_start_iso": _iso(day_start),
                "day_end_ts": day_end,
                "credits_in": int(m["credits_in"]),
                "credits_out": int(m["credits_out"]),
                "net_credits": int(m["net_credits"]),
                "spend_events": int(m["spend_events"]),
                "topup_events": int(m["topup_events"]),
            }
        )

    return {
        "tz": "UTC",
        "days": days,
        "start_ts": start_ts,
        "start_iso": _iso(start_ts),
        "end_ts": end_ts,
        "end_iso": _iso(end_ts),
        "series": series,
    }


def usage_forecast(
    db_path: str,
    *,
    client_id: int,
    current_balance_credits: int,
    lookback_hours: int = 24,
    now_ts: int | None = None,
) -> dict:
    """Simple forecast: when will you run out of credits if you keep the same pace?

    - Based on consumption (negative deltas) over the `lookback_hours` window.
    - Best-effort guidance; treat as directional if traffic is bursty.
    """

    lookback_hours = max(1, min(int(lookback_hours), 24 * 30))
    current_balance_credits = max(0, int(current_balance_credits))

    if now_ts is None:
        now_ts = int(datetime.now(timezone.utc).timestamp())
    else:
        now_ts = int(now_ts)

    window_seconds = lookback_hours * 3600
    since_ts = now_ts - window_seconds

    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN delta_credits < 0 THEN -delta_credits ELSE 0 END), 0) AS credits_out,
              COALESCE(SUM(CASE WHEN delta_credits < 0 THEN 1 ELSE 0 END), 0) AS spend_events,
              MAX(CASE WHEN delta_credits < 0 THEN created_at ELSE NULL END) AS last_spend_at
            FROM ledger
            WHERE client_id = ? AND created_at >= ?
            """,
            (int(client_id), since_ts),
        ).fetchone()

    credits_out = int(row["credits_out"])
    spend_events = int(row["spend_events"])

    last_spend_at = row["last_spend_at"]
    last_spend_ts = int(last_spend_at) if last_spend_at is not None else None

    rate_credits_per_hour = credits_out / float(lookback_hours)
    rate_events_per_hour = spend_events / float(lookback_hours)

    avg_credits_per_event: float | None = None
    if spend_events > 0:
        avg_credits_per_event = credits_out / float(spend_events)

    estimated_hours_remaining: float | None = None
    estimated_depletion_ts: int | None = None
    if rate_credits_per_hour > 0:
        estimated_hours_remaining = current_balance_credits / rate_credits_per_hour
        estimated_depletion_ts = int(now_ts + (estimated_hours_remaining * 3600))

    if spend_events == 0:
        status = "insufficient_data"
    elif spend_events < 10:
        status = "low_sample"
    else:
        status = "ok"

    rate_credits_per_day = rate_credits_per_hour * 24.0
    rate_events_per_day = rate_events_per_hour * 24.0

    estimated_days_remaining: float | None = None
    if estimated_hours_remaining is not None:
        estimated_days_remaining = estimated_hours_remaining / 24.0

    return {
        "status": status,
        "tz": "UTC",
        "now_ts": now_ts,
        "now_iso": _iso(now_ts),
        "since_ts": since_ts,
        "since_iso": _iso(since_ts),
        "lookback_hours": lookback_hours,
        "current_balance_credits": current_balance_credits,
        "spend_events": spend_events,
        "credits_out": credits_out,
        "avg_credits_per_event": avg_credits_per_event,
        "rate_credits_per_hour": rate_credits_per_hour,
        "rate_credits_per_day": rate_credits_per_day,
        "rate_events_per_hour": rate_events_per_hour,
        "rate_events_per_day": rate_events_per_day,
        "estimated_hours_remaining": estimated_hours_remaining,
        "estimated_days_remaining": estimated_days_remaining,
        "estimated_depletion_ts": estimated_depletion_ts,
        "estimated_depletion_iso": _iso(estimated_depletion_ts) if estimated_depletion_ts else None,
        "last_spend_ts": last_spend_ts,
        "last_spend_iso": _iso(last_spend_ts) if last_spend_ts else None,
        "note": "Simple forecast based on the lookback window. If your traffic is irregular, treat it as guidance.",
    }


def usage_by_endpoint(
    db_path: str,
    *,
    client_id: int,
    since_ts: int,
    limit: int = 50,
) -> dict:
    """Spend summary grouped by endpoint (ledger reason).

    Useful for agents/operators to audit and budget per endpoint.

    We treat any negative delta as a spend event.
    """

    since_ts = int(since_ts)
    limit = max(1, min(int(limit), 200))

    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
              reason AS endpoint,
              COUNT(*) AS spend_events,
              COALESCE(SUM(-delta_credits), 0) AS verifications_spent,
              MAX(created_at) AS last_spend_at
            FROM ledger
            WHERE client_id = ? AND created_at >= ? AND delta_credits < 0
            GROUP BY reason
            ORDER BY verifications_spent DESC, spend_events DESC
            LIMIT ?
            """,
            (int(client_id), since_ts, limit),
        ).fetchall()

    endpoints: list[dict] = []
    for r in rows:
        last_spend_at = r["last_spend_at"]
        last_spend_ts = int(last_spend_at) if last_spend_at is not None else None

        spend_events = int(r["spend_events"])
        verifications_spent = int(r["verifications_spent"])

        endpoints.append(
            {
                "endpoint": r["endpoint"],
                "spend_events": spend_events,
                "verifications_spent": verifications_spent,
                "avg_verifications_per_call": (verifications_spent / float(spend_events)) if spend_events else None,
                "last_spend_ts": last_spend_ts,
                "last_spend_iso": _iso(last_spend_ts) if last_spend_ts else None,
            }
        )

    return {
        "since_ts": since_ts,
        "since_iso": _iso(since_ts),
        "endpoints": endpoints,
    }
