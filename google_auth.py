"""
Google OAuth + YouTube Data API v3 helpers.
All HTTP calls use httpx with explicit error handling for quota limits.
Never logs full response bodies from token endpoints.
"""
from __future__ import annotations

import logging
import httpx
from urllib.parse import urlencode
from datetime import datetime, timezone
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
    return f"{settings.OAUTH_AUTH_URL}?{urlencode(params)}"


# ────────────────── Safe error extraction ──────────────────

def _safe_error(resp: httpx.Response) -> str:
    """Extract error message without exposing tokens or full response body."""
    try:
        body = resp.json()
        err = body.get("error", body)
        if isinstance(err, dict):
            return err.get("message", err.get("error", "unknown"))
        return str(err)
    except Exception:
        return f"HTTP {resp.status_code} (non-JSON response)"


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
            err_msg = _safe_error(resp)
            logger.error("Token exchange failed: HTTP %d — %s", resp.status_code, err_msg)
            raise RuntimeError(f"Token exchange failed: {err_msg}")
        return resp.json()


async def refresh_access_token(refresh_token: str) -> dict:
    """Use a refresh_token to get a new access_token. Detects revoked tokens."""
    data = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(settings.OAUTH_TOKEN_URL, data=data)
        if resp.status_code != 200:
            err_msg = _safe_error(resp)
            # Only clear tokens on confirmed invalid_grant (revoked/expired refresh token)
            if "invalid_grant" in err_msg.lower():
                logger.error("Refresh token is invalid or revoked. Clearing creator tokens.")
                await db.clear_creator_tokens()
                raise RuntimeError("Refresh token revoked. Admin must re-authenticate via /admin/login.")
            logger.error("Token refresh failed: HTTP %d — %s", resp.status_code, err_msg)
            raise RuntimeError(f"Token refresh failed: {err_msg}")
        return resp.json()


# ────────────────── User info ──────────────────

async def get_userinfo(access_token: str) -> dict:
    """Fetch the user's email and basic profile from Google."""
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(settings.OAUTH_USERINFO_URL, headers=headers)
        if resp.status_code != 200:
            err_msg = _safe_error(resp)
            raise RuntimeError(f"Userinfo failed: {err_msg}")
        return resp.json()


async def get_youtube_channel_id(access_token: str) -> str | None:
    """Fetch the user's public YouTube channel ID."""
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"part": "id", "mine": "true"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(settings.YOUTUBE_CHANNELS_URL, headers=headers, params=params)
        if resp.status_code != 200:
            err_msg = _safe_error(resp)
            logger.warning("YouTube channels lookup failed: %s", err_msg)
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
            err_msg = _safe_error(resp)
            logger.error("Creator channel lookup failed: %s", err_msg)
            return None
        data = resp.json()
        items = data.get("items", [])
        return items[0]["id"] if items else None


class QuotaExceededError(Exception):
    """Raised when YouTube API quota is hit."""
    pass


async def list_channel_members(access_token: str) -> list[dict]:
    """
    Hit the youtube.members.list endpoint.
    Returns list of transformed member dicts: {channel_id, membership_level}
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    raw_members: list[dict] = []
    page_token: str | None = None
    base_url = "https://www.googleapis.com/youtube/v3/members"
    max_pages = 50  # safety cap: 50 * 50 = 2500 members max
    pages_fetched = 0

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            if pages_fetched >= max_pages:
                logger.warning("Pagination cap reached (%d pages) — stopping", max_pages)
                break
            params: dict = {"part": "snippet", "maxResults": "50"}
            if page_token:
                params["pageToken"] = page_token

            resp = await client.get(base_url, headers=headers, params=params)

            # ── Quota / rate limit handling (safe JSON parsing) ──
            if resp.status_code in (403, 429):
                reason = ""
                try:
                    error_body = resp.json().get("error", {})
                    errors_list = error_body.get("errors", [])
                    reason = errors_list[0].get("reason", "") if errors_list else ""
                except (ValueError, KeyError, IndexError):
                    pass

                if reason in ("quotaExceeded", "rateLimitExceeded", "dailyLimitExceeded") or resp.status_code == 429:
                    logger.error("YouTube API QUOTA HIT: %s", reason or f"HTTP {resp.status_code}")
                    raise QuotaExceededError(f"YouTube API quota exceeded: {reason or 'rate limited'}")

                logger.error("YouTube API access denied: HTTP %d", resp.status_code)
                raise RuntimeError(f"YouTube API error: HTTP {resp.status_code}")

            if resp.status_code != 200:
                err_msg = _safe_error(resp)
                logger.error("YouTube members API error: %s", err_msg)
                raise RuntimeError(f"YouTube members API error: {err_msg}")

            data = resp.json()
            raw_members.extend(data.get("items", []))
            pages_fetched += 1

            page_token = data.get("nextPageToken")
            if not page_token:
                break

    # ── Transform raw API items into flat dicts ──
    members: list[dict] = []
    for item in raw_members:
        snippet = item.get("snippet", {})
        channel_id = snippet.get("memberDetails", {}).get("channelId")
        if not channel_id:
            logger.warning("Skipping member item without channelId: %s", item.get("id", "unknown"))
            continue

        # Extract membership level name from nested structure
        memberships = snippet.get("membershipsDetails", {}).get("membershipsDetails", [])
        level_name = memberships[0].get("membershipLevelName", "Unknown") if memberships else "Unknown"

        members.append({
            "channel_id": channel_id,
            "membership_level": level_name,
        })

    logger.info("Fetched %d active members from YouTube API", len(members))
    return members


async def get_valid_creator_access_token() -> str:
    """
    Return a valid access token for the creator, refreshing if necessary.
    Raises RuntimeError if no creator tokens are stored or refresh fails.
    """
    tokens = await db.get_creator_tokens()
    if not tokens:
        raise RuntimeError("No creator tokens found. Run the admin OAuth flow first.")

    # Check if token is still valid (with 5-min buffer)
    expires_str = tokens["token_expires_at"]
    if expires_str:
        try:
            expires_at = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            # Handle both aware and naive datetimes
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
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
