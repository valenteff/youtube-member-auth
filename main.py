"""
Main FastAPI application — YouTube Member Auth PoC.

Routes:
  GET  /                         — Landing / status
  GET  /login                    — Redirect to Google OAuth (client flow)
  GET  /auth/google/callback     — OAuth callback for client flow
  GET  /admin/login              — Redirect to Google OAuth (creator flow)
  GET  /admin/google/callback    — OAuth callback for creator flow
  GET  /admin/sync               — Manually trigger member sync
  GET  /protected/strategy-code  — Protected endpoint (requires active membership)
  GET  /health                   — Health check
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jose import jwt

from config import settings
import database as db
from google_auth import (
    build_client_auth_url,
    build_creator_auth_url,
    exchange_code_for_tokens,
    get_userinfo,
    get_youtube_channel_id,
    get_creator_channel_id,
)
from middleware import MemberAuthMiddleware
from sync import sync_members_once, sync_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("app")


# ────────────────── Lifespan (startup/shutdown) ──────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing database...")
    await db.init_db()
    logger.info("Starting background sync task...")
    task = asyncio.create_task(sync_loop())
    yield
    task.cancel()
    logger.info("Shutdown complete.")


import asyncio

app = FastAPI(title="YouTube Member Auth PoC", version="1.0.0", lifespan=lifespan)
app.add_middleware(MemberAuthMiddleware)


# ────────────────── Helpers ──────────────────

def _make_jwt(user_id: str, email: str, channel_id: str | None) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "channel_id": channel_id,
        "exp": datetime.now(timezone.utc) + timedelta(hours=24),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


# ────────────────── Landing ──────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return """
    <html><body style="font-family: sans-serif; max-width: 600px; margin: 80px auto;">
      <h1>YouTube Member Auth — PoC</h1>
      <p>This gateway restricts access to active YouTube channel members only.</p>
      <hr>
      <h3>Endpoints:</h3>
      <ul>
        <li><a href="/login">/login</a> — Login with Google (user flow)</li>
        <li><a href="/admin/login">/admin/login</a> — Creator OAuth (one-time setup)</li>
        <li><a href="/admin/sync">/admin/sync</a> — Manually trigger member sync</li>
        <li><code>/protected/strategy-code</code> — Protected endpoint (needs JWT + membership)</li>
        <li><a href="/health">/health</a> — Health check</li>
      </ul>
    </body></html>
    """


# ────────────────── User (Client) OAuth Flow ──────────────────

@app.get("/login")
async def login():
    """Redirect user to Google OAuth consent."""
    state = f"user-{datetime.now().timestamp()}"
    auth_url = build_client_auth_url(state)
    return RedirectResponse(auth_url)


@app.get("/auth/google/callback")
async def client_callback(code: str, state: str = "", error: str = ""):
    """Handle Google OAuth callback for end-users."""
    if error:
        raise HTTPException(400, f"OAuth error: {error}")

    # Exchange code → tokens
    token_data = await exchange_code_for_tokens(code, settings.CLIENT_REDIRECT_URI)
    access_token = token_data["access_token"]

    # Get user info
    userinfo = await get_userinfo(access_token)
    user_id = userinfo["sub"]
    email = userinfo.get("email", "")

    # Try to get YouTube channel ID
    channel_id = await get_youtube_channel_id(access_token)

    # Save to DB
    await db.upsert_user(user_id, email, channel_id, access_token)

    # Issue JWT
    jwt_token = _make_jwt(user_id, email, channel_id)

    return JSONResponse({
        "message": "Login successful",
        "email": email,
        "youtube_channel_id": channel_id,
        "jwt": jwt_token,
        "instructions": "Use this JWT in the Authorization header: Bearer <token>",
    })


# ────────────────── Creator (Admin) OAuth Flow ──────────────────

@app.get("/admin/login")
async def admin_login():
    """Redirect creator to Google OAuth with channel-memberships scope."""
    state = f"creator-{datetime.now().timestamp()}"
    auth_url = build_creator_auth_url(state)
    return RedirectResponse(auth_url)


@app.get("/admin/google/callback")
async def creator_callback(code: str, state: str = "", error: str = ""):
    """Handle Google OAuth callback for the channel creator."""
    if error:
        raise HTTPException(400, f"OAuth error: {error}")

    token_data = await exchange_code_for_tokens(code, settings.CREATOR_REDIRECT_URI)

    access_token = token_data["access_token"]
    refresh_token = token_data.get("refresh_token")

    if not refresh_token:
        raise HTTPException(
            400,
            "No refresh_token returned. Revoke access at https://myaccount.google.com/permissions "
            "and try again. You must use prompt=consent and access_type=offline.",
        )

    expires_in = token_data.get("expires_in", 3600)

    # Get creator's email and channel ID
    userinfo = await get_userinfo(access_token)
    email = userinfo.get("email", "")
    channel_id = await get_creator_channel_id(access_token)

    # Store creator tokens (single row, id=1)
    await db.save_creator_tokens(email, channel_id or "", access_token, refresh_token, expires_in)

    return JSONResponse({
        "message": "Creator authenticated successfully. Server can now sync members offline.",
        "email": email,
        "channel_id": channel_id,
        "has_refresh_token": True,
    })


# ────────────────── Manual Sync Trigger ──────────────────

@app.get("/admin/sync")
async def manual_sync():
    """Manually trigger a member sync. Useful for testing."""
    success, count, error = await sync_members_once()
    if success:
        return JSONResponse({"status": "ok", "active_members": count})
    return JSONResponse({"status": "error", "error": error}, status_code=500)


# ────────────────── Protected Endpoint ──────────────────

@app.get("/protected/strategy-code")
async def protected_strategy_code(request: Request):
    """
    Dummy protected endpoint. Only accessible to active members.
    The middleware already verified membership — user is in request.state.
    """
    user = request.state.user
    return JSONResponse({
        "message": "Welcome! You are an active channel member.",
        "strategy_code": "def my_strategy(): return 'alpha_signal_v3'",
        "accessed_by": user["email"],
        "channel_id": user["youtube_channel_id"],
    })


# ────────────────── Health ──────────────────

@app.get("/health")
async def health():
    member_count = await db.count_active_members()
    creator = await db.get_creator_tokens()
    return JSONResponse({
        "status": "healthy",
        "creator_configured": creator is not None,
        "active_members_synced": member_count,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
