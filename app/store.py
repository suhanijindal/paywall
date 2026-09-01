from __future__ import annotations
"""
Where we keep data.

Two tables:

  orders  - one row per purchase attempt, and what happened to it
  events  - an append-only log of everything the system did

"Append-only" means we only ever add rows, never edit or delete them.
That is what makes it an audit trail rather than just a database table.
Tomorrow we add hashing so that tampering with old rows becomes detectable.

We use SQLite today because it needs zero setup - it is just a file on disk.
We move to PostgreSQL later. The code that talks to the database is all in
this one file, so swapping the database means changing one file, not fifty.
"""

import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "paywall.db"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create the tables if they don't exist yet. Safe to run every startup."""
    with connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id                TEXT PRIMARY KEY,
                idempotency_key   TEXT UNIQUE,
                sku               TEXT NOT NULL,
                quantity          INTEGER NOT NULL,
                amount_paise      INTEGER NOT NULL,
                status            TEXT NOT NULL,
                razorpay_order_id TEXT,
                razorpay_payment_id TEXT,
                created_at        REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                seq        INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id   TEXT,
                kind       TEXT NOT NULL,
                detail     TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)


def log_event(kind: str, detail: dict, order_id: str | None = None) -> None:
    """
    Write one line into the audit trail.

    'kind' is a short machine-readable label like 'order.created' or
    'payment.captured'. 'detail' is any extra data as a dictionary.
    Every meaningful action in the system calls this.
    """
    with connect() as conn:
        conn.execute(
            "INSERT INTO events (order_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
            (order_id, kind, json.dumps(detail), time.time()),
        )


def create_order(order_id, idempotency_key, sku, quantity, amount_paise, razorpay_order_id):
    with connect() as conn:
        conn.execute(
            """INSERT INTO orders
               (id, idempotency_key, sku, quantity, amount_paise, status,
                razorpay_order_id, razorpay_payment_id, created_at)
               VALUES (?, ?, ?, ?, ?, 'created', ?, NULL, ?)""",
            (order_id, idempotency_key, sku, quantity, amount_paise,
             razorpay_order_id, time.time()),
        )


def find_order_by_idempotency_key(key: str) -> dict | None:
    """
    Look up an order by the caller's idempotency key.

    An idempotency key is a unique string the caller sends with a request.
    If the same key arrives twice, we return the original order instead of
    creating a second one. This is how payment systems prevent double
    charging when a network glitch causes a request to be retried.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return dict(row) if row else None


def get_order(order_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return dict(row) if row else None


def get_order_by_razorpay_id(razorpay_order_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE razorpay_order_id = ?", (razorpay_order_id,)
        ).fetchone()
        return dict(row) if row else None


def mark_paid(order_id: str, razorpay_payment_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE orders SET status = 'paid', razorpay_payment_id = ? WHERE id = ?",
            (razorpay_payment_id, order_id),
        )


def list_events(limit: int = 100) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            {**dict(r), "detail": json.loads(r["detail"])}
            for r in rows
        ]


def list_orders(limit: int = 100) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
