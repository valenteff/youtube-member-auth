"""
SQLite database layer — async with aiosqlite.

Tables:
  users          — end-users who log in with Google
  creator_tokens — the channel owner's OAuth tokens (single row)
  active_members — current paying members synced from YouTube API
"""
from __future__ import annotations

import aiosqlite
from config import settings

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS users (
    user_id             TEXT PRIMARY KEY,
    email               TEXT NOT NULL,
    youtube_channel_id  TEXT,
    oauth_access_token  TEXT,
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


async def get_db() -> aiosqlite.Connection:
    """Return a connection. Callers must close it."""
    db = await aiosqlite.connect(settings.DATABASE_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db():
    """Create tables on startup."""
    db = await get_db()
    try:
        await db.executescript(_CREATE_TABLES)
        await db.commit()
    finally:
        await db.close()


# ---------- users ----------

async def upsert_user(user_id: str, email: str, channel_id: str | None, access_token: str):
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO users (user_id, email, youtube_channel_id, oauth_access_token)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 email = excluded.email,
                 youtube_channel_id = excluded.youtube_channel_id,
                 oauth_access_token = excluded.oauth_access_token""",
            (user_id, email, channel_id, access_token),
        )
        await db.commit()
    finally:
        await db.close()


async def get_user_by_id(user_id: str) -> dict | None:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


# ---------- creator_tokens ----------

async def save_creator_tokens(email: str, channel_id: str, access_token: str, refresh_token: str, expires_in: int):
    db = await get_db()
    try:
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
    finally:
        await db.close()


async def get_creator_tokens() -> dict | None:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM creator_tokens WHERE id = 1")
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def update_creator_access_token(access_token: str, expires_in: int):
    db = await get_db()
    try:
        await db.execute(
            """UPDATE creator_tokens
               SET access_token = ?,
                   token_expires_at = datetime('now', '+' || ? || ' seconds'),
                   updated_at = datetime('now')
               WHERE id = 1""",
            (access_token, str(expires_in)),
        )
        await db.commit()
    finally:
        await db.close()


# ---------- active_members ----------

async def replace_active_members(members: list[dict]):
    """Overwrite the entire active_members table with the fresh list."""
    db = await get_db()
    try:
        await db.execute("DELETE FROM active_members")
        for m in members:
            await db.execute(
                """INSERT INTO active_members (youtube_channel_id, membership_level, last_updated)
                   VALUES (?, ?, datetime('now'))""",
                (m["channel_id"], m.get("memberships_details", [{}])[0].get("memberDetails", {}).get("membershipLevelName", "Unknown")),
            )
        await db.commit()
    finally:
        await db.close()


async def is_active_member(channel_id: str) -> bool:
    if not channel_id:
        return False
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT 1 FROM active_members WHERE youtube_channel_id = ?",
            (channel_id,),
        )
        row = await cursor.fetchone()
        return row is not None
    finally:
        await db.close()


async def count_active_members() -> int:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM active_members")
        row = await cursor.fetchone()
        return row["cnt"] if row else 0
    finally:
        await db.close()
