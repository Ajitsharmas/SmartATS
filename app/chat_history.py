# ---------------------------------------------------------------------------
# Purpose: Redis-backed chat history for the RAG Q&A feature (Phase 4)
# ---------------------------------------------------------------------------
#
# Each conversation is one Redis list keyed by (user_id, application_id,
# session_id). Turns are appended as JSON-encoded strings; LTRIM keeps the
# list capped at the sliding-window size; EXPIRE refreshes the TTL on every
# write so active conversations stay alive but idle ones eventually clear.
#
# Why Redis and not Postgres:
#   - First-class TTL: conversations auto-expire without a cleanup job
#   - Native list operations (RPUSH / LTRIM / LRANGE) match exactly the
#     append-only sliding-window pattern of chat history
#   - Sub-millisecond writes; chat is on the hot path of the user experience
#   - We already have Redis in the stack (Celery + rate limiter + query cache)
#
# Graceful degradation: if Redis is unreachable for any operation, the helpers
# treat history as empty / drop writes silently. Chat still works, just
# without prior context — same fallback philosophy as the query cache.

import hashlib
import json
import uuid

from redis import Redis
from redis.exceptions import RedisError

from app.config import settings
from app.models import ChatTurn


# How many entries to keep per conversation (6 user + 6 assistant turns).
# Matches the sliding window described in the Phase 4 design doc.
CHAT_HISTORY_MAX_ENTRIES = 12

# Conversations expire 24 hours after the last message. Refreshed on every
# write — active conversations stay alive, idle ones eventually clear.
CHAT_HISTORY_TTL_SECONDS = 86400


_redis_client: Redis | None = None


def _get_redis_client() -> Redis:
    """Lazily construct a Redis client for chat history."""
    global _redis_client
    if _redis_client is None:
        _redis_client = Redis.from_url(
            settings.RATE_LIMITER_STORAGE_URL,
            decode_responses=True,
        )
    return _redis_client


def _key(user_id: int, application_id: int, session_id: str) -> str:
    """
    Build the Redis key for a conversation.

    Including `user_id` in the key gives defense-in-depth on top of the
    endpoint's ownership check: even if a malicious client tampered with
    session IDs, they could not read another recruiter's history because
    the key would not match.

    session_id is hashed because user-supplied UUIDs are already
    well-formed but we want a predictable max key length regardless.
    """
    sid = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return f"chat:{user_id}:{application_id}:{sid}"


def new_session_id() -> str:
    """Generate a fresh session ID (UUID v4 as a hex string)."""
    return uuid.uuid4().hex


def load_history(user_id: int, application_id: int, session_id: str) -> list[ChatTurn]:
    """
    Return the conversation history for a session, oldest turn first.

    Returns an empty list if the session does not exist, has expired, or
    if Redis is unreachable.
    """
    try:
        raw = _get_redis_client().lrange(_key(user_id, application_id, session_id), 0, -1)
    except RedisError:
        return []

    turns: list[ChatTurn] = []
    for item in raw:
        try:
            data = json.loads(item)
            turns.append(ChatTurn(role=data["role"], content=data["content"]))
        except (json.JSONDecodeError, KeyError, ValueError):
            # Skip malformed entries — don't break the whole conversation
            continue
    return turns


def append_turn(
    user_id: int,
    application_id: int,
    session_id: str,
    role: str,
    content: str,
) -> None:
    """
    Append a single turn to the conversation, trim to the sliding-window
    size, and refresh the TTL.

    All three operations happen as separate Redis commands. We could pipeline
    them for atomicity, but the cost of three round-trips is negligible and
    not pipelining keeps the code easier to read. The worst-case interleaving
    is benign: if a concurrent read happens between RPUSH and LTRIM, it sees
    one extra turn — not a correctness problem for chat.
    """
    if not content:
        return
    try:
        client = _get_redis_client()
        key = _key(user_id, application_id, session_id)
        client.rpush(key, json.dumps({"role": role, "content": content}))
        client.ltrim(key, -CHAT_HISTORY_MAX_ENTRIES, -1)
        client.expire(key, CHAT_HISTORY_TTL_SECONDS)
    except RedisError:
        # Conversation continues without persistence — acceptable
        pass


def reset_session(user_id: int, application_id: int, session_id: str) -> None:
    """Delete the conversation. Used by the 'New conversation' button."""
    try:
        _get_redis_client().delete(_key(user_id, application_id, session_id))
    except RedisError:
        pass


def clear_application_chats(application_id: int) -> int:
    """
    Delete every chat session for an application, across all sessions and
    all users. Used by the /reanalyze endpoint to invalidate stale
    conversation history when the underlying resume embeddings change.

    Returns the number of sessions deleted. Returns 0 silently on Redis
    errors — same graceful-degradation philosophy as the rest of the module.

    Uses SCAN (not KEYS) so the operation is safe to run on a busy Redis
    without blocking other commands. The pattern matches any user_id and
    any session_id for the given application_id.
    """
    try:
        client = _get_redis_client()
        pattern = f"chat:*:{application_id}:*"
        keys = list(client.scan_iter(match=pattern))
        if not keys:
            return 0
        return client.delete(*keys)
    except RedisError:
        return 0
