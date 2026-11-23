"""Tenant-safe thread ownership and internal checkpoint identities."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from functools import lru_cache
from typing import Any

from src.agentic.core.config import get_settings

logger = logging.getLogger(__name__)


class ThreadOwnershipError(PermissionError):
    """Raised when a caller tries to reuse a thread owned by another principal."""


class ThreadRegistryUnavailable(RuntimeError):
    """Raised when the distributed ownership registry is unavailable in production."""


class ThreadRegistry:
    """Claims public thread IDs and maps them to tenant-scoped checkpoint IDs."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._owners: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._redis: Any | None = None

    def _key(self, thread_id: str) -> str:
        digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()
        return f"agentic:thread-owner:{digest}"

    def scoped_thread_id(self, thread_id: str, owner_id: str) -> str:
        """Create a stable opaque checkpoint key without exposing the principal ID."""
        secret = self.settings.checkpoint_signing_key.encode("utf-8")
        digest = hmac.new(
            secret, f"{owner_id}\x00{thread_id}".encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return f"thread:{digest}"

    async def _client(self) -> Any | None:
        if self._redis is not None:
            return self._redis
        try:
            import redis.asyncio as redis

            client = redis.from_url(self.settings.redis_url, socket_timeout=1.0)
            await client.ping()
            self._redis = client
            return client
        except Exception as exc:
            if self.settings.environment == "production":
                raise ThreadRegistryUnavailable(
                    "Distributed thread ownership registry is unavailable."
                ) from exc
            logger.debug("Using process-local thread registry: %s", exc)
            return None

    async def claim(self, thread_id: str, owner_id: str) -> str:
        """Atomically claim a thread or verify that the current caller owns it."""
        client = await self._client()
        key = self._key(thread_id)
        if client is not None:
            created = await client.set(key, owner_id, nx=True, ex=86400 * 30)
            current = owner_id if created else await client.get(key)
            if isinstance(current, bytes):
                current = current.decode("utf-8")
            if current != owner_id:
                raise ThreadOwnershipError("Thread belongs to another principal.")
            await client.expire(key, 86400 * 30)
        else:
            async with self._lock:
                current = self._owners.setdefault(thread_id, owner_id)
                if current != owner_id:
                    raise ThreadOwnershipError("Thread belongs to another principal.")
        return self.scoped_thread_id(thread_id, owner_id)

    async def owner(self, thread_id: str) -> str | None:
        """Resolve the owner for an administrative HITL resume operation."""
        client = await self._client()
        if client is not None:
            value = await client.get(self._key(thread_id))
            if isinstance(value, bytes):
                return value.decode("utf-8")
            return str(value) if value is not None else None
        async with self._lock:
            return self._owners.get(thread_id)

    async def aclose(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None


@lru_cache
def get_thread_registry() -> ThreadRegistry:
    return ThreadRegistry()
