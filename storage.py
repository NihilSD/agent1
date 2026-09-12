"""SQLite-backed persistence for per-server (or per-user, in DMs) template schemas."""
import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Optional

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS templates (
    scope_id TEXT PRIMARY KEY,
    schema_json TEXT NOT NULL,
    source_filename TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""


@contextmanager
def _connect():
    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def scope_id_for(guild_id: Optional[int], user_id: int) -> str:
    """Templates are stored per-server; in a DM they fall back to per-user."""
    return f"guild:{guild_id}" if guild_id is not None else f"user:{user_id}"


def save_template(scope_id: str, schema: dict, source_filename: str) -> None:
    now = time.time()
    payload = json.dumps(schema, ensure_ascii=False)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO templates (scope_id, schema_json, source_filename, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(scope_id) DO UPDATE SET
                schema_json=excluded.schema_json,
                source_filename=excluded.source_filename,
                updated_at=excluded.updated_at
            """,
            (scope_id, payload, source_filename, now, now),
        )


def get_template(scope_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT schema_json, source_filename, created_at, updated_at FROM templates WHERE scope_id = ?",
            (scope_id,),
        ).fetchone()
    if not row:
        return None
    schema_json, source_filename, created_at, updated_at = row
    return {
        "schema": json.loads(schema_json),
        "source_filename": source_filename,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def delete_template(scope_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM templates WHERE scope_id = ?", (scope_id,))
        return cur.rowcount > 0
