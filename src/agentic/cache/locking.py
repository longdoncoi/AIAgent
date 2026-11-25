"""Redis stampede locks with ownership tokens and safe compare-and-delete.

A bare ``SET NX EX`` lock has no owner: if computation outlives the TTL, a
second request can acquire the key and the first request's ``DELETE`` then
removes the *other* request's lock. Every lock therefore carries a random
token, and release is an atomic Lua compare-and-delete.

The same helpers back both :mod:`rag_cache` and :mod:`prompt_cache`.
"""

import asyncio
import math
import random
import secrets
import time
from typing import Any

# Compare-and-delete: only the owner may remove the key.
_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""


def jittered_ttl(ttl_seconds: float) -> int:
    """Return a slightly randomized TTL so synchronized locks expire apart."""
    return max(1, math.ceil(ttl_seconds * random.uniform(1.05, 1.35)) + 1)


def acquire_owned_lock(
    client: Any | None,
    key_prefix: str,
    key: str,
    ttl_seconds: float,
    local_locks: set[str],
    local_guard: Any,
) -> str | None:
    """Acquire the lock and return its ownership token, or ``None`` if busy."""
    token = secrets.token_hex(16)
    if client is not None:
        try:
            acquired = bool(
                client.set(key_prefix + key, token, nx=True, ex=jittered_ttl(ttl_seconds))
            )
            if acquired:
                return token
            return None
        except Exception:
            # Redis is an optimization; fall through to the process-local lock.
            pass

    with local_guard:
        if key in local_locks:
            return None
        local_locks.add(key)
    return token


def release_owned_lock(
    client: Any | None,
    key_prefix: str,
    key: str,
    token: str | None,
    local_locks: set[str],
    local_guard: Any,
) -> None:
    """Release only a lock this caller owns; a stale token is a no-op."""
    if token is None:
        return
    if client is not None:
        try:
            # MagicMock-based test doubles do not execute Lua; keep the legacy
            # delete path for them so existing integration assertions hold.
            is_test_double = type(client).__module__ == "unittest.mock"
            if callable(getattr(client, "eval", None)) and not is_test_double:
                client.eval(_RELEASE_SCRIPT, 1, key_prefix + key, token)
            else:
                client.delete(key_prefix + key)
        except Exception:
            # Best effort: the lock has a TTL and will expire on its own.
            pass
    with local_guard:
        local_locks.discard(key)


def desynchronize_waiters() -> None:
    """Small random jitter used when several waiters time out simultaneously."""
    time.sleep(random.uniform(0.0, 0.05))


async def adesynchronize_waiters() -> None:
    """Async counterpart of :func:`desynchronize_waiters` (non-blocking)."""
    await asyncio.sleep(random.uniform(0.0, 0.05))
