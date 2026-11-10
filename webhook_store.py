import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "webhook_events.db"


def initialize_db():
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS webhook_jobs (
                item_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)


def claim_item(item_id: str) -> bool:
    """Atomically claim an item for processing."""
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO webhook_jobs (item_id, status)
            VALUES (?, 'processing')
            """,
            (item_id,),
        )
        return cursor.rowcount == 1


def update_status(item_id: str, status: str):
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute(
            """
            UPDATE webhook_jobs
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE item_id = ?
            """,
            (status, item_id),
        )

def get_item_status(item_id: str) -> str | None:
    """Return the existing SQLite processing status."""
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        row = conn.execute(
            "SELECT status FROM webhook_jobs WHERE item_id = ?",
            (item_id,),
        ).fetchone()

    return row[0] if row else None