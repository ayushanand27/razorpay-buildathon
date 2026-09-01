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
