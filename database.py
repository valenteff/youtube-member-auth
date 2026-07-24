"""
SQLite database layer — async with aiosqlite.
Uses a shared connection for efficiency with busy_timeout.

Tables:
  users          — end-users who log in with Google (no access_token stored)
  creator_tokens — the channel owner's OAuth tokens (single row)
  active_members — current paying members synced from YouTube API
"""
from __future__ import annotations

import os
import stat
import logging
import aiosqlite
from config import settings

logger = logging.getLogger("database")

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS users (
    user_id             TEXT PRIMARY KEY,
    email               TEXT NOT NULL,
    youtube_channel_id  TEXT,
    created_at          TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS creator_tokens (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    email           TEXT,
    channel_id      TEXT,
    access_token    TEXT,
    refresh_token   TEXT,
    token_expires_at TEXT,
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS active_members (
    youtube_channel_id  TEXT PRIMARY KEY,
    membership_level    TEXT,
    last_updated        TEXT DEFAULT (datetime('now'))
);
"""

# Shared connection (opened on first use, closed on shutdown)
_db: aiosqlite.Connection | None = None


async def get_db() -> aiosqlite.Connection:
    """Return the shared connection."""
    global _db
    if _db is None:
        _db = await aiosqlite.connect(settings.DATABASE_PATH)
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA foreign_keys=ON")
        await _db.execute("PRAGMA busy_timeout=5000")  # 5s wait on lock
    return _db


async def close_db():
    """Close the shared connection on shutdown."""
    global _db
    if _db is not None:
        await _db.close()
        _db = None


async def init_db():
    """Create tables on startup and restrict file permissions."""
    db = await get_db()
    await db.executescript(_CREATE_TABLES)
    await db.commit()

    # Restrict DB file permissions to owner only
    db_path = settings.DATABASE_PATH
    if os.path.exists(db_path):
        os.chmod(db_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        logger.info("Database initialized at %s (perms: 0600)", db_path)


# ---------- users ----------

async def upsert_user(user_id: str, email: str, channel_id: str | None):
    """Insert or update a user. No access_token is stored."""
    db = await get_db()
    await db.execute(
        """INSERT INTO users (user_id, email, youtube_channel_id)
           VALUES (?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
             email = excluded.email,
             youtube_channel_id = excluded.youtube_channel_id""",
        (user_id, email, channel_id),
    )
    await db.commit()


async def get_user_by_id(user_id: str) -> dict | None:
    db = await get_db()
    cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


# ---------- creator_tokens ----------

async def save_creator_tokens(email: str, channel_id: str, access_token: str, refresh_token: str, expires_in: int):
    db = await get_db()
    await db.execute(
        """INSERT INTO creator_tokens (id, email, channel_id, access_token, refresh_token, token_expires_at, updated_at)
           VALUES (1, ?, ?, ?, ?, datetime('now', '+' || ? || ' seconds'), datetime('now'))
           ON CONFLICT(id) DO UPDATE SET
             email = excluded.email,
             channel_id = excluded.channel_id,
             access_token = excluded.access_token,
             refresh_token = excluded.refresh_token,
             token_expires_at = excluded.token_expires_at,
             updated_at = datetime('now')""",
        (email, channel_id, access_token, refresh_token, str(expires_in)),
    )
    await db.commit()


async def get_creator_tokens() -> dict | None:
    db = await get_db()
    cursor = await db.execute("SELECT * FROM creator_tokens WHERE id = 1")
    row = await cursor.fetchone()
    return dict(row) if row else None


async def update_creator_access_token(access_token: str, expires_in: int):
    db = await get_db()
    await db.execute(
        """UPDATE creator_tokens
           SET access_token = ?,
               token_expires_at = datetime('now', '+' || ? || ' seconds'),
               updated_at = datetime('now')
           WHERE id = 1""",
        (access_token, str(expires_in)),
    )
    await db.commit()


async def clear_creator_tokens():
    """Clear creator tokens when refresh_token is revoked."""
    db = await get_db()
    await db.execute("DELETE FROM creator_tokens WHERE id = 1")
    await db.commit()
    logger.warning("Creator tokens cleared — admin must re-authenticate")


# ---------- active_members ----------

async def replace_active_members(members: list[dict]):
    """
    Atomically overwrite the entire active_members table.
    Uses a transaction with rollback to prevent data loss on failure.
    Each member dict must have: channel_id, membership_level
    """
    db = await get_db()
    try:
        # Execute delete + inserts — aiosqlite wraps in implicit transaction
        await db.execute("DELETE FROM active_members")
        for m in members:
            await db.execute(
                """INSERT INTO active_members (youtube_channel_id, membership_level, last_updated)
                   VALUES (?, ?, datetime('now'))""",
                (m["channel_id"], m.get("membership_level", "Unknown")),
            )
        await db.commit()
    except Exception:
        await db.rollback()
        raise


async def is_active_member(channel_id: str) -> bool:
    if not channel_id:
        return False
    db = await get_db()
    cursor = await db.execute(
        "SELECT 1 FROM active_members WHERE youtube_channel_id = ?",
        (channel_id,),
    )
    row = await cursor.fetchone()
    return row is not None


async def count_active_members() -> int:
    db = await get_db()
    cursor = await db.execute("SELECT COUNT(*) as cnt FROM active_members")
    row = await cursor.fetchone()
    return row["cnt"] if row else 0
