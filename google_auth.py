"""
Google OAuth + YouTube Data API v3 helpers.
All HTTP calls use httpx with explicit error handling for quota limits.
"""
from __future__ import annotations

import time
import logging
import httpx
from config import settings
import database as db

logger = logging.getLogger("youtube_auth")


# ────────────────── OAuth URL builders ──────────────────

def build_client_auth_url(state: str) -> str:
    """Login with Google URL for end-users."""
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.CLIENT_REDIRECT_URI,
        "response_type": "code",
        "scope": settings.CLIENT_SCOPES,
        "state": state,
        "access_type": "online",
        "prompt": "consent",
    }
    from urllib.parse import urlencode
    return f"{settings.OAUTH_AUTH_URL}?{urlencode(params)}"


def build_creator_auth_url(state: str) -> str:
    """One-time creator OAuth URL — needs offline access for refresh_token."""
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.CREATOR_REDIRECT_URI,
        "response_type": "code",
        "scope": settings.CREATOR_SCOPES,
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    from urllib.parse import urlencode
    return f"{settings.OAUTH_AUTH_URL}?{urlencode(params)}"


# ────────────────── Token exchange ──────────────────

async def exchange_code_for_tokens(code: str, redirect_uri: str) -> dict:
    """Exchange authorization code for access/refresh tokens."""
    data = {
        "code": code,
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(settings.OAUTH_TOKEN_URL, data=data)
        if resp.status_code != 200:
            logger.error("Token exchange failed: %s — %s", resp.status_code, resp.text)
            raise RuntimeError(f"Token exchange failed: {resp.status_code}")
        return resp.json()


async def refresh_access_token(refresh_token: str) -> dict:
    """Use a refresh_token to get a new access_token."""
    data = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(settings.OAUTH_TOKEN_URL, data=data)
        if resp.status_code != 200:
            logger.error("Token refresh failed: %s — %s", resp.status_code, resp.text)
            raise RuntimeError(f"Token refresh failed: {resp.status_code}")
        return resp.json()


# ────────────────── User info ──────────────────

async def get_userinfo(access_token: str) -> dict:
    """Fetch the user's email and basic profile from Google."""
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(settings.OAUTH_USERINFO_URL, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"Userinfo failed: {resp.status_code} — {resp.text}")
        return resp.json()


async def get_youtube_channel_id(access_token: str) -> str | None:
    """Fetch the user's public YouTube channel ID."""
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"part": "id", "mine": "true"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(settings.YOUTUBE_CHANNELS_URL, headers=headers, params=params)
        if resp.status_code != 200:
            logger.warning("YouTube channels lookup failed: %s — %s", resp.status_code, resp.text)
            return None
        data = resp.json()
        items = data.get("items", [])
        return items[0]["id"] if items else None


# ────────────────── Creator: channel memberships ──────────────────

async def get_creator_channel_id(access_token: str) -> str | None:
    """Fetch the creator's own channel ID."""
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"part": "id", "mine": "true"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(settings.YOUTUBE_CHANNELS_URL, headers=headers, params=params)
        if resp.status_code != 200:
            logger.error("Creator channel lookup failed: %s — %s", resp.status_code, resp.text)
            return None
        data = resp.json()
        items = data.get("items", [])
        return items[0]["id"] if items else None


async def list_channel_members(access_token: str) -> list[dict]:
    """
    Hit the youtube.members.list endpoint (part of channel-memberships.creator scope).
    Paginates through all active members.

    Returns list of raw member items from the API.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    members: list[dict] = []
    page_token: str | None = None
    base_url = "https://www.googleapis.com/youtube/v3/members"

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            params: dict = {"part": "snippet", "maxResults": "50"}
            if page_token:
                params["pageToken"] = page_token

            resp = await client.get(base_url, headers=headers, params=params)

            # ── Quota / rate limit handling ──
            if resp.status_code == 403:
                error_body = resp.json().get("error", {})
                reason = error_body.get("errors", [{}])[0].get("reason", "")
                if reason in ("quotaExceeded", "rateLimitExceeded", "dailyLimitExceeded"):
                    logger.error("YouTube API QUOTA HIT: %s — %s", reason, error_body.get("message", ""))
                    raise QuotaExceededError(f"YouTube API quota exceeded: {reason}")
                # Non-quota 403
                raise RuntimeError(f"YouTube API 403: {error_body.get('message', resp.text)}")

            if resp.status_code == 429:
                logger.error("YouTube API rate limited (429)")
                raise QuotaExceededError("YouTube API rate limited (429)")

            if resp.status_code != 200:
                logger.error("YouTube members API error: %s — %s", resp.status_code, resp.text)
                raise RuntimeError(f"YouTube members API error: {resp.status_code}")

            data = resp.json()
            members.extend(data.get("items", []))

            page_token = data.get("nextPageToken")
            if not page_token:
                break

    logger.info("Fetched %d active members from YouTube API", len(members))
    return members


async def get_valid_creator_access_token() -> str:
    """
    Return a valid access token for the creator, refreshing if necessary.
    Raises RuntimeError if no creator tokens are stored.
    """
    tokens = await db.get_creator_tokens()
    if not tokens:
        raise RuntimeError("No creator tokens found. Run the admin OAuth flow first.")

    # Check if token is still valid (with 5-min buffer)
    # token_expires_at is stored as a datetime string
    from datetime import datetime
    expires_str = tokens["token_expires_at"]
    if expires_str:
        try:
            expires_at = datetime.fromisoformat(expires_str.replace("Z", ""))
            # Reuse if more than 5 minutes remain
            now = datetime.utcnow()
            if (expires_at - now).total_seconds() > 300:
                return tokens["access_token"]
        except (ValueError, TypeError):
            pass

    # Need to refresh
    refresh_token = tokens["refresh_token"]
    if not refresh_token:
        raise RuntimeError("No refresh_token stored for creator. Re-run the admin OAuth flow.")

    logger.info("Refreshing creator access token...")
    token_data = await refresh_access_token(refresh_token)
    new_access = token_data["access_token"]
    expires_in = token_data.get("expires_in", 3600)

    await db.update_creator_access_token(new_access, expires_in)
    return new_access


class QuotaExceededError(Exception):
    """Raised when YouTube API quota is hit."""
    pass
