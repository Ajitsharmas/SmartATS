# ---------------------------------------------------------------------------
# Purpose: Shared SlowAPI rate limiter instance used across all routers
# ---------------------------------------------------------------------------

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import settings


def get_user_key(request: Request) -> str:
    """
    Rate-limit key derived from the JWT subject (email).
    Falls back to the client IP if the token is absent or invalid.
    Used for endpoints where the caller is always authenticated.
    """
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if token:
        try:
            from app.security import decode_token
            payload = decode_token(token)
            email = payload.get("sub")
            if email:
                return f"user:{email}"
        except Exception:
            pass
    return f"ip:{get_remote_address(request)}"


# Counters are stored in Redis so they persist across restarts and are shared
# across all web container instances. The same Redis instance used by Celery
# is reused here — no extra infrastructure needed.
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=settings.RATE_LIMITER_STORAGE_URL,
)
