"""Regression tests for v8 Principal Architect Production Readiness fixes.

Covers:
1. RequestIDAndMetricsMiddleware end-to-end SSE / EventSourceResponse latency tracking.
2. RedisSaver multi-pod latest checkpoint consistency (sync & async) + SCAN iteration.
3. PromptCache.clear() non-blocking Redis SCAN iteration.
4. Dispatch client_id attribution defaults to 'unattributed' when missing.
5. Kubernetes OTel Prometheus scrape NetworkPolicy (8889) & shared RWX RAG corpus PVC.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage
from sse_starlette.sse import EventSourceResponse

from src.agentic.api.middleware import RequestIDAndMetricsMiddleware
from src.agentic.cache.prompt_cache import PromptCache
from src.agentic.memory.redis_saver import RedisSaver
from src.agentic.observability.metrics import REQUEST_DURATION
from src.agentic.orchestrator.nodes.dispatch import dispatch_node
from src.agentic.orchestrator.state import AgentState, Plan, Step

ROOT = Path(__file__).resolve().parents[2]


def _counter_and_sum(metric, **labels: str) -> tuple[float, float]:
    count = 0.0
    total = 0.0
    for family in metric.collect():
        for sample in family.samples:
            if sample.labels != labels:
                continue
            if sample.name.endswith("_count"):
                count = float(sample.value)
            elif sample.name.endswith("_sum"):
                total = float(sample.value)
    return count, total


def test_middleware_sse_records_end_to_end_stream_duration() -> None:
    """Streaming/SSE responses wrapped by BaseHTTPMiddleware must record full stream latency."""
    app = FastAPI()
    app.add_middleware(RequestIDAndMetricsMiddleware)

    @app.get("/sse")
    async def sse_endpoint():
        async def _events():
            yield {"data": "chunk-1"}
            await asyncio.sleep(0.18)
            yield {"data": "chunk-2"}

        return EventSourceResponse(_events())

    before_count, before_sum = _counter_and_sum(
        REQUEST_DURATION, component="api", status="success"
    )

    with TestClient(app) as client, client.stream("GET", "/sse") as resp:
        assert resp.status_code == 200
        assert resp.headers.get("x-request-id")
        body = "".join(resp.iter_text())
        assert "chunk-1" in body and "chunk-2" in body

    after_count, after_sum = _counter_and_sum(
        REQUEST_DURATION, component="api", status="success"
    )
    assert after_count - before_count == 1.0
    assert (after_sum - before_sum) >= 0.15


class _SharedFakeRedis:
    """In-memory shared Redis stub implementing both sync and async APIs."""

    def __init__(self) -> None:
        self.kv: dict[str, bytes] = {}
        self.sets: dict[str, set[str]] = {}
        self.ttls: dict[str, int] = {}
        self.keys_called = False
        self.scan_patterns: list[str] = []

    def pipeline(self) -> _SharedFakePipeline:
        return _SharedFakePipeline(self)

    def set(self, key: str, value: bytes, ex: int | None = None) -> bool:
        self.kv[key] = value if isinstance(value, bytes) else str(value).encode("utf-8")
        if ex is not None:
            self.ttls[key] = int(ex)
        return True

    def sadd(self, key: str, member: str) -> int:
        self.sets.setdefault(key, set()).add(str(member))
        return 1

    def get(self, key: str) -> bytes | None:
        return self.kv.get(key)

    def smembers(self, key: str) -> set[bytes]:
        return {m.encode("utf-8") for m in self.sets.get(key, set())}

    def scan_iter(self, match: str | None = None, count: int = 100):
        import fnmatch

        self.scan_patterns.append(str(match or "*"))
        all_keys = list(self.kv.keys()) + list(self.sets.keys())
        for k in all_keys:
            if match is None or fnmatch.fnmatch(k, match):
                yield k.encode("utf-8")

    def keys(self, pattern: str = "*"):
        self.keys_called = True
        raise AssertionError("Production paths must use scan_iter instead of blocking KEYS")

    def ttl(self, key: str) -> int:
        if key not in self.kv and key not in self.sets:
            return -2
        return self.ttls.get(key, -1)

    def delete(self, *keys: str | bytes) -> int:
        removed = 0
        for raw in keys:
            k = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            if k in self.kv:
                del self.kv[k]
                removed += 1
            if k in self.sets:
                del self.sets[k]
                removed += 1
        return removed

    def expire(self, key: str, ttl: int) -> bool:
        self.ttls[key] = ttl
        return True

    async def aget(self, key: str) -> bytes | None:
        return self.get(key)

    async def aclose(self) -> None:
        return None


class _SharedFakePipeline:
    def __init__(self, store: _SharedFakeRedis) -> None:
        self._store = store
        self._ops: list[tuple] = []

    def set(self, key: str, value: bytes, ex: int | None = None) -> _SharedFakePipeline:
        self._ops.append(("set", key, value, ex))
        return self

    def sadd(self, key: str, member: str) -> _SharedFakePipeline:
        self._ops.append(("sadd", key, member))
        return self

    def expire(self, key: str, ttl: int) -> _SharedFakePipeline:
        self._ops.append(("expire", key, ttl))
        return self

    def execute(self) -> list[bool]:
        results: list[bool] = []
        for op in self._ops:
            if op[0] == "set":
                _, key, val, ex = op
                self._store.kv[key] = val if isinstance(val, bytes) else str(val).encode("utf-8")
                if ex is not None:
                    self._store.ttls[key] = int(ex)
                results.append(True)
            elif op[0] == "sadd":
                _, key, member = op
                self._store.sets.setdefault(key, set()).add(str(member))
                results.append(True)
            elif op[0] == "expire":
                _, key, ttl = op
                self._store.ttls[key] = int(ttl)
                results.append(True)
        self._ops.clear()
        return results


class _SharedFakeAsyncRedis:
    """Async adapter backed by the same shared fake store."""

    def __init__(self, store: _SharedFakeRedis) -> None:
        self._store = store

    def pipeline(self):
        return _AsyncFakePipeline(self._store)

    async def get(self, key: str) -> bytes | None:
        return self._store.get(key)

    async def set(self, key: str, value: bytes, ex: int | None = None) -> bool:
        return self._store.set(key, value, ex=ex)

    async def sadd(self, key: str, member: str) -> int:
        return self._store.sadd(key, member)

    async def expire(self, key: str, ttl: int) -> bool:
        return self._store.expire(key, ttl)

    async def smembers(self, key: str) -> set[bytes]:
        return self._store.smembers(key)

    async def aclose(self) -> None:
        return None


class _AsyncFakePipeline(_SharedFakePipeline):
    async def execute(self) -> list[bool]:  # type: ignore[override]
        return super().execute()


@pytest.mark.asyncio
async def test_redis_saver_multi_pod_sync_and_async_latest_read() -> None:
    """Pod1 must observe newer checkpoints written to shared Redis by Pod2."""
    store = _SharedFakeRedis()
    pod1 = RedisSaver(redis_url=None)
    pod2 = RedisSaver(redis_url=None)
    pod1._redis_client = store
    pod2._redis_client = store
    pod1._async_redis_client = _SharedFakeAsyncRedis(store)
    pod2._async_redis_client = _SharedFakeAsyncRedis(store)

    cfg_base = {"configurable": {"thread_id": "thr-mp", "checkpoint_ns": ""}}
    cp1 = {
        "v": 1,
        "id": "001",
        "ts": "2026-11-26T00:00:01Z",
        "channel_values": {"turn": 1},
        "channel_versions": {"turn": 1},
        "versions_seen": {},
    }
    cp2 = {
        "v": 1,
        "id": "002",
        "ts": "2026-11-26T00:00:02Z",
        "channel_values": {"turn": 2},
        "channel_versions": {"turn": 2},
        "versions_seen": {},
    }

    pod1.put(cfg_base, cp1, {"step": 1}, {"turn": 1})
    pod2.put(cfg_base, cp2, {"step": 2}, {"turn": 2})

    sync_res = pod1.get_tuple(cfg_base)
    assert sync_res is not None
    assert sync_res.checkpoint["id"] == "002"
    assert sync_res.checkpoint["channel_values"]["turn"] == 2

    cp3 = {
        "v": 1,
        "id": "003",
        "ts": "2026-11-26T00:00:03Z",
        "channel_values": {"turn": 3},
        "channel_versions": {"turn": 3},
        "versions_seen": {},
    }
    await pod2.aput(cfg_base, cp3, {"step": 3}, {"turn": 3})
    async_res = await pod1.aget_tuple(cfg_base)
    assert async_res is not None
    assert async_res.checkpoint["id"] == "003"
    assert async_res.checkpoint["channel_values"]["turn"] == 3


def test_redis_saver_and_prompt_cache_use_scan_iter_not_keys() -> None:
    """Cleanup and L2 flush must iterate via SCAN rather than blocking KEYS."""
    store = _SharedFakeRedis()
    store.kv["prompt_cache:k1"] = b"v1"
    store.kv["prompt_cache:k2"] = b"v2"

    cache = PromptCache(enable_redis=False)
    cache._redis_client = store
    cache.clear()
    assert not store.keys_called
    assert "prompt_cache:*" in store.scan_patterns
    assert "prompt_cache:k1" not in store.kv
    assert "prompt_cache:k2" not in store.kv

    saver = RedisSaver(redis_url=None)
    saver._redis_client = store
    idx_key = saver._thread_index_key("thr-empty", "")
    store.sets[idx_key] = set()
    cleaned = saver.cleanup_inactive_threads()
    assert cleaned >= 1
    assert not store.keys_called
    assert idx_key not in store.sets


@pytest.mark.asyncio
async def test_dispatch_step_client_id_defaults_to_unattributed() -> None:
    """Missing metadata.client_id must map to 'unattributed', never owner_id."""
    captured: list[dict] = []

    async def _fake_call_agent_stream(*, metadata: dict | None = None, **_kw):
        captured.append(dict(metadata or {}))
        yield {"event": "completed", "output": "done"}

    fake_client = MagicMock()
    fake_client.call_agent_stream = _fake_call_agent_stream

    plan = Plan(
        goal="Answer",
        steps=[Step(id="s1", description="Chat", agent="chat", input_query="hi", status="pending")],
    )
    state_missing = AgentState(
        messages=[HumanMessage(content="hi")],
        plan=plan,
        metadata={"user_id": "user-42", "thread_id": "t1"},
    )
    with patch("src.agentic.orchestrator.nodes.dispatch.get_a2a_client", return_value=fake_client):
        await dispatch_node(state_missing)

    assert len(captured) == 1
    assert captured[0]["owner_id"] == "user-42"
    assert captured[0]["client_id"] == "unattributed"

    captured.clear()
    plan.steps[0].status = "pending"
    state_explicit = AgentState(
        messages=[HumanMessage(content="hi")],
        plan=plan,
        metadata={"user_id": "user-42", "client_id": "acme-corp", "thread_id": "t1"},
    )
    with patch("src.agentic.orchestrator.nodes.dispatch.get_a2a_client", return_value=fake_client):
        await dispatch_node(state_explicit)

    assert captured[0]["client_id"] == "acme-corp"


def test_k8s_otel_scrape_and_rag_shared_pvc() -> None:
    """OTel Collector NetworkPolicy allows monitoring:8889 and RAG corpus uses shared RWX PVC."""
    k8s = ROOT / "deploy" / "k8s"

    netpols = {
        d["metadata"]["name"]: d
        for d in yaml.safe_load_all((k8s / "network-policy.yaml").read_text(encoding="utf-8"))
        if isinstance(d, dict) and d.get("kind") == "NetworkPolicy"
    }
    otel_np = netpols["allow-otel-collector"]
    assert any(
        any(p.get("port") == 8889 for p in rule.get("ports", []))
        and any(
            peer.get("namespaceSelector", {}).get("matchLabels", {}).get(
                "kubernetes.io/metadata.name"
            )
            == "monitoring"
            for peer in rule.get("from", [])
        )
        for rule in otel_np["spec"]["ingress"]
    ), "allow-otel-collector must allow Prometheus scrape on 8889 from monitoring namespace"

    kustom = yaml.safe_load((k8s / "kustomization.yaml").read_text(encoding="utf-8"))
    assert "rag-storage.yaml" in kustom["resources"]

    pvc = yaml.safe_load((k8s / "rag-storage.yaml").read_text(encoding="utf-8"))
    assert pvc["kind"] == "PersistentVolumeClaim"
    assert pvc["metadata"]["namespace"] == "agentic-platform"
    assert "ReadWriteMany" in pvc["spec"]["accessModes"]

    temporal_dep = next(
        d
        for d in yaml.safe_load_all((k8s / "temporal-worker.yaml").read_text(encoding="utf-8"))
        if isinstance(d, dict) and d.get("kind") == "Deployment"
    )
    t_spec = temporal_dep["spec"]["template"]["spec"]
    t_mounts = {m["mountPath"]: m for m in t_spec["containers"][0]["volumeMounts"]}
    assert "/app/data/raw" in t_mounts and "/app/data/index" in t_mounts
    assert t_mounts["/app/data/raw"].get("readOnly") is not True
    assert any(ic["name"] == "seed-rag-corpus" for ic in t_spec.get("initContainers", []))

    rag_dep = next(
        d
        for d in yaml.safe_load_all((k8s / "deployment.yaml").read_text(encoding="utf-8"))
        if isinstance(d, dict)
        and d.get("kind") == "Deployment"
        and d["metadata"]["name"] == "agentic-rag-agent"
    )
    r_mounts = {
        m["mountPath"]: m for m in rag_dep["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    }
    assert r_mounts["/app/data/raw"].get("readOnly") is True
    assert r_mounts["/app/data/index"].get("readOnly") is True

    # Ensure test-generated artifacts do not linger in tracked raw corpus
    assert not (ROOT / "data" / "raw" / "delta_test_doc.md").exists()
    assert not (ROOT / "data" / "raw" / "security" / "vpn_policy_delta.md").exists()
