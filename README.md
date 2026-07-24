# YouTube Member Auth Gateway

A lightweight authentication gateway that restricts access to protected endpoints **exclusively to active YouTube channel members**.

Built with **FastAPI + SQLite + Google OAuth 2.0 + YouTube Data API v3**.

## How It Works

```
┌──────────────┐       ┌──────────────────────────┐       ┌─────────────────┐
│   End User   │──────▶│  FastAPI Gateway          │──────▶│  SQLite (local)  │
│  (browser)   │◀──────│                           │       │                  │
└──────────────┘       │  1. OAuth login (Google)  │       │  • users         │
                       │  2. JWT session (60min)   │       │  • creator_tokens│
┌──────────────┐       │  3. Member check (403/200)│       │  • active_members│
│   Creator    │──────▶│                           │       └─────────────────┘
│ (one-time)   │       └───────────┬──────────────┘
└──────────────┘                   │                ┌─────────────────────┐
                         ┌──────────▼──────────┐    │  YouTube Data API   │
                         │  Background Sync     │───▶│  v3  members.list   │
                         │  (hourly, automatic) │    │  (creator scope)    │
                         └─────────────────────┘     └─────────────────────┘
```

### Two OAuth Flows

| Flow | Scope | Purpose |
|------|-------|---------|
| **Creator** (Admin) | `youtube.channel-memberships.creator` | One-time setup. Gets a `refresh_token` so the server can sync members offline. |
| **User** (Client) | `youtube` (read-only) | End-user login. Extracts email + YouTube `channelId`. Returns a JWT. |

### Authorization Middleware

Every request to `/protected/*` passes through `MemberAuthMiddleware`:

1. Extracts JWT from `Authorization: Bearer <token>`
2. Validates signature, issuer, audience, expiry
3. Looks up the user's `youtube_channel_id` from DB
4. Queries `active_members` table for a match
5. **Match → 200 OK** | **No match → 403 Forbidden**

### Background Sync

An `asyncio` loop runs every hour (configurable via `SYNC_INTERVAL`):

- Uses the creator's stored `refresh_token` to get a fresh access token
- Calls `youtube.members.list` (paginated, 50/page)
- **Atomically overwrites** the `active_members` table — canceled memberships are dropped
- **Empty-wipe guard**: if the API returns an empty list but members exist locally, the wipe is skipped (prevents mass-lockout from transient API issues)

## Quick Start

### Prerequisites

- Python 3.9+
- A YouTube channel with **channel memberships enabled** (YouTube Partner Program)
- A Google Cloud project

### 1. Install

```bash
git clone https://github.com/valenteff/youtube-member-auth.git
cd youtube-member-auth
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### 2. Google Cloud Console Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. **Create or select a project**
3. **Enable YouTube Data API v3**: APIs & Services → Library → search "YouTube Data API v3" → Enable
4. **Configure OAuth Consent Screen** (External type):
   - Under **Scopes**, add:
     - `https://www.googleapis.com/auth/youtube.channel-memberships.creator`
     - `https://www.googleapis.com/auth/youtube`
     - `openid`, `email`, `profile`
   - Under **Test Users**, add the Google account that owns your YouTube channel
5. **Create OAuth Credentials**:
   - Type: **Desktop app** (recommended — no redirect URI setup needed)
   - Or type: **Web application** (requires redirect URI configuration)
   - Copy the **Client ID** and **Client Secret**

### 3. Configure `.env`

```bash
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret

# Generate a random JWT secret:
JWT_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")

# Generate a random admin API key:
ADMIN_API_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(24))")
```

Fill these into your `.env` file. See `.env.example` for all options.

### 4. Run the Server

```bash
source venv/bin/activate
uvicorn main:app --reload --port 8000
```

### 5. Creator Setup (One-Time)

You have two options:

**Option A: Desktop Mode (Recommended — no redirect URI config)**

Run the standalone script (works with Desktop-type OAuth credentials):

```bash
python3 creator_auth.py
```

This opens your browser, you authorize with your Google account, and it auto-captures the callback via a local server on port 8888.

**Option B: Web Mode**

If you created Web-type OAuth credentials, add these redirect URIs in Google Cloud Console:
```
http://localhost:8000/auth/google/callback
http://localhost:8000/admin/google/callback
```

Then open in your browser:
```
http://localhost:8000/admin/login
```
(Note: pass the `X-Admin-Key` header to authenticate)

After successful creator auth, you'll see:
```json
{
  "message": "Creator authenticated successfully.",
  "channel_id": "UCxxxxxxxxx",
  "has_refresh_token": true
}
```

### 6. Sync Members

```bash
# Manual sync:
curl -H "X-Admin-Key: your-admin-key" \
     http://localhost:8000/admin/sync

# Or just wait — the background loop syncs every hour
```

> **Note**: If your channel has zero active members, the YouTube API returns `403 Forbidden`. This is expected — the sync will succeed once you have at least one member.

### 7. User Login Flow

```bash
# Open in browser:
open http://localhost:8000/login
```

After Google consent, the response includes a JWT:
```json
{
  "email": "user@example.com",
  "youtube_channel_id": "UCxxxxx",
  "jwt": "eyJ...",
  "expires_in_minutes": 60
}
```

### 8. Access Protected Content

```bash
curl -H "Authorization: Bearer eyJ..." \
     http://localhost:8000/protected/strategy-code
```

**Active member:**
```json
{
  "message": "Welcome! You are an active channel member.",
  "strategy_code": "def my_strategy(): return 'alpha_signal_v3'",
  "accessed_by": "user@example.com"
}
```

**Non-member:**
```json
{
  "error": "not_a_member",
  "message": "Access denied. Become a channel member to unlock this content."
}
```

## API Reference

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/` | — | Landing page |
| GET | `/login` | — | Redirect to Google OAuth (user flow) |
| GET | `/auth/google/callback` | — | OAuth callback, returns JWT |
| GET | `/admin/login` | `X-Admin-Key` | Redirect to Google OAuth (creator flow) |
| GET | `/admin/google/callback` | cookie state | Stores creator `refresh_token` |
| GET | `/admin/sync` | `X-Admin-Key` | Manually trigger member sync |
| GET | `/protected/strategy-code` | JWT + Membership | Protected example endpoint |
| GET | `/health` | — | Health check + member count |

## Database Schema

```sql
CREATE TABLE users (
    user_id             TEXT PRIMARY KEY,   -- Google subject ID
    email               TEXT NOT NULL,
    youtube_channel_id  TEXT,
    created_at          TEXT DEFAULT (datetime('now'))
);
-- Note: No access_token column — we don't store user OAuth tokens.

CREATE TABLE creator_tokens (
    id               INTEGER PRIMARY KEY CHECK (id = 1),  -- singleton row
    email            TEXT,
    channel_id       TEXT,
    access_token     TEXT,
    refresh_token    TEXT,     -- persistent, for offline API calls
    token_expires_at TEXT,
    updated_at       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE active_members (
    youtube_channel_id  TEXT PRIMARY KEY,
    membership_level    TEXT,
    last_updated        TEXT DEFAULT (datetime('now'))
);
```

The database file is created with `0600` permissions (owner read/write only).

## Quota Handling

YouTube Data API v3 has a daily quota (default: 10,000 units). Each `members.list` call costs ~1 unit.

| HTTP | Reason | Behavior |
|------|--------|----------|
| 403 | `quotaExceeded` | Log error, sync fails gracefully, retries next cycle |
| 403 | `dailyLimitExceeded` | Same — waits for quota reset |
| 429 | `rateLimitExceeded` | Same — backs off automatically |
| 403 | Other | Log error with details |

`QuotaExceededError` is raised and caught in `sync.py`, preventing crashes.

## Project Structure

```
youtube-member-auth/
├── main.py              # FastAPI app — routes, lifespan, middleware
├── config.py            # Settings (reads .env, fail-fast validation)
├── database.py          # SQLite async layer (aiosqlite + write lock)
├── google_auth.py       # OAuth flows + YouTube API + quota handling
├── sync.py              # Background member sync loop
├── middleware.py        # MemberAuthMiddleware (the gatekeeper)
├── creator_auth.py      # Standalone creator OAuth script (Desktop mode)
├── test_suite.py        # Unit tests (15 tests)
├── requirements.txt
├── .env.example
├── Dockerfile           # Production container (non-root, healthcheck)
├── docker-compose.yml   # Single-replica deployment
└── README.md
```

## Security Features

- **OAuth CSRF protection**: signed `state` parameter in httpOnly cookie, validated on callback
- **JWT hardening**: HS256 with `iss`/`aud`/`jti`/`exp` claims, 60-minute expiry
- **Admin endpoints**: require `X-Admin-Key` header, constant-time comparison (`hmac.compare_digest`)
- **Creator allowlist**: only approved Google accounts can complete the creator OAuth flow
- **No user token storage**: access tokens are never persisted — only email + channel ID
- **Write serialization**: `asyncio.Lock` around all DB writes prevents race conditions
- **Empty-wipe guard**: skips member table wipe if API returns empty but members exist
- **Security headers**: `X-Content-Type-Options`, `X-Frame-Options`, HSTS
- **DB file permissions**: `0600` (owner-only)
- **Fail-fast startup**: refuses to start without `JWT_SECRET` and Google credentials

## Docker

```bash
# Build and run
docker-compose up -d

# View logs
docker logs youtube-member-auth

# Health check
curl http://localhost:8000/health
```

> **Important**: SQLite does not scale horizontally. Always use `replicas: 1`.
> For multi-node deployment, switch to PostgreSQL and update `database.py`.

## Run Tests

```bash
source venv/bin/activate
python3 test_suite.py
```

## Tech Stack

| Layer | Choice | Why |
|-------|--------|-----|
| Framework | **FastAPI** | Async, fast, automatic OpenAPI docs |
| Database | **SQLite** (aiosqlite) | Zero-config, local, perfect for PoC |
| Auth | **Google OAuth 2.0** | Industry standard, YouTube-native |
| Sessions | **JWT** (python-jose) | Stateless, no server-side session store |
| HTTP Client | **httpx** | Async, modern, clean API |
| Background | **asyncio** loop | No external scheduler needed |

## License

MIT — Use it, fork it, ship it.
