"""
Append-only audit trail.

Every action that touches money or cart state MUST be logged here,
regardless of whether it came from the WhatsApp human-agent flow
or the MCP AI-buyer flow. This is what makes the system's actions
explainable after the fact -- the buildathon's stated bar for
Track 1 is "every money action explainable, bounded and gated" and
"show the audit trail" -- this module IS that audit trail.
"""

import sqlite3
import time
import json
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "audit_trail.db")


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            actor TEXT NOT NULL,          -- 'human_whatsapp' | 'ai_agent_mcp'
            actor_id TEXT,                -- phone number / mcp session id
            action TEXT NOT NULL,         -- e.g. 'checkout_attempt', 'checkout_success'
            amount_inr REAL,
            status TEXT NOT NULL,         -- 'ok' | 'blocked' | 'failed' | 'retried'
            details TEXT                  -- JSON blob, free-form
        )
        """
    )
    return conn


def log_action(actor: str, actor_id: str, action: str, status: str,
                amount_inr: float | None = None, details: dict | None = None):
    conn = _get_conn()
    conn.execute(
        "INSERT INTO audit_log (timestamp, actor, actor_id, action, amount_inr, status, details) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (time.time(), actor, actor_id, action, amount_inr, status,
         json.dumps(details or {})),
    )
    conn.commit()
    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return row_id


def get_trail(limit: int = 100):
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, timestamp, actor, actor_id, action, amount_inr, status, details "
        "FROM audit_log ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    cols = ["id", "timestamp", "actor", "actor_id", "action", "amount_inr", "status", "details"]
    return [dict(zip(cols, r)) for r in rows]


def captured_spend_today(merchant_id: str, actor: str) -> float:
    """Sum of this actor's CAPTURED (payment_confirmed, status=paid)
    transactions AT THIS MERCHANT today -- not merely order/link-created
    ones, and not spend at a DIFFERENT merchant (a warrant's daily cap
    is itself per-(agent, merchant); the audit_log schema has no
    merchant_id column of its own, so this resolves each row's actor_id
    -- which is always the session_id, see log_action()'s call sites --
    back to its merchant via sessions.get_session() and filters).

    This is the `spend_today` input policy.evaluate() needs for its
    daily-cap rule; computed here (a plain query) rather than inside
    policy.py so that module stays a pure function of its inputs, with
    no I/O of its own."""
    from . import sessions  # local import -- avoids a circular import at module load time

    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT actor_id, amount_inr FROM audit_log
            WHERE actor = ?
              AND action = 'payment_confirmed'
              AND status = 'paid'
              AND date(timestamp, 'unixepoch', 'localtime') = date('now', 'localtime')
            """,
            (actor,),
        ).fetchall()
    finally:
        conn.close()

    total = 0.0
    for session_id, amount_inr in rows:
        session = sessions.get_session(session_id)
        if session and session.get("merchant_id") == merchant_id:
            total += amount_inr or 0.0
    return total
