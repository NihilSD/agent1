"""SQLite-backed persistence for the active spec sheet template schema."""
import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Optional

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS templates (
    id TEXT PRIMARY KEY,
    schema_json TEXT NOT NULL,
    source_filename TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""

_ACTIVE_ID = "active"


@contextmanager
def _connect():
    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def save_template(schema: dict, source_filename: str) -> None:
    now = time.time()
    payload = json.dumps(schema, ensure_ascii=False)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO templates (id, schema_json, source_filename, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                schema_json=excluded.schema_json,
                source_filename=excluded.source_filename,
                updated_at=excluded.updated_at
            """,
            (_ACTIVE_ID, payload, source_filename, now, now),
        )


def get_template() -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT schema_json, source_filename, created_at, updated_at FROM templates WHERE id = ?",
            (_ACTIVE_ID,),
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


def delete_template() -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM templates WHERE id = ?", (_ACTIVE_ID,))
        return cur.rowcount > 0
