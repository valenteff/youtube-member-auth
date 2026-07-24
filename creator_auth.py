#!/usr/bin/env python3
"""
Standalone Creator OAuth Flow — Desktop/Loopback mode.

Works without configuring redirect URIs in Google Cloud Console.
Spawns a temporary local HTTP server to catch the OAuth callback.

Usage:
    python3 creator_auth.py

The script will:
    1. Open your browser to Google's consent screen
    2. Auto-capture the authorization code via localhost callback
    3. Exchange it for tokens (including refresh_token)
    4. Save everything to the SQLite database
"""
from __future__ import annotations

import asyncio
import socket
import webbrowser
from urllib.parse import urlencode, urlparse, parse_qs

import httpx

from config import settings
import database as db

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
_LOOPBACK_PORT = 8888
_REDIRECT_URI = f"http://localhost:{_LOOPBACK_PORT}/callback"
_SCOPES = (
    "openid email profile "
    "https://www.googleapis.com/auth/youtube.channel-memberships.creator"
)
_TIMEOUT = 120  # seconds to wait for browser callback


def _build_auth_url() -> str:
    """Construct the Google OAuth authorization URL."""
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": _REDIRECT_URI,
        "response_type": "code",
        "scope": _SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"


async def _exchange_code(code: str) -> dict:
    """Exchange the authorization code for access/refresh tokens."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(_TOKEN_URL, data={
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": _REDIRECT_URI,
        })
        resp.raise_for_status()
        return resp.json()


async def _fetch_userinfo(access_token: str) -> dict:
    """Fetch the Google account's email and profile."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            _USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()


async def _fetch_channel_id(access_token: str) -> str:
    """Fetch the creator's YouTube channel ID.

    Returns 'unknown' if the memberships scope doesn't grant channels.list
    access (this is expected — the channel ID can be set manually).
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            _CHANNELS_URL,
            params={"part": "id", "mine": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code != 200:
            print(f"  Note: Could not auto-detect channel ID ({resp.status_code}).")
            print(f"  You can set it manually in the database or via CHANNEL_ID env var.")
            return "unknown"
        items = resp.json().get("items", [])
        return items[0]["id"] if items else "unknown"


async def _catch_callback() -> str | None:
    """Listen on localhost for the OAuth redirect and extract the code."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", _LOOPBACK_PORT))
    sock.listen(1)
    sock.settimeout(_TIMEOUT)

    print(f"  Waiting for browser redirect on port {_LOOPBACK_PORT}...")
    print(f"  (timeout: {_TIMEOUT}s)")
    print()

    try:
        conn, _ = sock.accept()
        raw = conn.recv(4096).decode("utf-8", errors="replace")
        path = raw.split("\n")[0].split(" ")[1] if " " in raw else ""
        code = parse_qs(urlparse(path).query).get("code", [None])[0]

        html = (
            "<html><body style='font-family:sans-serif;text-align:center;padding:60px'>"
            "<h1>&#10004; Authorized!</h1>"
            "<p>Code captured. You can close this tab.</p>"
            "</body></html>"
        ) if code else (
            "<html><body><h1>Error</h1><p>No code found in callback.</p></body></html>"
        )
        conn.sendall(f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n{html}".encode())
        conn.close()
        return code
    except socket.timeout:
        return None
    except OSError:
        return None
    finally:
        sock.close()


async def main() -> None:
    """Run the creator OAuth flow end-to-end."""
    print("=" * 60)
    print("  YouTube Creator OAuth Setup (Desktop Mode)")
    print("=" * 60)
    print()
    print("  This opens your browser to authorize channel member access.")
    print("  No redirect URI configuration needed — uses loopback.")
    print()

    url = _build_auth_url()
    print("  Opening browser...")
    print()
    print(f"  {url}")
    print()
    webbrowser.open(url)

    code = await _catch_callback()

    if not code:
        code = input("  Paste the authorization code manually: ").strip()
    if not code:
        print("  No code provided. Aborting.")
        return

    print()
    print("  Exchanging code for tokens...")
    tokens = await _exchange_code(code)

    access_token = tokens["access_token"]
    refresh_token = tokens.get("refresh_token", "")
    expires_in = tokens.get("expires_in", 3600)

    if not refresh_token:
        print("  ERROR: No refresh_token received.")
        print("  Revoke access at https://myaccount.google.com/permissions and retry.")
        return

    print("  Fetching user info...")
    userinfo = await _fetch_userinfo(access_token)
    email = userinfo.get("email", "unknown")
    print(f"  Email: {email}")

    channel_id = await _fetch_channel_id(access_token)

    print()
    print("  Saving to database...")
    await db.init_db()
    await db.save_creator_tokens(email, channel_id, access_token, refresh_token, expires_in)
    await db.close_db()

    print()
    print("  Creator tokens saved successfully.")
    print(f"    Email: {email}")
    print(f"    Channel: {channel_id}")
    print(f"    Refresh token: stored")
    print()
    print("  The server can now sync channel members automatically.")


if __name__ == "__main__":
    asyncio.run(main())
