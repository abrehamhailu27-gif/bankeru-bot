"""
Database layer for the BANKERU bot using Supabase PostgreSQL.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import uuid
import psycopg2
from psycopg2.extras import RealDictCursor


def _new_id() -> str:
    return str(uuid.uuid4())


def get_connection():
    # Use port 6543 pooler URL to enforce IPv4 routing on Render
    db_url = "postgresql://postgres.vmurqdyzpikuizjqvdmr:Aku%401106229%40B@aws-0-eu-central-1.pooler.supabase.co:6543/postgres"

    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    conn = psycopg2.connect(db_url, cursor_factory=RealDictCursor)
    return conn


@contextmanager
def db_transaction():
    conn = get_connection()
    try:
        cur = conn.cursor()
        yield conn, cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    now = datetime.now(timezone.utc)
    with db_transaction() as (conn, cur):
        cur.execute("SELECT id FROM player WHERE is_house = TRUE")
        if cur.fetchone() is None:
            cur.execute(
                "INSERT INTO player (id, telegram_id, display_name, balance, "
                "is_house, created_at) VALUES (%s, %s, %s, %s, TRUE, %s) "
                "ON CONFLICT (telegram_id) DO NOTHING;",
                (_new_id(), 0, "HOUSE", 0, now),
            )

        cur.execute("SELECT amount FROM stake_tier WHERE is_custom = FALSE")
        existing = {row["amount"] for row in cur.fetchall()}
        for preset in (5, 10, 20):
            if preset not in existing:
                cur.execute(
                    "INSERT INTO stake_tier (amount, is_custom, created_at) "
                    "VALUES (%s, FALSE, %s)",
                    (preset, now),
                )


if __name__ == "__main__":
    init_db()
    print("Connected to Supabase PostgreSQL and initialized baseline records.")