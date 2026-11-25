"""Tenant-safe thread ownership and internal checkpoint identities."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from functools import lru_cache
from typing import Any

from src.agentic.core.config import get_settings, is_strict_env

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
        # Reverse index used by the Right-to-be-Forgotten purge to enumerate
        # every public thread owned by a principal.
        self._owner_threads: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()
        self._redis: Any | None = None

    @staticmethod
    def _registry_id(thread_id: str, tenant_id: str) -> str:
        return f"{tenant_id.strip().lower() or 'default'}\x00{thread_id}"

    def _key(self, thread_id: str, tenant_id: str = "default") -> str:
        digest = hashlib.sha256(self._registry_id(thread_id, tenant_id).encode("utf-8")).hexdigest()
        return f"agentic:thread-owner:{digest}"

    def _owner_index_key(self, owner_id: str) -> str:
        digest = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()
        return f"agentic:owner-threads:{digest}"

    def scoped_thread_id(
        self, thread_id: str, owner_id: str, tenant_id: str = "default"
    ) -> str:
        """Create a stable opaque checkpoint key without exposing the principal ID."""
        secret = self.settings.checkpoint_signing_key.encode("utf-8")
        digest = hmac.new(
            secret,
            f"{tenant_id.strip().lower()}\x00{owner_id}\x00{thread_id}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"thread:{digest}"

    def candidate_scoped_thread_ids(
        self, thread_id: str, owner_id: str, tenant_id: str = "default"
    ) -> list[str]:
        """Return candidate scoped thread IDs across active and rotated HMAC keys."""
        keys = getattr(
            self.settings,
            "checkpoint_signing_keys",
            (self.settings.checkpoint_signing_key,),
        )
        scoped: list[str] = []
        for raw_key in keys:
            digest = hmac.new(
                str(raw_key).encode("utf-8"),
                f"{tenant_id.strip().lower()}\x00{owner_id}\x00{thread_id}".encode(),
                hashlib.sha256,
            ).hexdigest()
            candidate = f"thread:{digest}"
            if candidate not in scoped:
                scoped.append(candidate)
        return scoped

    async def _client(self) -> Any | None:
        # Unit tests must be hermetic even when a Redis service happens to be
        # present on the CI runner. Dedicated integration tests inject a client
        # explicitly when Redis behaviour is under test.
        if self.settings.environment == "test":
            return None
        if self._redis is not None:
            return self._redis
        try:
            import redis.asyncio as redis

            state_redis_url = self.settings.redis_state_url or self.settings.redis_url
            client = redis.from_url(state_redis_url, socket_timeout=1.0)
            await client.ping()
            self._redis = client
            return client
        except Exception as exc:
            if is_strict_env(self.settings):
                raise ThreadRegistryUnavailable(
                    "Distributed thread ownership registry is unavailable."
                ) from exc
            logger.debug("Using process-local thread registry: %s", exc)
            return None

    async def _handle_redis_failure(
        self, operation: str, client: Any, exc: Exception
    ) -> None:
        """Drop a failed client and fail closed for distributed production/staging state."""
        if self._redis is client:
            self._redis = None
        try:
            await client.aclose()
        except Exception:
            logger.debug("Failed to close Redis client after %s error", operation, exc_info=True)
        if is_strict_env(self.settings):
            raise ThreadRegistryUnavailable(
                f"Distributed thread {operation} registry is unavailable."
            ) from exc
        logger.warning(
            "Redis thread %s failed; using process-local registry outside production: %s",
            operation,
            exc,
        )

    async def claim(
        self, thread_id: str, owner_id: str, tenant_id: str = "default"
    ) -> str:
        """Atomically claim a thread or verify that the current caller owns it."""
        client = await self._client()
        registry_id = self._registry_id(thread_id, tenant_id)
        key = self._key(thread_id, tenant_id)
        if client is not None:
            try:
                created = await client.set(key, owner_id, nx=True, ex=86400 * 30)
                current = owner_id if created else await client.get(key)
                if isinstance(current, bytes):
                    current = current.decode("utf-8")
                if current != owner_id:
                    raise ThreadOwnershipError("Thread belongs to another principal.")
                await client.expire(key, 86400 * 30)
                await client.sadd(self._owner_index_key(owner_id), thread_id)
                await client.expire(self._owner_index_key(owner_id), 86400 * 30)
                return self.scoped_thread_id(thread_id, owner_id, tenant_id)
            except ThreadOwnershipError:
                raise
            except Exception as exc:
                await self._handle_redis_failure("claim", client, exc)

        if client is None or not is_strict_env(self.settings):
            async with self._lock:
                current = self._owners.setdefault(registry_id, owner_id)
                if current != owner_id:
                    raise ThreadOwnershipError("Thread belongs to another principal.")
                self._owner_threads.setdefault(owner_id, set()).add(thread_id)
        return self.scoped_thread_id(thread_id, owner_id, tenant_id)

    async def threads_for_owner(self, owner_id: str) -> list[str]:
        """Enumerate public thread ids owned by ``owner_id`` (for data purge)."""
        client = await self._client()
        if client is not None:
            try:
                members = await client.smembers(self._owner_index_key(owner_id))
                return [
                    m.decode("utf-8") if isinstance(m, bytes) else str(m) for m in members
                ]
            except Exception as exc:
                await self._handle_redis_failure("owner index", client, exc)
        async with self._lock:
            return sorted(self._owner_threads.get(owner_id, set()))

    async def owner(self, thread_id: str, tenant_id: str = "default") -> str | None:
        """Resolve the owner for an administrative HITL resume operation."""
        client = await self._client()
        if client is not None:
            try:
                value = await client.get(self._key(thread_id, tenant_id))
                if isinstance(value, bytes):
                    return value.decode("utf-8")
                return str(value) if value is not None else None
            except Exception as exc:
                await self._handle_redis_failure("owner", client, exc)
        async with self._lock:
            return self._owners.get(self._registry_id(thread_id, tenant_id))

    async def forget_local(
        self,
        *,
        owner_id: str | None = None,
        thread_ids: set[str] | None = None,
    ) -> None:
        """Evict purged thread/owner ownership mappings from process-local caches."""
        async with self._lock:
            if thread_ids:
                for tid in thread_ids:
                    self._owners.pop(tid, None)
                    for owned in self._owner_threads.values():
                        owned.discard(tid)
            if owner_id is not None:
                removed_threads = self._owner_threads.pop(owner_id, set())
                for tid in removed_threads:
                    if self._owners.get(tid) == owner_id:
                        self._owners.pop(tid, None)
                stale = [tid for tid, oid in self._owners.items() if oid == owner_id]
                for tid in stale:
                    self._owners.pop(tid, None)

    async def aclose(self) -> None:
        client = self._redis
        self._redis = None
        if client is not None:
            await client.aclose()


@lru_cache
def get_thread_registry() -> ThreadRegistry:
    return ThreadRegistry()
