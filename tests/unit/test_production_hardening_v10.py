"""Production hardening v10 regression and coverage tests.

Covers offline Alembic SQL generation, pre-baked neural reranker artifacts,
CodingAgent HA & PDBs, backup checksum verification, per-tenant JWT rate-limit
keying, table-driven A2A local fallback loading, and RAG server/migration paths.
"""

from __future__ import annotations

import io
import socket
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import jwt
import pytest
import yaml
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[2]


def _make_request(
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    client_host: str = "198.51.100.10",
) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/chat",
        "headers": headers or [],
        "client": (client_host, 54321),
    }
    return Request(scope)


# ---------------------------------------------------------------------------
# 1. Offline Alembic DDL generation (`alembic --sql`)
# ---------------------------------------------------------------------------
def test_alembic_offline_upgrade_and_downgrade_sql() -> None:
    """Both `alembic upgrade head --sql` and `downgrade head:base --sql` succeed."""
    from alembic import command
    from alembic.config import Config

    up_buf = io.StringIO()
    cfg_up = Config(str(ROOT / "alembic.ini"))
    cfg_up.attributes["stdout"] = up_buf
    cfg_up.output_buffer = up_buf
    command.upgrade(cfg_up, "head", sql=True)
    up_sql = up_buf.getvalue()

    assert "CREATE TABLE long_term_memories" in up_sql
    assert "ADD COLUMN created_at" in up_sql
    assert "CREATE TABLE audit_logs" in up_sql

    down_buf = io.StringIO()
    cfg_down = Config(str(ROOT / "alembic.ini"))
    cfg_down.attributes["stdout"] = down_buf
    cfg_down.output_buffer = down_buf
    command.downgrade(cfg_down, "head:base", sql=True)
    down_sql = down_buf.getvalue()

    assert "DROP TABLE audit_logs" in down_sql
    assert "DROP COLUMN created_at" in down_sql


def test_offline_migration_inspector_fallbacks() -> None:
    """OfflineMigrationInspector and safe_alembic_inspect handle NoInspectionAvailable."""
    from sqlalchemy.exc import NoInspectionAvailable

    from src.agentic.memory.migrations import OfflineMigrationInspector, safe_alembic_inspect

    offline = OfflineMigrationInspector()
    assert offline.has_table("any_table") is False
    assert offline.get_columns("any_table") == []
    assert offline.get_indexes("any_table") == []

    fake_sa = SimpleNamespace(inspect=MagicMock(side_effect=NoInspectionAvailable("mock")))
    res = safe_alembic_inspect(fake_sa, object())
    assert isinstance(res, OfflineMigrationInspector)
    assert safe_alembic_inspect(fake_sa, object(), offline_returns_none=True) is None


# ---------------------------------------------------------------------------
# 2. Pre-baked HuggingFace model cache & K8s CodingAgent HA / Backup integrity
# ---------------------------------------------------------------------------
def test_dockerfile_agent_bakes_cross_encoder_offline() -> None:
    text = (ROOT / "deploy" / "docker" / "Dockerfile.agent").read_text(encoding="utf-8")
    assert "/opt/hf-cache" in text
    assert "CrossEncoder('BAAI/bge-reranker-base', revision=" in text
    assert "COPY --from=builder --chown=65532:65532 /opt/hf-cache /opt/hf-cache" in text
    assert 'ENV HF_HOME="/opt/hf-cache"' in text
    assert 'ENV HF_HUB_OFFLINE="1"' in text


def test_k8s_coding_agent_ha_rag_offline_cache_and_backup_sha256() -> None:
    k8s = ROOT / "deploy" / "k8s"
    deploy_docs = list(yaml.safe_load_all((k8s / "deployment.yaml").read_text(encoding="utf-8")))
    pdb_docs = list(
        yaml.safe_load_all((k8s / "pod-disruption-budgets.yaml").read_text(encoding="utf-8"))
    )
    backup_docs = list(yaml.safe_load_all((k8s / "backup.yaml").read_text(encoding="utf-8")))

    rag_dep = next(
        d for d in deploy_docs if d and d.get("kind") == "Deployment" and d["metadata"]["name"] == "agentic-rag-agent"
    )
    rag_env = {e["name"]: e["value"] for e in rag_dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert rag_env["HF_HOME"] == "/opt/hf-cache"
    assert rag_env["HF_HUB_OFFLINE"] == "1"

    coding_dep = next(
        d for d in deploy_docs if d and d.get("kind") == "Deployment" and d["metadata"]["name"] == "agentic-coding-agent"
    )
    assert coding_dep["spec"]["replicas"] >= 2

    coding_hpa = next(
        d
        for d in deploy_docs
        if d
        and d.get("kind") == "HorizontalPodAutoscaler"
        and d["spec"]["scaleTargetRef"]["name"] == "agentic-coding-agent"
    )
    assert coding_hpa["spec"]["minReplicas"] >= 2

    coding_pdb = next(
        d for d in pdb_docs if d and d.get("kind") == "PodDisruptionBudget" and d["metadata"]["name"] == "agentic-coding-agent-pdb"
    )
    assert coding_pdb["spec"]["minAvailable"] >= 1
    assert coding_pdb["spec"]["selector"]["matchLabels"]["app"] == "agentic-coding-agent"

    backup_job = next(d for d in backup_docs if d and d.get("kind") == "CronJob")
    script = backup_job["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["args"][0]
    assert "gzip -t" in script
    assert "sha256sum" in script
    assert "sha256sum -c" in script
    assert "agentic-*.dump.gz.sha256" in script


# ---------------------------------------------------------------------------
# 3. Per-tenant/subject JWT rate-limit key derivation
# ---------------------------------------------------------------------------
def test_resolve_rate_limit_key_jwt_and_ip_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.agentic.api import middleware
    from src.agentic.core.config import Settings

    secret = "test-jwt-secret-key-at-least-32-bytes"
    cfg = Settings(
        environment="test",
        jwt_secret_key=secret,
        jwt_algorithm="HS256",
        jwt_audience="agentic-clients",
        jwt_issuer="agentic-platform",
    )
    monkeypatch.setattr("src.agentic.core.config.get_settings", lambda: cfg)

    now = int(time.time())
    tok_tenant = jwt.encode(
        {
            "sub": "alice",
            "tenant_id": "acme-corp",
            "aud": "agentic-clients",
            "iss": "agentic-platform",
            "iat": now,
            "exp": now + 300,
        },
        secret,
        algorithm="HS256",
    )
    req_tenant = _make_request(headers=[(b"authorization", f"Bearer {tok_tenant}".encode())])
    assert middleware.resolve_rate_limit_key(req_tenant) == "tenant:acme-corp:alice"

    tok_client = jwt.encode(
        {
            "sub": "bob",
            "client_id": "svc-billing",
            "aud": "agentic-clients",
            "iss": "agentic-platform",
            "iat": now,
            "exp": now + 300,
        },
        secret,
        algorithm="HS256",
    )
    req_client = _make_request(headers=[(b"authorization", f"Bearer {tok_client}".encode())])
    assert middleware.resolve_rate_limit_key(req_client) == "tenant:svc-billing:bob"

    req_bad = _make_request(headers=[(b"authorization", b"Bearer invalid.jwt.token")])
    assert middleware.resolve_rate_limit_key(req_bad) == "ip:198.51.100.10"

    req_anon = _make_request()
    assert middleware.resolve_rate_limit_key(req_anon) == "ip:198.51.100.10"


# ---------------------------------------------------------------------------
# 4. A2A table-driven fallback registry & RAG server checks
# ---------------------------------------------------------------------------
def test_a2a_local_graph_registry_loads_and_caches() -> None:
    from src.agentic.a2a.client import A2AClient

    client = A2AClient()
    with (
        patch("src.agentic.agents.chat.graph.get_chat_graph", return_value="chat_g") as m_chat,
        patch("src.agentic.agents.rag.graph.get_rag_graph", return_value="rag_g") as m_rag,
    ):
        assert client._get_local_graph("chat_agent") == "chat_g"
        assert client._get_local_graph("chat") == "chat_g"
        assert client._get_local_graph("rag") == "rag_g"
        assert client._get_local_graph("unknown") is None
        m_chat.assert_called_once()
        m_rag.assert_called_once()


def test_rag_server_check_tcp_and_readiness_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.agentic.agents.rag import server as rag_server
    from src.agentic.core.config import Settings

    assert rag_server._check_tcp("127.0.0.1", 59997, timeout=0.05) is False

    dummy_sock = MagicMock()
    dummy_sock.__enter__ = MagicMock(return_value=dummy_sock)
    dummy_sock.__exit__ = MagicMock(return_value=False)
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_kw: dummy_sock)
    assert rag_server._check_tcp("127.0.0.1", 6333, timeout=0.1) is True

    cfg_offline = Settings(
        environment="development",
        enable_qdrant=True,
        qdrant_host="qdrant",
        qdrant_port=6333,
        require_embedding_service=True,
        llama_embed_base_url="not-a-url",
    )
    monkeypatch.setattr(rag_server, "get_settings", lambda: cfg_offline)
    monkeypatch.setattr(rag_server, "_check_tcp", lambda _host, _port: False)

    import asyncio

    ok, checks = asyncio.run(rag_server._rag_readiness())
    assert ok is False
    assert checks["vector_store"] == "down"
    assert checks["embedding_service"] == "offline"
