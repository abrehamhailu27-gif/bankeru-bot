"""
Database layer for the BANKERU bot using Supabase PostgreSQL.

Implements access to all database tables:
player, stake_tier, game_group, group_member, round, hand, deck_card,
transaction, deposit_request.
"""

from contextlib import contextmanager
import os
import time
import uuid
import psycopg2
from psycopg2.extras import RealDictCursor


def _new_id() -> str:
  return str(uuid.uuid4())


def get_connection():
  db_url = os.environ.get("DATABASE_URL")
  if not db_url:
    raise ValueError("DATABASE_URL environment variable is missing.")

  # Fix for Render/Heroku postgres:// vs postgresql:// prefix standard
  if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

  conn = psycopg2.connect(db_url, cursor_factory=RealDictCursor)
  return conn


@contextmanager
def db_transaction():
  """
  Context manager providing a connection + cursor, committing on success
  and rolling back on any exception. All balance/ledger functions wrap writes
  in a transaction so partial writes never occur.
  """
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
  """
  Verifies database connectivity and seeds initial house account & preset tiers
  if they do not already exist.
  """
  with db_transaction() as (conn, cur):
    # Ensure exactly one house account exists.
    cur.execute("SELECT id FROM player WHERE is_house = 1")
    if cur.fetchone() is None:
      cur.execute(
          "INSERT INTO player (id, telegram_id, display_name, balance, "
          "is_house, created_at) VALUES (%s, %s, %s, %s, 1, %s) "
          "ON CONFLICT (telegram_id) DO NOTHING;",
          (_new_id(), 0, "HOUSE", 0, time.time()),
      )

    # Seed the fixed preset stake tiers (5, 10, 20) if not already present.
    cur.execute("SELECT amount FROM stake_tier WHERE is_custom = 0")
    existing = {row["amount"] for row in cur.fetchall()}
    for preset in (5, 10, 20):
      if preset not in existing:
        cur.execute(
            "INSERT INTO stake_tier (id, amount, is_custom, created_at) "
            "VALUES (%s, %s, 0, %s)",
            (_new_id(), preset, time.time()),
        )


if __name__ == "__main__":
  init_db()
  print("Connected to Supabase PostgreSQL and initialized baseline records.")