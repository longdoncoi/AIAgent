"""Redis Checkpointer for LangGraph Multi-Replica State Persistence."""

import hashlib
import hmac
import logging
import pickle
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
)
from langgraph.checkpoint.memory import MemorySaver

from src.agentic.core.config import get_settings

logger = logging.getLogger(__name__)


class RedisSaver(MemorySaver):
    """Distributed Redis Checkpointer with multi-tier memory caching, async I/O, and HMAC verification.

    Persists LangGraph checkpoints to Redis with TTL for multi-pod consistency.
    Supports both synchronous and asynchronous (aput, aget_tuple) LangGraph execution.
    """

    def __init__(
        self,
        redis_url: str | None = None,
        ttl_seconds: int | None = None,
        key_prefix: str = "agentic:checkpoint",
    ) -> None:
        super().__init__()
        self.settings = get_settings()
        self.redis_url = redis_url or self.settings.redis_url
        self.ttl = (
            ttl_seconds if ttl_seconds is not None else self.settings.redis_checkpoint_ttl_seconds
        )
        self.key_prefix = key_prefix
        self._secret_key = self.settings.checkpoint_signing_key.encode("utf-8")
        self._redis_client: Any = None
        self._async_redis_client: Any = None
        self._init_redis()

    def _init_redis(self) -> None:
        """Initializes both sync and async connections to Redis server."""
        if not self.redis_url:
            return
        try:
            import redis

            client = redis.Redis.from_url(self.redis_url, socket_timeout=1.0)
            client.ping()
            self._redis_client = client
            logger.info(
                f"Initialized sync RedisSaver backend at {self.redis_url} (TTL={self.ttl}s)"
            )
        except Exception:
            try:
                from src.agentic.core.redis import get_redis_client

                pool_client = get_redis_client(self.redis_url)
                if pool_client is not None:
                    pool_client.ping()
                    self._redis_client = pool_client
                    logger.info(
                        f"Initialized sync RedisSaver backend from connection pool at {self.redis_url}"
                    )
            except Exception as e:
                logger.debug(
                    f"Redis backend not reachable for RedisSaver ({e}). Operating in memory mode."
                )
                self._redis_client = None

        if self._redis_client is not None:
            try:
                import redis.asyncio as aioredis

                self._async_redis_client = aioredis.from_url(self.redis_url, socket_timeout=1.0)
                logger.info(f"Initialized async RedisSaver backend at {self.redis_url}")
            except Exception:
                try:
                    from src.agentic.core.redis import get_async_redis_client

                    self._async_redis_client = get_async_redis_client(self.redis_url)
                except Exception as e:
                    logger.debug(f"Async Redis backend not reachable for RedisSaver ({e}).")
                    self._async_redis_client = None
        else:
            self._async_redis_client = None

    @property
    def is_redis_active(self) -> bool:
        return self._redis_client is not None or self._async_redis_client is not None

    def _redis_key(self, thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> str:
        return f"{self.key_prefix}:{thread_id}:{checkpoint_ns}:{checkpoint_id}"

    def _thread_index_key(self, thread_id: str, checkpoint_ns: str) -> str:
        return f"{self.key_prefix}:index:{thread_id}:{checkpoint_ns}"

    def _sign_and_serialize(self, obj: Any, use_json: bool = False) -> bytes:
        """Serializes checkpoint object and prefixes with HMAC signature to prevent tampering.
        Defaults to HMAC-SHA256 Pickle for full Python object fidelity; supports use_json=True.
        """
        if use_json and isinstance(obj, (dict, list, str, int, float, bool)):
            try:
                import json

                raw_json = json.dumps(obj).encode("utf-8")
                sig = (
                    hmac.new(self._secret_key, raw_json, hashlib.sha256).hexdigest().encode("ascii")
                )
                return b"HMAC-JSON:" + sig + b":" + raw_json
            except (TypeError, ValueError):
                pass

        raw = pickle.dumps(obj)
        sig = hmac.new(self._secret_key, raw, hashlib.sha256).hexdigest().encode("ascii")
        return b"HMAC-SHA256:" + sig + b":" + raw

    def _verify_and_deserialize(self, payload: bytes) -> Any | None:
        """Verifies HMAC signature and deserializes checkpoint payload securely (supporting JSON & Pickle)."""
        if payload.startswith(b"HMAC-JSON:"):
            parts = payload.split(b":", 2)
            if len(parts) == 3:
                sig = parts[1]
                raw = parts[2]
                expected_sig = (
                    hmac.new(self._secret_key, raw, hashlib.sha256).hexdigest().encode("ascii")
                )
                if not hmac.compare_digest(sig, expected_sig):
                    logger.error(
                        "Security Alert: Redis checkpoint HMAC-JSON signature mismatch! Tampered payload rejected."
                    )
                    return None
                import json

                return json.loads(raw.decode("utf-8"))

        if payload.startswith(b"HMAC-SHA256:"):
            parts = payload.split(b":", 2)
            if len(parts) == 3:
                sig = parts[1]
                raw = parts[2]
                expected_sig = (
                    hmac.new(self._secret_key, raw, hashlib.sha256).hexdigest().encode("ascii")
                )
                if not hmac.compare_digest(sig, expected_sig):
                    logger.error(
                        "Security Alert: Redis checkpoint HMAC signature mismatch! Tampered payload rejected."
                    )
                    return None
                return pickle.loads(raw)
        if self.settings.allow_legacy_unsigned_checkpoints:
            logger.warning("Loading an unsigned legacy checkpoint; migrate it immediately.")
            try:
                return pickle.loads(payload)
            except Exception as exc:
                logger.warning("Could not deserialize legacy Redis payload: %s", exc)
        else:
            logger.error("Security Alert: rejected unsigned Redis checkpoint payload.")
        return None

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Saves checkpoint to in-memory state and persists signed serialized payload to Redis."""
        res_config = super().put(config, checkpoint, metadata, new_versions)

        if self._redis_client is not None:
            try:
                thread_id = str(config["configurable"]["thread_id"])
                checkpoint_ns = str(config["configurable"].get("checkpoint_ns", ""))
                checkpoint_id = str(checkpoint["id"])
                effective_ttl = int(config["configurable"].get("checkpoint_ttl") or self.ttl)

                saved = self.storage[thread_id][checkpoint_ns].get(checkpoint_id)
                if saved:
                    payload = self._sign_and_serialize(
                        {
                            "storage_entry": saved,
                            "blobs": {
                                (k, v): self.blobs[(thread_id, checkpoint_ns, k, v)]
                                for k, v in new_versions.items()
                                if (thread_id, checkpoint_ns, k, v) in self.blobs
                            },
                        }
                    )
                    r_key = self._redis_key(thread_id, checkpoint_ns, checkpoint_id)
                    self._redis_client.set(r_key, payload, ex=effective_ttl)

                    idx_key = self._thread_index_key(thread_id, checkpoint_ns)
                    self._redis_client.sadd(idx_key, checkpoint_id)
                    self._redis_client.expire(idx_key, effective_ttl)
            except Exception as e:
                logger.debug(f"Failed to persist checkpoint to Redis ({e})")

        return res_config

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Asynchronously saves checkpoint to in-memory state and Redis without event loop blocking."""
        res_config = super().put(config, checkpoint, metadata, new_versions)

        if self._async_redis_client is not None:
            try:
                thread_id = str(config["configurable"]["thread_id"])
                checkpoint_ns = str(config["configurable"].get("checkpoint_ns", ""))
                checkpoint_id = str(checkpoint["id"])
                effective_ttl = int(config["configurable"].get("checkpoint_ttl") or self.ttl)

                saved = self.storage[thread_id][checkpoint_ns].get(checkpoint_id)
                if saved:
                    payload = self._sign_and_serialize(
                        {
                            "storage_entry": saved,
                            "blobs": {
                                (k, v): self.blobs[(thread_id, checkpoint_ns, k, v)]
                                for k, v in new_versions.items()
                                if (thread_id, checkpoint_ns, k, v) in self.blobs
                            },
                        }
                    )
                    r_key = self._redis_key(thread_id, checkpoint_ns, checkpoint_id)
                    await self._async_redis_client.set(r_key, payload, ex=effective_ttl)

                    idx_key = self._thread_index_key(thread_id, checkpoint_ns)
                    await self._async_redis_client.sadd(idx_key, checkpoint_id)
                    await self._async_redis_client.expire(idx_key, effective_ttl)
            except Exception as e:
                logger.debug(f"Failed to persist async checkpoint to Redis ({e})")

        return res_config

    def cleanup_inactive_threads(self, max_idle_seconds: int = 86400 * 30) -> int:
        """Evicts expired or inactive thread checkpoints from Redis to optimize cluster memory."""
        cleaned_threads = 0
        if self._redis_client is not None:
            try:
                index_pattern = f"{self.key_prefix}:index:*"
                keys = self._redis_client.keys(index_pattern)
                for idx_key in keys:
                    ttl_left = self._redis_client.ttl(idx_key)
                    members = self._redis_client.smembers(idx_key)
                    if not members or ttl_left == -2:
                        self._redis_client.delete(idx_key)
                        cleaned_threads += 1
                    elif ttl_left == -1:
                        self._redis_client.expire(idx_key, max_idle_seconds)
            except Exception as e:
                logger.warning(f"Error during RedisSaver cleanup_inactive_threads: {e}")

        # Also purge any empty in-memory threads
        for t_id in list(self.storage.keys()):
            if not self.storage[t_id] or all(not v for v in self.storage[t_id].values()):
                del self.storage[t_id]
                cleaned_threads += 1
        return cleaned_threads

    async def acleanup_inactive_threads(self, max_idle_seconds: int = 86400 * 30) -> int:
        """Asynchronously scan indexes; never issue a cluster-blocking Redis KEYS call."""
        cleaned_threads = 0
        if self._async_redis_client is not None:
            try:
                pattern = f"{self.key_prefix}:index:*"
                async for idx_key in self._async_redis_client.scan_iter(match=pattern, count=200):
                    ttl_left = await self._async_redis_client.ttl(idx_key)
                    members = await self._async_redis_client.scard(idx_key)
                    if not members or ttl_left == -2:
                        await self._async_redis_client.delete(idx_key)
                        cleaned_threads += 1
                    elif ttl_left == -1:
                        await self._async_redis_client.expire(idx_key, max_idle_seconds)
            except Exception as exc:
                logger.warning("Error during async Redis checkpoint cleanup: %s", exc)

        for thread_id in list(self.storage.keys()):
            if not self.storage[thread_id] or all(
                not namespace for namespace in self.storage[thread_id].values()
            ):
                del self.storage[thread_id]
                cleaned_threads += 1
        return cleaned_threads

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Retrieves checkpoint tuple from memory, or pulls from Redis if missing locally."""
        thread_id = str(config["configurable"]["thread_id"])
        checkpoint_ns = str(config["configurable"].get("checkpoint_ns", ""))
        checkpoint_id = get_checkpoint_id(config)

        # 1. First check local memory
        local_result = super().get_tuple(config)
        if local_result is not None:
            return local_result

        # 2. If not found locally and Redis is connected, try loading from Redis
        if self._redis_client is not None:
            try:
                if checkpoint_id:
                    target_id = checkpoint_id
                else:
                    idx_key = self._thread_index_key(thread_id, checkpoint_ns)
                    members = self._redis_client.smembers(idx_key)
                    if not members:
                        return None
                    target_id = max(
                        m.decode("utf-8") if isinstance(m, bytes) else str(m) for m in members
                    )

                r_key = self._redis_key(thread_id, checkpoint_ns, target_id)
                raw_payload = self._redis_client.get(r_key)
                if raw_payload:
                    data = self._verify_and_deserialize(raw_payload)
                    if data:
                        self.storage[thread_id][checkpoint_ns][target_id] = data["storage_entry"]
                        for (k, v), blob in data.get("blobs", {}).items():
                            self.blobs[(thread_id, checkpoint_ns, k, v)] = blob

                        return super().get_tuple(
                            {
                                "configurable": {
                                    "thread_id": thread_id,
                                    "checkpoint_ns": checkpoint_ns,
                                    "checkpoint_id": target_id,
                                }
                            }
                        )
            except Exception as e:
                logger.debug(f"Failed to load checkpoint from Redis ({e})")

        return None

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Asynchronously retrieves checkpoint tuple from memory or Redis."""
        thread_id = str(config["configurable"]["thread_id"])
        checkpoint_ns = str(config["configurable"].get("checkpoint_ns", ""))
        checkpoint_id = get_checkpoint_id(config)

        local_result = super().get_tuple(config)
        if local_result is not None:
            return local_result

        if self._async_redis_client is not None:
            try:
                if checkpoint_id:
                    target_id = checkpoint_id
                else:
                    idx_key = self._thread_index_key(thread_id, checkpoint_ns)
                    members = await self._async_redis_client.smembers(idx_key)
                    if not members:
                        return None
                    target_id = max(
                        m.decode("utf-8") if isinstance(m, bytes) else str(m) for m in members
                    )

                r_key = self._redis_key(thread_id, checkpoint_ns, target_id)
                raw_payload = await self._async_redis_client.get(r_key)
                if raw_payload:
                    data = self._verify_and_deserialize(raw_payload)
                    if data:
                        self.storage[thread_id][checkpoint_ns][target_id] = data["storage_entry"]
                        for (k, v), blob in data.get("blobs", {}).items():
                            self.blobs[(thread_id, checkpoint_ns, k, v)] = blob

                        return super().get_tuple(
                            {
                                "configurable": {
                                    "thread_id": thread_id,
                                    "checkpoint_ns": checkpoint_ns,
                                    "checkpoint_id": target_id,
                                }
                            }
                        )
            except Exception as e:
                logger.debug(f"Failed to load async checkpoint from Redis ({e})")

        return None
