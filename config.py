"""
Configuration loader — reads from environment / .env file.
Fails fast on missing critical settings.
"""
from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()

# ── JWT secret: fail-fast if missing or default ──
_jwt_secret = os.getenv("JWT_SECRET", "")
_DEFAULT_SENTINEL = "dev-secret-change-me"
if not _jwt_secret or _jwt_secret == _DEFAULT_SENTINEL:
    raise RuntimeError(
        "JWT_SECRET must be set to a random value. "
        'Generate with: python -c "import secrets; print(secrets.token_hex(32))"'
    )


class Settings:
    # Google OAuth — fail-fast if not configured
    GOOGLE_CLIENT_ID: str = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET: str = os.getenv("GOOGLE_CLIENT_SECRET", "")

    # JWT
    JWT_SECRET: str = _jwt_secret
    JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM", "HS256")
    JWT_ISSUER: str = "youtube-member-auth"
    JWT_AUDIENCE: str = "youtube-member-auth"
    JWT_EXPIRY_MINUTES: int = int(os.getenv("JWT_EXPIRY_MINUTES", "60"))

    # Redirect URIs
    CLIENT_REDIRECT_URI: str = os.getenv("CLIENT_REDIRECT_URI", "http://localhost:8000/auth/google/callback")
    CREATOR_REDIRECT_URI: str = os.getenv("CREATOR_REDIRECT_URI", "http://localhost:8000/admin/google/callback")

    # Scopes
    CLIENT_SCOPES: str = os.getenv("CLIENT_SCOPES", "openid email profile https://www.googleapis.com/auth/youtube")
    CREATOR_SCOPES: str = os.getenv("CREATOR_SCOPES", "openid email profile https://www.googleapis.com/auth/youtube.channel-memberships.creator")

    # Database
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", "./members.db")

    # Admin API key for /admin/* endpoints
    ADMIN_API_KEY: str = os.getenv("ADMIN_API_KEY", "")

    # Creator email allowlist (comma-separated) — only these Google accounts
    # can complete the creator OAuth flow
    CREATOR_ALLOWLIST: str = os.getenv("CREATOR_ALLOWLIST", "")

    # Cookie secure flag — set to True when serving over HTTPS
    COOKIE_SECURE: bool = os.getenv("COOKIE_SECURE", "false").lower() == "true"

    # Server
    HOST: str = os.getenv("HOST", "127.0.0.1")
    PORT: int = int(os.getenv("PORT", "8000"))

    # Sync
    SYNC_INTERVAL: int = int(os.getenv("SYNC_INTERVAL", "3600"))

    # Google OAuth endpoints (constant)
    OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
    OAUTH_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
    YOUTUBE_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"


settings = Settings()

# ── Validate critical Google credentials at import ──
if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
    raise RuntimeError(
        "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set in .env. "
        "See README.md for Google Cloud Console setup instructions."
    )
