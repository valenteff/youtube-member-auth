"""
Main FastAPI application — YouTube Member Auth PoC.

Routes:
  GET  /                         — Landing / status
  GET  /login                    — Redirect to Google OAuth (client flow)
  GET  /auth/google/callback     — OAuth callback for client flow
  GET  /admin/login              — Redirect to Google OAuth (creator flow)
  GET  /admin/google/callback    — OAuth callback for creator flow
  GET  /admin/sync               — Manually trigger member sync (requires admin key)
  GET  /protected/strategy-code  — Protected endpoint (requires active membership)
  GET  /health                   — Health check
"""
from __future__ import annotations

import asyncio
import secrets
import hmac
import logging
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager

from typing import Optional
from fastapi import FastAPI, Request, HTTPException, Depends, Header, Cookie
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
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


# ────────────────── Admin auth dependency ──────────────────

async def require_admin(x_admin_key: str = Header(default="")):
    """Require a valid admin API key for /admin/* management endpoints.
    Uses constant-time comparison to prevent timing attacks."""
    if not settings.ADMIN_API_KEY:
        raise HTTPException(503, "ADMIN_API_KEY not configured. Set it in .env.")
    if not hmac.compare_digest(x_admin_key, settings.ADMIN_API_KEY):
        raise HTTPException(403, "Admin access required")
    return True


# ────────────────── Lifespan ──────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing database...")
    await db.init_db()
    logger.info("Starting background sync task...")
    task = asyncio.create_task(sync_loop())
    yield
    # Graceful shutdown
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await db.close_db()
    logger.info("Shutdown complete.")


app = FastAPI(title="YouTube Member Auth PoC", version="1.2.0", lifespan=lifespan)

# ── CORS: derive origin properly from redirect URI ──
_parsed = urlparse(settings.CLIENT_REDIRECT_URI)
_cors_origin = f"{_parsed.scheme}://{_parsed.netloc}"

# ── Middleware (order matters: outermost is added last) ──
# Security headers first (innermost, runs after CORS handles preflight)
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    # Let CORS middleware handle OPTIONS preflight without interception
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

# CORS middleware (handles OPTIONS preflight before auth middleware blocks it)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[_cors_origin],
    allow_credentials=True,
    allow_methods=["GET"],
    allow_headers=["Authorization", "Content-Type"],
)

# Auth middleware (outermost — but CORS already handled OPTIONS above
# because CORSMiddleware is added after and thus wraps it)
app.add_middleware(MemberAuthMiddleware)


# ────────────────── JWT helper ──────────────────

def _make_jwt(user_id: str, email: str, channel_id: str | None) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "channel_id": channel_id,
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.JWT_EXPIRY_MINUTES),
        "iat": datetime.now(timezone.utc),
        "jti": secrets.token_hex(16),  # unique token ID for revocation tracking
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
        <li><code>/admin/sync</code> — Manually trigger sync (requires X-Admin-Key header)</li>
        <li><code>/protected/strategy-code</code> — Protected endpoint (needs JWT + membership)</li>
        <li><a href="/health">/health</a> — Health check</li>
      </ul>
    </body></html>
    """


# ────────────────── User (Client) OAuth Flow ──────────────────

@app.get("/login")
async def login():
    """Redirect user to Google OAuth consent with CSRF-protected state."""
    state = secrets.token_urlsafe(32)
    auth_url = build_client_auth_url(state)
    response = RedirectResponse(auth_url)
    response.set_cookie(
        "oauth_state", state,
        httponly=True, secure=settings.COOKIE_SECURE, samesite="lax",
        max_age=600,  # 10 minutes
    )
    return response


@app.get("/auth/google/callback")
async def client_callback(
    code: str = "",
    state: str = "",
    error: str = "",
    oauth_state: Optional[str] = Cookie(default=None),
):
    """Handle Google OAuth callback for end-users."""
    if error:
        raise HTTPException(400, f"OAuth error: {error}")

    # ── CSRF protection: validate state cookie ──
    if not state or not oauth_state or state != oauth_state:
        raise HTTPException(400, "Invalid state parameter — possible CSRF attack")

    # ── Input validation ──
    if not code or len(code) > 256:
        raise HTTPException(400, "Invalid authorization code")

    # ── Exchange code → tokens ──
    try:
        token_data = await exchange_code_for_tokens(code, settings.CLIENT_REDIRECT_URI)
    except RuntimeError as e:
        raise HTTPException(502, str(e))

    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(502, "OAuth provider did not return access_token")

    # ── Get user info ──
    try:
        userinfo = await get_userinfo(access_token)
    except RuntimeError as e:
        raise HTTPException(502, str(e))

    user_id = userinfo.get("sub")
    if not user_id:
        raise HTTPException(502, "OAuth provider did not return subject")

    email = userinfo.get("email", "")

    # Try to get YouTube channel ID
    channel_id = await get_youtube_channel_id(access_token)

    # Save to DB (no access_token stored)
    await db.upsert_user(user_id, email, channel_id)

    # Issue JWT
    jwt_token = _make_jwt(user_id, email, channel_id)

    # Clear state cookie
    response = JSONResponse({
        "message": "Login successful",
        "email": email,
        "youtube_channel_id": channel_id,
        "jwt": jwt_token,
        "expires_in_minutes": settings.JWT_EXPIRY_MINUTES,
        "instructions": "Use this JWT in the Authorization header: Bearer <token>",
    })
    response.delete_cookie("oauth_state")
    return response


# ────────────────── Creator (Admin) OAuth Flow ──────────────────

@app.get("/admin/login", dependencies=[Depends(require_admin)])
async def admin_login():
    """Redirect creator to Google OAuth with channel-memberships scope.
    Requires X-Admin-Key header — prevents unauthorized creator hijacking."""
    state = secrets.token_urlsafe(32)
    auth_url = build_creator_auth_url(state)
    response = RedirectResponse(auth_url)
    response.set_cookie(
        "oauth_state", state,
        httponly=True, secure=settings.COOKIE_SECURE, samesite="lax",
        max_age=600,
    )
    return response


@app.get("/admin/google/callback")
async def creator_callback(
    code: str = "",
    state: str = "",
    error: str = "",
    oauth_state: Optional[str] = Cookie(default=None),
):
    """Handle Google OAuth callback for the channel creator."""
    if error:
        raise HTTPException(400, f"OAuth error: {error}")

    # ── CSRF protection ──
    if not state or not oauth_state or state != oauth_state:
        raise HTTPException(400, "Invalid state parameter — possible CSRF attack")

    if not code or len(code) > 256:
        raise HTTPException(400, "Invalid authorization code")

    try:
        token_data = await exchange_code_for_tokens(code, settings.CREATOR_REDIRECT_URI)
    except RuntimeError as e:
        raise HTTPException(502, str(e))

    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(502, "OAuth provider did not return access_token")

    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        raise HTTPException(
            400,
            "No refresh_token returned. Revoke access at https://myaccount.google.com/permissions "
            "and try again.",
        )

    expires_in = token_data.get("expires_in", 3600)

    # Get creator's email and channel ID
    try:
        userinfo = await get_userinfo(access_token)
    except RuntimeError as e:
        raise HTTPException(502, str(e))

    email = userinfo.get("email", "")

    # ── Creator allowlist: only approved Google accounts can be the creator ──
    if settings.CREATOR_ALLOWLIST:
        allowed_emails = [e.strip().lower() for e in settings.CREATOR_ALLOWLIST.split(",") if e.strip()]
        if email.lower() not in allowed_emails:
            logger.warning("Creator OAuth rejected — email %s not in allowlist", email)
            raise HTTPException(403, "This Google account is not authorized as a channel creator.")

    channel_id = await get_creator_channel_id(access_token)

    # Store creator tokens
    await db.save_creator_tokens(email, channel_id or "", access_token, refresh_token, expires_in)

    response = JSONResponse({
        "message": "Creator authenticated successfully. Server can now sync members offline.",
        "email": email,
        "channel_id": channel_id,
        "has_refresh_token": True,
    })
    response.delete_cookie("oauth_state")
    return response


# ────────────────── Manual Sync (admin-protected) ──────────────────

@app.get("/admin/sync", dependencies=[Depends(require_admin)])
async def manual_sync():
    """Manually trigger a member sync. Requires X-Admin-Key header."""
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
