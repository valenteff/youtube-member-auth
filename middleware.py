"""
Authorization middleware — the Gatekeeper.

Extracts the JWT from the request, identifies the user's channel_id,
and checks active_members. Returns 403 if not a member.
"""
from __future__ import annotations

import logging
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from jose import jwt, JWTError
from config import settings
import database as db

logger = logging.getLogger("middleware")


class MemberAuthMiddleware(BaseHTTPMiddleware):
    """
    Protects routes starting with /protected/.
    Checks JWT validity + active membership.
    """

    async def dispatch(self, request, call_next):
        path = request.url.path

        # Only gate /protected/* routes
        if path != "/protected" and not path.startswith("/protected/"):
            return await call_next(request)

        # ── Extract token from Authorization header ──
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"error": "missing_token", "message": "Authorization Bearer token required"},
            )

        token = auth_header.removeprefix("Bearer ").strip()

        # ── Verify JWT (with issuer + audience validation) ──
        try:
            payload = jwt.decode(
                token,
                settings.JWT_SECRET,
                algorithms=[settings.JWT_ALGORITHM],
                issuer=settings.JWT_ISSUER,
                audience=settings.JWT_AUDIENCE,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except JWTError:
            return JSONResponse(
                status_code=401,
                content={"error": "invalid_token", "message": "Token is invalid or expired"},
            )

        user_id = payload.get("sub")
        if not user_id:
            return JSONResponse(status_code=401, content={"error": "invalid_token", "message": "No subject in token"})

        # ── Load user from DB + check membership (with error handling) ──
        try:
            user = await db.get_user_by_id(user_id)
            if not user:
                return JSONResponse(
                    status_code=403,
                    content={"error": "forbidden", "message": "User not found. Please log in again."},
                )

            channel_id = user.get("youtube_channel_id")
            if not channel_id:
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": "no_channel",
                        "message": "No YouTube channel linked to your account. "
                                   "Ensure your Google account has a public YouTube channel.",
                    },
                )

            is_member = await db.is_active_member(channel_id)
            if not is_member:
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": "not_a_member",
                        "message": "Access denied. Become a channel member to unlock this content.",
                    },
                )

            # ── Attach user info to request state for downstream handlers ──
            request.state.user = user
            return await call_next(request)

        except Exception:
            logger.exception("Database error during authorization check")
            return JSONResponse(
                status_code=503,
                content={"error": "service_unavailable", "message": "Temporary database error. Try again."},
            )
