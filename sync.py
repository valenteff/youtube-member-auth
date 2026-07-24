"""
Background sync job — periodically pulls the latest member list from YouTube
and overwrites the local active_members table.
"""
from __future__ import annotations

import asyncio
import logging
from config import settings
import database as db
from google_auth import get_valid_creator_access_token, list_channel_members, QuotaExceededError

logger = logging.getLogger("sync")


async def sync_members_once():
    """Single sync run. Returns (success: bool, member_count: int, error: str|None)."""
    try:
        access_token = await get_valid_creator_access_token()
    except RuntimeError as e:
        msg = f"Cannot get creator token: {e}"
        logger.error(msg)
        return False, 0, msg

    try:
        members = await list_channel_members(access_token)
    except QuotaExceededError as e:
        msg = f"Quota exceeded during sync: {e}"
        logger.error(msg)
        return False, 0, msg
    except RuntimeError as e:
        msg = f"API error during sync: {e}"
        logger.error(msg)
        return False, 0, msg

    # Overwrite local table (atomically)
    await db.replace_active_members(members)
    count = await db.count_active_members()
    logger.info("Sync complete — %d active members in DB", count)
    return True, count, None


async def sync_loop():
    """Background loop that runs sync_members_once on SYNC_INTERVAL."""
    logger.info("Starting member sync loop (interval: %ds)", settings.SYNC_INTERVAL)
    while True:
        try:
            await sync_members_once()
        except Exception as e:
            logger.error("Unexpected sync error: %s", e, exc_info=True)
        await asyncio.sleep(settings.SYNC_INTERVAL)
