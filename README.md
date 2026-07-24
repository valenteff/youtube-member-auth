# 🔐 YouTube Member Auth Gateway

A lightweight authentication gateway that restricts access to protected content (dashboards, API endpoints, trading scripts) **exclusively to active YouTube channel members**.

Built with **FastAPI + SQLite + Google OAuth 2.0 + YouTube Data API v3**.

## How It Works

```
┌──────────────┐       ┌──────────────────────────┐       ┌─────────────────┐
│   End User   │──────▶│  FastAPI Gateway          │──────▶│  SQLite (local)  │
│  (browser)   │◀──────│                           │       │                  │
└──────────────┘       │  1. OAuth login (Google)  │       │  • users         │
                       │  2. JWT session (24h)     │       │  • creator_tokens│
┌──────────────┐       │  3. Member check (403/200)│       │  • active_members│
│   Creator    │──────▶│                           │       └─────────────────┘
│ (one-time)   │       └───────────┬──────────────┘
└──────────────┘                   │                ┌─────────────────────┐
                         ┌──────────▼──────────┐    │  YouTube Data API   │
                         │  Background Sync     │───▶│  v3  members.list   │
                         │  (hourly, automatic) │    │  (creator scope)    │
                         └─────────────────────┘     └─────────────────────┘
```

### The Two OAuth Flows

| Flow | Scope | Purpose |
|------|-------|---------|
| **Creator (Admin)** | `youtube.channel-memberships.creator` | One-time setup. Gets a `refresh_token` so the server can query members offline. |
| **User (Client)** | `youtube` (read-only) | End-user login. Extracts email + YouTube `channelId`. Returns a JWT. |

### The Gatekeeper Middleware

Every request to `/protected/*` passes through `MemberAuthMiddleware`:

1. Extracts JWT from `Authorization: Bearer <token>`
2. Looks up the user's `youtube_channel_id`
3. Queries `active_members` table for a match
4. **Match → 200 OK** | **No match → 403 Forbidden**

### Background Sync

An `asyncio` loop runs every hour (configurable):
- Uses the creator's stored `refresh_token` to get a fresh access token
- Calls `youtube.members.list` (paginated, handles up to 50/page)
- **Overwrites** the entire `active_members` table — canceled memberships are automatically dropped

## Quick Start

### Prerequisites

- Python 3.9+
- A YouTube channel with **channel memberships enabled**
- A Google Cloud project with YouTube Data API v3 enabled

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
2. **Enable YouTube Data API v3** (APIs & Services → Library → search → Enable)
3. **Configure OAuth Consent Screen** (External):
   - Add scopes:
     - `https://www.googleapis.com/auth/youtube.channel-memberships.creator`
     - `https://www.googleapis.com/auth/youtube`
     - `openid`, `email`, `profile`
   - Add yourself as a **Test User**
4. **Create OAuth Credentials** → Web application:
   - Authorized redirect URIs:
     ```
     http://localhost:8000/auth/google/callback
     http://localhost:8000/admin/google/callback
     ```
   - Copy **Client ID** and **Client Secret**
5. Paste them into `.env`:
   ```bash
   GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=your-client-secret
   JWT_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")
   ```

### 3. Run

```bash
source venv/bin/activate
uvicorn main:app --reload --port 8000
```

### 4. Creator Setup (one-time)

Open `http://localhost:8000/admin/login` in your browser. Log in with the Google account that owns your YouTube channel. Authorize the request.

You'll see:
```json
{
  "message": "Creator authenticated successfully",
  "channel_id": "UCxxxxxxxxx",
  "has_refresh_token": true
}
```

### 5. Sync Members

```bash
# Manual sync (immediate):
curl http://localhost:8000/admin/sync

# Or just wait — the background loop syncs every hour
```

### 6. Test as a User

```bash
# Login (opens Google consent in browser):
open http://localhost:8000/login
```

The response includes a JWT. Use it:

```bash
curl -H "Authorization: Bearer <JWT>" \
     http://localhost:8000/protected/strategy-code
```

**If the user is an active member:**
```json
{
  "message": "Welcome! You are an active channel member.",
  "strategy_code": "def my_strategy(): return 'alpha_signal_v3'",
  "accessed_by": "user@example.com"
}
```

**If NOT a member:**
```json
{
  "error": "not_a_member",
  "message": "Access denied. Become a channel member to unlock this content."
}
```

## API Reference

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/` | — | Landing page with links |
| GET | `/login` | — | Redirect to Google OAuth (user flow) |
| GET | `/auth/google/callback` | — | OAuth callback, returns JWT |
| GET | `/admin/login` | — | Redirect to Google OAuth (creator flow) |
| GET | `/admin/google/callback` | — | Stores creator `refresh_token` |
| GET | `/admin/sync` | — | Manually trigger member sync |
| GET | `/protected/strategy-code` | JWT + Membership | Protected example endpoint |
| GET | `/health` | — | Health check + member count |

## Database Schema

```sql
-- End-users who logged in via Google
CREATE TABLE users (
    user_id             TEXT PRIMARY KEY,   -- Google sub
    email               TEXT NOT NULL,
    youtube_channel_id  TEXT,               -- public channel ID
    oauth_access_token  TEXT,
    created_at          TEXT DEFAULT (datetime('now'))
);

-- Creator's tokens (single row, id=1)
CREATE TABLE creator_tokens (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    email            TEXT,
    channel_id       TEXT,
    access_token     TEXT,
    refresh_token    TEXT,                  -- persistent, for offline API calls
    token_expires_at TEXT,
    updated_at       TEXT DEFAULT (datetime('now'))
);

-- Current paying members (synced from YouTube API)
CREATE TABLE active_members (
    youtube_channel_id  TEXT PRIMARY KEY,
    membership_level    TEXT,
    last_updated        TEXT DEFAULT (datetime('now'))
);
```

## Quota Handling

The YouTube Data API v3 has a daily quota (default: 10,000 units). Each `members.list` call costs ~1 unit.

This PoC explicitly handles:

| HTTP Status | Error Reason | Behavior |
|-------------|-------------|----------|
| 403 | `quotaExceeded` | Log error, sync fails gracefully, retries next cycle |
| 403 | `dailyLimitExceeded` | Same — waits for quota reset |
| 429 | `rateLimitExceeded` | Same — backs off automatically |
| 403 | Other | Log error with details |

The `QuotaExceededError` exception is raised and caught in `sync.py`, preventing crashes.

## Project Structure

```
youtube-member-auth/
├── main.py            # FastAPI app — routes, lifespan, handlers
├── config.py          # Settings (reads .env)
├── database.py        # SQLite async layer (aiosqlite)
├── google_auth.py     # OAuth + YouTube API + quota handling
├── sync.py            # Background sync loop (asyncio)
├── middleware.py      # MemberAuthMiddleware (gatekeeper)
├── requirements.txt
├── .env.example
└── README.md
```

## Tech Stack

| Layer | Choice | Why |
|-------|--------|-----|
| Framework | **FastAPI** | Async, fast, great DX, automatic OpenAPI docs |
| Database | **SQLite** (aiosqlite) | Zero-config, local, perfect for PoC |
| Auth | **Google OAuth 2.0** | Industry standard, YouTube-native |
| Sessions | **JWT** (python-jose) | Stateless, no server-side session store |
| HTTP Client | **httpx** | Async, modern, clean API |
| Background | **asyncio** loop | No external scheduler needed for PoC |

## Security Notes

- The `refresh_token` is stored in plaintext in SQLite. For production, encrypt it (e.g., AES-256-GCM with a key from a KMS or environment variable).
- JWT expiry is 24h. Adjust in `main.py` if needed.
- SQLite DB file (`members.db`) should not be committed. It's in `.gitignore`.
- The `.env` file contains secrets. Also in `.gitignore`.

## License

MIT — Use it, fork it, ship it.

---

Built as a Proof of Concept for gating premium content behind YouTube channel memberships.
