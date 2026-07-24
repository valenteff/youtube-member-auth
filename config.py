"""
Configuration loader — reads from environment / .env file.
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # Google OAuth
    GOOGLE_CLIENT_ID: str = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET: str = os.getenv("GOOGLE_CLIENT_SECRET", "")

    # JWT
    JWT_SECRET: str = os.getenv("JWT_SECRET", "dev-secret-change-me")
    JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM", "HS256")

    # Redirect URIs
    CLIENT_REDIRECT_URI: str = os.getenv("CLIENT_REDIRECT_URI", "http://localhost:8000/auth/google/callback")
    CREATOR_REDIRECT_URI: str = os.getenv("CREATOR_REDIRECT_URI", "http://localhost:8000/admin/google/callback")

    # Scopes
    CLIENT_SCOPES: str = os.getenv("CLIENT_SCOPES", "openid email profile https://www.googleapis.com/auth/youtube")
    CREATOR_SCOPES: str = os.getenv("CREATOR_SCOPES", "openid email profile https://www.googleapis.com/auth/youtube.channel-memberships.creator")

    # Database
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", "./members.db")

    # Server
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))

    # Sync
    SYNC_INTERVAL: int = int(os.getenv("SYNC_INTERVAL", "3600"))

    # Google OAuth endpoints (constant)
    OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
    OAUTH_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
    YOUTUBE_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"


settings = Settings()
