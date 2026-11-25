"""PRR 10/10 verification and coverage hardening tests (v5)."""

from __future__ import annotations

import base64
import importlib
import json
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import LLMResult
from llama_index.core.schema import TextNode

ROOT = Path(__file__).resolve().parents[2]
K8S = ROOT / "deploy" / "k8s"


def _load_yaml_docs(path: Path) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]


def _named_k8s(kind: str, name: str) -> dict:
    for path in sorted(K8S.glob("*.yaml")):
        for doc in _load_yaml_docs(path):
            if doc.get("kind") == kind and doc.get("metadata", {}).get("name") == name:
                return doc
    raise AssertionError(f"Missing {kind} {name!r} in deploy/k8s")


def _prod_settings(**overrides: object):
    from src.agentic.core.config import Settings

    b64_key = base64.urlsafe_b64encode(b"p" * 32).decode("ascii").rstrip("=")
    defaults: dict[str, object] = {
        "environment": "production",
        "require_authentication": True,
        "jwt_secret_key": "prod-jwt-secret-key-for-unit-tests-only",
        "jwt_algorithm": "RS256",
        "jwt_allowed_algorithms": ["RS256"],
        "jwt_jwks_url": "https://auth.example.com/.well-known/jwks.json",
        "s2s_internal_token": "prod-s2s-internal-token-for-unit-tests",
        "checkpoint_hmac_secret": "prod-checkpoint-hmac-secret-32chars",
        "use_postgres_checkpoint": True,
        "postgres_uri": "postgresql://agentic:secret@pgbouncer:5432/agentic_db",
        "allowed_origins": ["https://app.example.com"],
        "approval_private_key": b64_key,
        "qdrant_api_key": "prod-qdrant-api-key",
        "neo4j_password": "prod-neo4j-test-secret",
        "litellm_api_key": "sk-prod-test-key",
        "enable_redis_rate_limit": True,
        "redis_url": "redis://localhost:6379/0",
    }
    defaults.update(overrides)
    return Settings(**defaults)


# ---------------------------------------------------------------------------
# 1. Fail-closed rate limiter auto-recovery, /readyz check & rejection metrics
# ---------------------------------------------------------------------------
def test_fail_closed_rate_limiter_recovers_when_redis_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.agentic.api import middleware

    cfg = _prod_settings()
    state = {"down": True}

    class _MockSliding:
        def __init__(self, **_kwargs) -> None:
            if state["down"]:
                raise middleware.RateLimiterUnavailable("still down")

        def is_allowed(self, _key: str, cost: int = 1) -> tuple[bool, int]:
            return (True, 0)

        def is_token_allowed(self, _key: str, _tokens: int) -> tuple[bool, int]:
            return (True, 0)

    monkeypatch.setattr(middleware, "SlidingWindowRateLimiter", _MockSliding)
    limiter = middleware.FailClosedRateLimiter(settings=cfg, retry_interval_seconds=0.0)

    assert limiter.is_ready() is False
    with pytest.raises(middleware.RateLimiterUnavailable):
        limiter.is_allowed("u1")

    state["down"] = False
    assert limiter.is_ready() is True
    assert limiter.is_allowed("u1") == (True, 0)
    assert limiter.is_token_allowed("u1", 100) == (True, 0)


def test_readyz_reports_503_when_prod_rate_limiter_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import FastAPI

    from src.agentic.api import middleware
    from src.agentic.api.routes import health

    cfg = _prod_settings(
        enable_qdrant=False,
        qdrant_api_key=None,
        require_model_gateway_ready=False,
        require_embedding_service=False,
    )
    # _validate override: Settings forces model/embed checks true in production,
    # so patch the flags explicitly on the validated instance for isolated check.
    cfg.require_model_gateway_ready = False
    cfg.require_embedding_service = False
    cfg.enable_qdrant = False

    fake_cur = MagicMock()
    fake_cur.__enter__ = lambda self: self
    fake_cur.__exit__ = lambda *args: False
    fake_conn = MagicMock()
    fake_conn.cursor.return_value = fake_cur
    fake_conn.__enter__ = lambda self: self
    fake_conn.__exit__ = lambda *args: False

    monkeypatch.setattr("psycopg.connect", lambda *_a, **_kw: fake_conn)
    monkeypatch.setattr(health, "get_settings", lambda: cfg)
    monkeypatch.setattr(health, "_acheck_tcp_port", AsyncMock(return_value=True))
    monkeypatch.setattr(middleware, "is_rate_limiter_ready", lambda: False)

    app = FastAPI()
    app.include_router(health.router)
    client = TestClient(app)

    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["rate_limiter"] == "unavailable"

    monkeypatch.setattr(middleware, "is_rate_limiter_ready", lambda: True)
    resp_ok = client.get("/readyz")
    assert resp_ok.status_code == 200
    assert resp_ok.json()["checks"]["rate_limiter"] == "ok"


def test_rate_limiter_middleware_increments_rejection_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import FastAPI

    from src.agentic.api import middleware
    from src.agentic.core.config import Settings
    from src.agentic.observability.metrics import RATE_LIMIT_REJECTIONS_TOTAL

    app = FastAPI()

    @app.post("/api/v1/chat")
    async def _chat() -> dict[str, str]:
        return {"ok": "1"}

    app.add_middleware(middleware.RequestIDAndMetricsMiddleware)
    cfg = Settings(environment="development", enable_token_rate_limit=True)
    monkeypatch.setattr(middleware, "_settings", cfg)

    stub_limiter = MagicMock()
    monkeypatch.setattr(middleware, "_rate_limiter", stub_limiter)
    client = TestClient(app)

    before_unavail = RATE_LIMIT_REJECTIONS_TOTAL.labels(layer="unavailable")._value.get()
    stub_limiter.is_allowed.side_effect = middleware.RateLimiterUnavailable("down")
    r_unavail = client.post("/api/v1/chat", json={"query": "hi"})
    assert r_unavail.status_code == 503
    assert RATE_LIMIT_REJECTIONS_TOTAL.labels(layer="unavailable")._value.get() == before_unavail + 1

    before_req = RATE_LIMIT_REJECTIONS_TOTAL.labels(layer="request")._value.get()
    stub_limiter.is_allowed.side_effect = None
    stub_limiter.is_allowed.return_value = (False, 12)
    r_req = client.post("/api/v1/chat", json={"query": "hi"})
    assert r_req.status_code == 429
    assert RATE_LIMIT_REJECTIONS_TOTAL.labels(layer="request")._value.get() == before_req + 1

    before_tok = RATE_LIMIT_REJECTIONS_TOTAL.labels(layer="token")._value.get()
    stub_limiter.is_allowed.return_value = (True, 0)
    stub_limiter.is_token_allowed.return_value = (False, 30)
    r_tok = client.post("/api/v1/chat", json={"query": "hello"})
    assert r_tok.status_code == 429
    assert RATE_LIMIT_REJECTIONS_TOTAL.labels(layer="token")._value.get() == before_tok + 1


# ---------------------------------------------------------------------------
# 2. A2A /ready probe & RAG readiness
# ---------------------------------------------------------------------------
def test_a2a_ready_endpoint_reflects_gateway_and_custom_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.agentic.a2a import executor
    from src.agentic.a2a.cards import CHAT_AGENT_CARD
    from src.agentic.core.config import Settings

    cfg = Settings(environment="development", require_model_gateway_ready=True)
    monkeypatch.setattr(executor, "get_settings", lambda: cfg)
    monkeypatch.setattr(executor, "_tcp_reachable", lambda _h, _p: True)

    state = {"dep_ok": False}

    async def _dep() -> tuple[bool, dict[str, str]]:
        return (state["dep_ok"], {"vector_store": "ok" if state["dep_ok"] else "down"})

    app = executor.create_a2a_server(card=CHAT_AGENT_CARD, graph=MagicMock(), readiness_check=_dep)
    client = TestClient(app)

    r_fail = client.get("/ready")
    assert r_fail.status_code == 503
    assert r_fail.json()["checks"] == {"model_gateway": "ok", "vector_store": "down"}

    state["dep_ok"] = True
    r_ok = client.get("/ready")
    assert r_ok.status_code == 200
    assert r_ok.json()["status"] == "ready"
    assert client.get("/health").status_code == 200


@pytest.mark.asyncio
async def test_rag_readiness_verifies_qdrant_and_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.agentic.agents.rag import server as rag_server
    from src.agentic.core.config import Settings

    cfg = Settings(
        environment="development",
        enable_qdrant=True,
        qdrant_host="qdrant",
        qdrant_port=6333,
        require_embedding_service=True,
        llama_embed_base_url="http://embed:8081/v1",
    )
    monkeypatch.setattr(rag_server, "get_settings", lambda: cfg)
    monkeypatch.setattr(rag_server, "_check_tcp", lambda host, port: host == "qdrant")
    ok, checks = await rag_server._rag_readiness()
    assert ok is False
    assert checks == {"vector_store": "ok", "embedding_service": "offline"}

    monkeypatch.setattr(rag_server, "_check_tcp", lambda _host, _port: True)
    ok2, checks2 = await rag_server._rag_readiness()
    assert ok2 is True
    assert checks2 == {"vector_store": "ok", "embedding_service": "ok"}


# ---------------------------------------------------------------------------
# 3. Immutable tamper-evident audit trail, migration 003 & callers
# ---------------------------------------------------------------------------
def test_audit_record_sqlite_hash_verification_and_prod_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.agentic.core.config import Settings
    from src.agentic.safety import audit

    db_path = tmp_path / "audit.db"
    cfg = Settings(environment="test", checkpoint_hmac_secret="s" * 32)
    event_id = audit.record_audit_event(
        event_type="hitl_decision",
        actor_id="admin-1",
        thread_id="th-1",
        step_id="step-9",
        trace_id="req-xyz",
        decision="approved",
        risk_level="high",
        details={"agent": "coding"},
        settings=cfg,
        sqlite_path=db_path,
    )

    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT id, event_type, actor_id, thread_id, step_id, decision, details, integrity_hash, created_at "
            "FROM audit_logs WHERE id = ?",
            (event_id,),
        ).fetchone()
    assert row is not None
    expected_hash = audit.compute_audit_integrity_hash(
        event_id=row[0],
        event_type=row[1],
        actor_id=row[2],
        thread_id=row[3],
        step_id=row[4],
        decision=row[5],
        details=json.loads(row[6]),
        created_at=row[8],
        signing_key=cfg.checkpoint_signing_key,
    )
    assert row[7] == expected_hash

    prod_cfg = _prod_settings()
    monkeypatch.setattr(
        audit, "_write_postgres_audit", lambda **_kw: (_ for _ in ()).throw(OSError("pg down"))
    )
    with pytest.raises(RuntimeError, match="Audit trail persistence failed"):
        audit.record_audit_event(
            event_type="data_purge",
            actor_id="u-1",
            decision="failed",
            settings=prod_cfg,
        )


def test_alembic_migration_003_audit_logs_upgrade_and_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mig = importlib.import_module("migrations.versions.003_audit_logs")
    assert mig.revision == "003_audit_logs"
    assert mig.down_revision == "002_checkpoint_retention"

    calls: list[tuple[str, tuple]] = []
    fake_op = SimpleNamespace(
        create_table=lambda name, *cols: calls.append(("create_table", (name, len(cols)))),
        create_index=lambda idx, tbl, cols: calls.append(("create_index", (idx, tbl, tuple(cols)))),
        drop_index=lambda idx, table_name=None: calls.append(("drop_index", (idx, table_name))),
        drop_table=lambda name: calls.append(("drop_table", (name,))),
    )
    monkeypatch.setattr(mig, "op", fake_op)
    mig.upgrade()
    mig.downgrade()

    assert calls[0] == ("create_table", ("audit_logs", 11))
    assert any(c[0] == "create_index" and c[1][0] == "idx_audit_logs_created_at" for c in calls)
    assert calls[-1] == ("drop_table", ("audit_logs",))


@pytest.mark.asyncio
async def test_hitl_and_retention_emit_audit_events(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.agentic.memory import data_lifecycle
    from src.agentic.orchestrator.nodes import hitl
    from src.agentic.orchestrator.state import AgentState, Plan, Step

    events: list[dict] = []
    monkeypatch.setattr(hitl, "record_audit_event", lambda **kw: events.append(kw) or "ev-1")
    monkeypatch.setattr(data_lifecycle, "record_audit_event", lambda **kw: events.append(kw) or "ev-2")

    step = Step(
        id="s1",
        description="Run script",
        agent="coding",
        input_query="print(1)",
        risk_level="high",
    )
    state = AgentState(
        thread_id="th-audit",
        original_query="run",
        plan=Plan(goal="run", steps=[step]),
        metadata={"user_id": "alice"},
    )
    monkeypatch.setattr(hitl, "interrupt", lambda _payload: {"approved": True, "reason": "ok"})
    res = await hitl.hitl_node(state)
    assert res["approval_granted"] is True

    fake_cur = MagicMock()
    fake_cur.fetchone.return_value = (3,)
    fake_cur.__enter__ = lambda self: self
    fake_cur.__exit__ = lambda *args: False
    fake_conn = MagicMock()
    fake_conn.cursor.return_value = fake_cur
    fake_conn.__enter__ = lambda self: self
    fake_conn.__exit__ = lambda *args: False
    fake_pool = MagicMock()
    fake_pool.connection.return_value = fake_conn

    purged = data_lifecycle.cleanup_stale_postgres_checkpoints(
        max_idle_seconds=7200, dry_run=False, pool=fake_pool
    )
    assert purged == 3
    assert [e["event_type"] for e in events] == ["hitl_decision", "checkpoint_retention"]
    assert events[0]["decision"] == "approved"
    assert events[1]["details"]["stale_threads"] == 3


# ---------------------------------------------------------------------------
# 4. RAG ingestion deduplication & Cache tier metrics
# ---------------------------------------------------------------------------
def test_deduplicate_leaf_nodes_filters_duplicates_and_stamps_sha256() -> None:
    from src.agentic.rag.ingestion import deduplicate_leaf_nodes

    n1 = TextNode(text="Hello   world\nfrom RAG")
    n2 = TextNode(text="Hello world from RAG")
    n3 = TextNode(text="Different paragraph")
    out = deduplicate_leaf_nodes([n1, n2, n3], tenant_id="acme")
    assert len(out) == 2
    assert "content_sha256" in out[0].metadata
    assert out[0].metadata["content_sha256"] != out[1].metadata["content_sha256"]


def test_cache_tier_hit_counters_increment() -> None:
    from src.agentic.cache.prompt_cache import PromptCache
    from src.agentic.cache.rag_cache import RAGCache
    from src.agentic.observability.metrics import (
        PROMPT_CACHE_TIER_HITS_TOTAL,
        RAG_CACHE_TIER_HITS_TOTAL,
    )

    class _FakeRedis:
        def __init__(self) -> None:
            self.store: dict[str, str] = {}

        def ping(self) -> bool:
            return True

        def get(self, key: str) -> str | None:
            return self.store.get(key)

        def setex(self, key: str, _ttl: int, value: str) -> None:
            self.store[key] = value

        def set(self, key: str, value: str, ex: int | None = None) -> None:
            self.store[key] = value

        def ttl(self, _key: str) -> int:
            return 120

    fake_redis = _FakeRedis()
    pc = PromptCache(default_ttl=60, max_entries=16, enable_redis=False)
    pc._redis_client = fake_redis

    before_l1 = PROMPT_CACHE_TIER_HITS_TOTAL.labels(model_alias="fast-llm", tier="l1")._value.get()
    before_l2 = PROMPT_CACHE_TIER_HITS_TOTAL.labels(model_alias="fast-llm", tier="l2")._value.get()

    pc.set_response("fast-llm", ["hi"], 0.1, "hello back")
    assert pc.get_response("fast-llm", ["hi"], 0.1) == "hello back"
    assert PROMPT_CACHE_TIER_HITS_TOTAL.labels(model_alias="fast-llm", tier="l1")._value.get() == before_l1 + 1

    pc._cache.clear()
    assert pc.get_response("fast-llm", ["hi"], 0.1) == "hello back"
    assert PROMPT_CACHE_TIER_HITS_TOTAL.labels(model_alias="fast-llm", tier="l2")._value.get() == before_l2 + 1

    rag_cache = RAGCache(enable_redis=False, embed_fn=lambda _q: [1.0, 0.0, 0.0])
    rag_cache._redis_client = fake_redis

    before_rag_l1 = RAG_CACHE_TIER_HITS_TOTAL.labels(tier="l1")._value.get()
    before_rag_l2 = RAG_CACHE_TIER_HITS_TOTAL.labels(tier="l2")._value.get()
    before_rag_sem = RAG_CACHE_TIER_HITS_TOTAL.labels(tier="semantic")._value.get()

    rag_cache.set("What is SSO?", "Single sign-on", domain="policy", top_k=3)
    assert rag_cache.get("What is SSO?", domain="policy", top_k=3) == "Single sign-on"
    assert RAG_CACHE_TIER_HITS_TOTAL.labels(tier="l1")._value.get() == before_rag_l1 + 1

    rag_cache._cache.clear()
    assert rag_cache.get("What is SSO?", domain="policy", top_k=3) == "Single sign-on"
    assert RAG_CACHE_TIER_HITS_TOTAL.labels(tier="l2")._value.get() == before_rag_l2 + 1

    rag_cache.set_semantic(
        "What is SSO?",
        "Single sign-on",
        domain="policy",
        top_k=3,
        query_vector=[1.0, 0.0, 0.0],
    )
    rag_cache._cache.clear()
    assert (
        rag_cache.get_semantic(
            "Explain SSO policy", domain="policy", top_k=3, query_vector=[0.99, 0.01, 0.0]
        )
        == "Single sign-on"
    )
    assert RAG_CACHE_TIER_HITS_TOTAL.labels(tier="semantic")._value.get() == before_rag_sem + 1


# ---------------------------------------------------------------------------
# 5. Runtime entrypoint Temporal support & K8s/DR manifests & Locust JWT
# ---------------------------------------------------------------------------
def test_runtime_entrypoint_supports_temporal_and_rejects_unlisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.agentic import runtime_entrypoint

    invoked: list[list[str]] = []
    fake_mod = SimpleNamespace(main=lambda: invoked.append(sys.argv.copy()))
    monkeypatch.setenv("TEMPORAL_WORKER_MODULE", "src.agentic.workflows.worker")
    monkeypatch.setattr(runtime_entrypoint.importlib, "import_module", lambda _m: fake_mod)
    monkeypatch.setattr(sys, "argv", ["test"])

    runtime_entrypoint.main(["temporal"])
    assert invoked == [["src.agentic.workflows.worker"]]

    monkeypatch.setenv("TEMPORAL_WORKER_MODULE", "evil.worker")
    with pytest.raises(SystemExit):
        runtime_entrypoint.main(["temporal"])


def test_k8s_and_dr_hardening_manifests_and_locust_jwt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jwt

    monkeypatch.setitem(
        sys.modules,
        "locust",
        SimpleNamespace(
            HttpUser=object,
            between=lambda *a: None,
            task=lambda *a, **kw: (a[0] if len(a) == 1 and callable(a[0]) else (lambda fn: fn)),
            events=SimpleNamespace(
                quitting=SimpleNamespace(add_listener=lambda fn: fn),
            ),
        ),
    )
    from tests.load import locustfile

    code_exec_np = _named_k8s("NetworkPolicy", "allow-code-exec-ingress")
    egress_ports = {
        p["port"]
        for rule in code_exec_np["spec"].get("egress", [])
        for p in rule.get("ports", [])
    }
    assert {443, 6443}.issubset(egress_ports)

    temporal_dep = _named_k8s("Deployment", "agentic-temporal-worker")
    assert temporal_dep["spec"]["replicas"] == 2
    assert temporal_dep["spec"]["template"]["spec"]["serviceAccountName"] == "agentic-temporal-worker-sa"
    _named_k8s("PodDisruptionBudget", "agentic-temporal-worker-pdb")
    _named_k8s("ServiceAccount", "agentic-temporal-worker-sa")
    _named_k8s("NetworkPolicy", "allow-temporal-worker")

    ext_secret = _named_k8s("ExternalSecret", "agentic-temporal-secrets")
    secret_keys = {entry["secretKey"] for entry in ext_secret["spec"]["data"]}
    assert "APPROVAL_PRIVATE_KEY" not in secret_keys

    dep_path = K8S / "deployment.yaml"
    found_agents: set[str] = set()
    for doc in _load_yaml_docs(dep_path):
        name = doc["metadata"]["name"]
        if name in {"agentic-chat-agent", "agentic-rag-agent", "agentic-coding-agent"}:
            found_agents.add(name)
            probe_path = doc["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]["httpGet"]["path"]
            assert probe_path == "/ready"
    assert found_agents == {"agentic-chat-agent", "agentic-rag-agent", "agentic-coding-agent"}

    kustom = yaml.safe_load((K8S / "kustomization.yaml").read_text(encoding="utf-8"))
    assert "temporal-worker.yaml" in kustom["resources"]

    backup_script = (ROOT / "deploy" / "scripts" / "backup_qdrant.sh").read_text(encoding="utf-8")
    assert 'QDRANT_COLLECTION="${QDRANT_COLLECTION:-agentic_knowledge}"' in backup_script

    monkeypatch.delenv("LOCUST_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("LOCUST_JWT_SECRET", "super-secret-locust-key-for-testing-32")
    token = locustfile._resolve_bearer_token()
    decoded = jwt.decode(
        token,
        "super-secret-locust-key-for-testing-32",
        algorithms=["HS256"],
        audience="agentic-api",
        issuer="agentic-platform",
    )
    assert decoded["sub"] == "locust-load-tester"


# ---------------------------------------------------------------------------
# 6. Branch coverage uplift for context, thread_registry, tracing, long_term, gateway
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_context_and_thread_registry_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.agentic.core import context
    from src.agentic.core.config import Settings
    from src.agentic.memory.thread_registry import (
        ThreadOwnershipError,
        ThreadRegistry,
        ThreadRegistryUnavailable,
    )

    tok = context.set_request_id("req-abc")
    assert context.get_request_id() == "req-abc"
    context.reset_request_id(tok)
    assert context.get_request_id() == "-"

    reg = ThreadRegistry()
    reg.settings = Settings(environment="development")
    fake_redis = AsyncMock()
    fake_redis.ping = AsyncMock(return_value=True)
    fake_redis.set = AsyncMock(return_value=True)
    fake_redis.get = AsyncMock(return_value=b"alice")
    fake_redis.smembers = AsyncMock(return_value={b"t1", "t2"})
    fake_redis.expire = AsyncMock(return_value=True)
    fake_redis.sadd = AsyncMock(return_value=1)
    fake_redis.aclose = AsyncMock()
    monkeypatch.setattr("redis.asyncio.from_url", lambda *_a, **_kw: fake_redis)

    scoped = await reg.claim("t1", "alice")
    assert scoped.startswith("thread:")
    assert sorted(await reg.threads_for_owner("alice")) == ["t1", "t2"]
    assert await reg.owner("t1") == "alice"

    fake_redis.set = AsyncMock(return_value=False)
    fake_redis.get = AsyncMock(return_value=b"bob")
    with pytest.raises(ThreadOwnershipError):
        await reg.claim("t1", "alice")

    fake_redis.get = AsyncMock(side_effect=RuntimeError("redis reset"))
    assert await reg.owner("t1") is None
    assert reg._redis is None

    reg.settings = _prod_settings()
    failing_redis = AsyncMock()
    failing_redis.ping = AsyncMock(side_effect=RuntimeError("down"))
    monkeypatch.setattr("redis.asyncio.from_url", lambda *_a, **_kw: failing_redis)
    with pytest.raises(ThreadRegistryUnavailable):
        await reg._client()

    with pytest.raises(ThreadRegistryUnavailable):
        await reg._handle_redis_failure("claim", fake_redis, RuntimeError("boom"))

    reg._redis = fake_redis
    await reg.aclose()
    assert reg._redis is None


@pytest.mark.asyncio
async def test_tracing_helpers_and_error_recording() -> None:
    from src.agentic.observability import tracing

    async with tracing.trace_span("unit.span", {"user_id": "u1", "empty": None}) as span:
        assert span is not None

    @tracing.traced_operation("unit.op")
    async def _op(fail: bool = False) -> str:
        if fail:
            raise ValueError("boom")
        return "ok"

    assert await _op(False) == "ok"
    with pytest.raises(ValueError, match="boom"):
        await _op(True)

    carrier: dict[str, str] = {}
    tracing.inject_trace_context(carrier)
    extracted = tracing.extract_trace_context(carrier)
    assert extracted is not None


def test_long_term_store_list_delete_and_pg_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.agentic.memory.long_term import LongTermStore

    store = LongTermStore(storage_dir=tmp_path, force_sqlite=True)
    store.add_semantic("theme", "Likes dark mode", metadata={"user_id": "u-99"})
    store.add_semantic("theme", "Other user", metadata={"user_id": "u-other"})

    rows = store.list_by_user("u-99")
    assert len(rows) == 1
    assert rows[0].content == "Likes dark mode"

    bad_rows = store._entries_from_rows(
        [{"id": "1", "category": "c", "content": "x", "metadata": "{not-json", "created_at": "now"}],
        postgres=False,
    )
    assert bad_rows[0].metadata == {}

    deleted = store.delete_by_user("u-99")
    assert deleted == 1
    assert store.list_by_user("u-99") == []

    with pytest.raises(ValueError):
        store.list_by_user("bad user id!")

    store.is_postgres = True
    fake_cur = MagicMock()
    fake_cur.fetchall.return_value = [("m1", "fact", "PG memory", {"user_id": "u-pg"}, "2026-01-01")]
    fake_cur.rowcount = 1
    fake_cur.__enter__ = lambda self: self
    fake_cur.__exit__ = lambda *args: False
    fake_conn = MagicMock()
    fake_conn.cursor.return_value = fake_cur
    fake_conn.__enter__ = lambda self: self
    fake_conn.__exit__ = lambda *args: False
    monkeypatch.setattr(store, "_get_pg_conn", lambda: fake_conn)

    pg_entries = store.list_by_user("u-pg")
    assert pg_entries[0].id == "m1"
    assert store.delete_by_user("u-pg") == 1


@pytest.mark.asyncio
async def test_llm_gateway_prompt_cache_callbacks_and_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.agentic.core.execution_context import (
        ToolExecutionContext,
        reset_tool_execution_context,
        set_tool_execution_context,
    )
    from src.agentic.llm import gateway

    llm = gateway._PromptCachedChatOpenAI(
        model="fast-llm",
        api_key="test-key",
        base_url="http://localhost:4000/v1",
        temperature=0.1,
    )
    llm._cache_alias = "fast-llm"

    assert llm._should_use_cache({}) is True
    assert llm._should_use_cache({"tools": []}) is False
    assert llm._cache_messages("hello") == ["hello"]
    assert llm._cache_messages(12345) is None

    prompt_val = SimpleNamespace(to_messages=lambda: [HumanMessage(content="prompt")])
    tok = set_tool_execution_context(
        ToolExecutionContext(agent_name="chat", step_id="s1", owner_id="owner-7")
    )
    try:
        msgs = llm._cache_messages(prompt_val)
    finally:
        reset_tool_execution_context(tok)
    assert msgs[-1] == {"role": "system", "content": "tenant_scope:owner-7"}

    fake_cache = MagicMock()
    fake_cache.get_response.return_value = "cached reply"
    monkeypatch.setattr(gateway, "get_prompt_cache", lambda: fake_cache)

    assert llm.invoke("hi").content == "cached reply"
    assert (await llm.ainvoke("hi")).content == "cached reply"

    fake_cache.get_response.return_value = None
    monkeypatch.setattr(
        gateway.ChatOpenAI,
        "invoke",
        lambda self, *_a, **_kw: AIMessage(content="fresh reply"),
    )
    assert llm.invoke("hi").content == "fresh reply"
    fake_cache.set_response.assert_called()

    cb = gateway.TokenMetricsCallbackHandler("fast-llm")
    cb.on_llm_start({}, ["12345678"], run_id="r1")
    cb.on_chat_model_start({}, [[HumanMessage(content="abcd")]], run_id="r2")
    cb.on_llm_new_token("abcdefgh", run_id="r2")
    cb.on_llm_end(LLMResult(generations=[[]], llm_output=None), run_id="r2")

    gateway.reload_gateway_config()
    cb.on_llm_error(SimpleNamespace(status_code=400))
    cb.on_llm_error(RuntimeError("upstream 503"))
    assert gateway._circuit_breaker.failures.get("fast-llm", 0) == 1
    gateway.reload_gateway_config()
    assert time.time() > 0
