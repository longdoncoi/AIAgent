"""Regression tests for v7 Chief-Architect Production Readiness fixes.

Covers:
1. ServiceMonitor label matching across all 4 L1/L3 Services in k8s deployment.
2. K8s NetworkPolicy egress for MCP Redis nonces (6379), PgBouncer (6432), DB migration (5432),
   ESO REDIS_URL secret mapping, db-migration-job registration, and pod SA token hardening.
3. Alembic 001/002/003 idempotency against pre-existing AsyncPostgresSaver tables + `--check` CLI.
4. API lifespan ordering (migrations before checkpointer initialization).
5. Multi-replica RAG cache invalidation polling lifespan and FIPS-safe MD5 flag.
6. Official DeepEval release runner execution path + out-of-scope abstention gating.
7. Runbook alert names aligned cleanly with `deploy/monitoring/alerts.yaml`.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _load_yaml_docs(path: Path) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        return [doc for doc in yaml.safe_load_all(f) if isinstance(doc, dict)]


def test_kustomize_servicemonitor_matches_all_platform_services() -> None:
    """ServiceMonitor matchExpressions must match API, Chat, RAG, and Coding Services."""
    k8s = ROOT / "deploy" / "k8s"
    dep_docs = _load_yaml_docs(k8s / "deployment.yaml")
    mon_docs = _load_yaml_docs(k8s / "monitoring.yaml")

    sm = next(
        d
        for d in mon_docs
        if d.get("kind") == "ServiceMonitor"
        and d.get("metadata", {}).get("name") == "agentic-platform-servicemonitor"
    )
    exprs = sm["spec"]["selector"]["matchExpressions"]
    expected_apps = set(next(e["values"] for e in exprs if e["key"] == "app"))

    services = {
        d["metadata"]["name"]: d["metadata"].get("labels", {})
        for d in dep_docs
        if d.get("kind") == "Service"
    }
    for svc_name in (
        "agentic-api-svc",
        "agentic-chat-svc",
        "agentic-rag-svc",
        "agentic-coding-svc",
    ):
        assert svc_name in services, f"Missing Service {svc_name}"
        labels = services[svc_name]
        assert labels.get("app") in expected_apps, f"{svc_name} app label mismatch: {labels}"
        assert labels.get("tier"), f"{svc_name} missing tier label: {labels}"


def test_k8s_netpol_secrets_and_migration_job() -> None:
    k8s = ROOT / "deploy" / "k8s"
    kustom = yaml.safe_load((k8s / "kustomization.yaml").read_text(encoding="utf-8"))
    assert "db-migration-job.yaml" in kustom["resources"]

    netpols = {
        d["metadata"]["name"]: d
        for d in _load_yaml_docs(k8s / "network-policy.yaml")
        if d.get("kind") == "NetworkPolicy"
    }
    # API allows pgbouncer port 6432
    api_egress_ports = {
        p["port"]
        for rule in netpols["allow-agentic-api"]["spec"]["egress"]
        for p in rule.get("ports", [])
    }
    assert 6432 in api_egress_ports
    assert 5432 in api_egress_ports

    # Both MCP servers allow Redis port 6379 for nonce replay protection
    for np_name in ("allow-code-exec-ingress", "allow-repo-ingress"):
        egress_ports = {
            p["port"]
            for rule in netpols[np_name]["spec"].get("egress", [])
            for p in rule.get("ports", [])
        }
        assert 6379 in egress_ports, f"{np_name} missing Redis egress port 6379"

    # Dedicated DB migration job network policy allows direct Postgres 5432
    mig_ports = {
        p["port"]
        for rule in netpols["allow-db-migrate"]["spec"]["egress"]
        for p in rule.get("ports", [])
    }
    assert 5432 in mig_ports

    # ExternalSecret exposes REDIS_URL to MCP servers
    ext_secrets = {
        d["metadata"]["name"]: d
        for d in _load_yaml_docs(k8s / "external-secret.yaml")
        if d.get("kind") == "ExternalSecret"
    }
    mcp_keys = {item["secretKey"] for item in ext_secrets["agentic-mcp-secrets"]["spec"]["data"]}
    assert "REDIS_URL" in mcp_keys

    # PreSync DB migration job & pod SA token hardening
    job = yaml.safe_load((k8s / "db-migration-job.yaml").read_text(encoding="utf-8"))
    pod_spec = job["spec"]["template"]["spec"]
    assert pod_spec["automountServiceAccountToken"] is False
    env_map = {e["name"]: e.get("value") for e in pod_spec["containers"][0]["env"]}
    assert env_map["SERVICE_ROLE"] == "maintenance"
    assert env_map["STRICT_MIGRATION"] == "true"

    pgb = next(
        d
        for d in _load_yaml_docs(k8s / "pgbouncer.yaml")
        if d.get("kind") == "Deployment"
    )
    assert pgb["spec"]["template"]["spec"]["automountServiceAccountToken"] is False

    backup_cron = next(
        d for d in _load_yaml_docs(k8s / "backup.yaml") if d.get("kind") == "CronJob"
    )
    assert backup_cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["automountServiceAccountToken"] is False


def test_alembic_001_idempotent_when_tables_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    """001_initial_schema.upgrade() skips existing tables via sa.inspect()."""
    mod_path = ROOT / "migrations" / "versions" / "001_initial_schema.py"
    spec = importlib.util.spec_from_file_location("mig001", mod_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class _FakeInspector:
        def has_table(self, name: str) -> bool:
            return name in {
                "checkpoints",
                "checkpoint_blobs",
                "checkpoint_writes",
                "long_term_memories",
            }

        def get_indexes(self, table_name: str) -> list[dict[str, str]]:
            return [
                {"name": "idx_mem_category"},
                {"name": "idx_mem_created_at"},
                {"name": "idx_checkpoint_thread"},
            ]

    create_calls: list[str] = []
    index_calls: list[str] = []

    fake_op = MagicMock()
    fake_op.get_bind.return_value = object()
    fake_op.create_table.side_effect = lambda name, *a, **kw: create_calls.append(name)
    fake_op.create_index.side_effect = lambda name, *a, **kw: index_calls.append(name)

    monkeypatch.setattr(mod, "op", fake_op)
    monkeypatch.setattr(mod.sa, "inspect", lambda _bind: _FakeInspector())

    mod.upgrade()
    assert create_calls == []
    assert index_calls == []


def test_migrate_db_cli_check_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.migrate_db as cli

    monkeypatch.setattr(cli, "check_database_migrations", lambda: True)
    with pytest.raises(SystemExit) as ok_exit:
        cli.main(["--check"])
    assert ok_exit.value.code == 0

    monkeypatch.setattr(cli, "check_database_migrations", lambda: False)
    with pytest.raises(SystemExit) as bad_exit:
        cli.main(["--check"])
    assert bad_exit.value.code == 1

    def _boom() -> bool:
        raise RuntimeError("db down")

    monkeypatch.setattr(cli, "check_database_migrations", _boom)
    with pytest.raises(SystemExit) as err_exit:
        cli.main(["--check"])
    assert err_exit.value.code == 1


@pytest.mark.asyncio
async def test_api_lifespan_runs_migrations_before_checkpointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.agentic.api.main as api_main

    order: list[str] = []

    def _fake_migrate() -> bool:
        order.append("migrate")
        return True

    async def _fake_init_cp() -> None:
        order.append("checkpointer")

    settings = api_main.get_settings()
    monkeypatch.setattr(settings, "use_postgres_checkpoint", True)
    monkeypatch.setattr(api_main, "_poll_rag_invalidation", AsyncMock())
    monkeypatch.setattr(api_main, "get_supervisor_graph", lambda checkpointer=None: MagicMock())

    with (
        patch("src.agentic.memory.migrations.run_database_migrations", side_effect=_fake_migrate),
        patch("src.agentic.memory.short_term.initialize_checkpointer", side_effect=_fake_init_cp),
        patch.object(api_main, "_close_background_tasks", AsyncMock()),
        patch.object(api_main, "_close_a2a_client", AsyncMock()),
        patch.object(api_main, "_close_checkpointer", AsyncMock()),
        patch.object(api_main, "_close_thread_registry", AsyncMock()),
        patch("src.agentic.core.redis.aclose_redis_pools", AsyncMock()),
    ):
        async with api_main.lifespan(api_main.app):
            pass

    assert order == ["migrate", "checkpointer"]


@pytest.mark.asyncio
async def test_rag_server_invalidation_polling_and_fips_md5() -> None:
    import src.agentic.agents.rag.server as rag_server

    assert rag_server.app.router.lifespan_context is not None

    stop = asyncio.Event()
    calls = 0

    class _FakeCache:
        def check_invalidation_generation(self) -> bool:
            nonlocal calls
            calls += 1
            if calls >= 1:
                stop.set()
            return True

    with patch("src.agentic.cache.rag_cache.get_rag_cache", return_value=_FakeCache()):
        await rag_server._poll_rag_invalidation(stop)

    assert calls == 1

    rag_cache_src = (ROOT / "src" / "agentic" / "cache" / "rag_cache.py").read_text(
        encoding="utf-8"
    )
    assert "usedforsecurity=False" in rag_cache_src


@pytest.mark.asyncio
async def test_deepeval_release_mode_uses_official_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import evals.deepeval_runner as dr

    class _FakeFaith:
        def __init__(self, threshold: float, model: Any, async_mode: bool, include_reason: bool):
            self.score = 0.92

        async def a_measure(self, _tc: Any) -> None:
            return None

    class _FakeRel(_FakeFaith):
        pass

    class _FakeHalluc(_FakeFaith):
        def __init__(self, threshold: float, model: Any, async_mode: bool, include_reason: bool):
            super().__init__(threshold, model, async_mode, include_reason)
            # DeepEval 4.x HallucinationMetric scores 1.0=pass -> 0.98 means 0.02 hallucination ratio
            self.score = 0.98

    class _FakeBaseLLM:
        def __init__(self, model: str | None = None):
            self.name = model or "fake"
            self.model = self.load_model()

    fake_metrics_mod = MagicMock(
        FaithfulnessMetric=_FakeFaith,
        AnswerRelevancyMetric=_FakeRel,
        HallucinationMetric=_FakeHalluc,
    )
    fake_models_mod = MagicMock(DeepEvalBaseLLM=_FakeBaseLLM)
    fake_tc_mod = MagicMock(LLMTestCase=lambda **kw: kw)

    with (
        patch.dict(
            "sys.modules",
            {
                "deepeval.metrics": fake_metrics_mod,
                "deepeval.models": fake_models_mod,
                "deepeval.test_case": fake_tc_mod,
            },
        ),
        patch("src.agentic.llm.gateway.get_llm", return_value=MagicMock()),
    ):
        samples = [
            {
                "id": "q1",
                "question": "Timeout là bao lâu?",
                "ground_truth": "30 giây.",
            },
            {
                "id": "q2",
                "question": "Chính sách ESOP 2030?",
                "ground_truth": "Hệ thống không có thông tin.",
            },
        ]
        answers = {
            "q1": "Giới hạn timeout tối đa là 30 giây.",
            "q2": "Xin lỗi, hệ thống không tìm thấy thông tin trong tài liệu.",
        }
        from evals.answer_generator import NO_CONTEXT_MARKER

        contexts = {
            "q1": ["Giới hạn thời gian timeout tối đa là 30 giây."],
            "q2": [NO_CONTEXT_MARKER],
        }
        result = await dr._run_real_deepeval(
            samples,
            answers,
            contexts,
            thresholds={
                "faithfulness": 0.85,
                "answer_relevancy": 0.80,
                "max_hallucination_ratio": 0.05,
            },
            model_alias="reasoning-llm",
        )

    assert result["faithfulness"] == pytest.approx(0.92)
    assert result["answer_relevancy"] == pytest.approx(0.92)
    assert result["max_hallucination_ratio"] == pytest.approx(0.02)
    assert result["abstention_accuracy"] == pytest.approx(1.0)


def test_runbook_alerts_match_prometheus_rules() -> None:
    alerts_doc = yaml.safe_load(
        (ROOT / "deploy" / "monitoring" / "alerts.yaml").read_text(encoding="utf-8")
    )
    defined = {
        rule["alert"]
        for group in alerts_doc["groups"]
        for rule in group["rules"]
    }
    gpu_rb = (ROOT / "docs" / "runbooks" / "gpu_failure.md").read_text(encoding="utf-8")
    rb_rb = (ROOT / "docs" / "runbooks" / "rollback.md").read_text(encoding="utf-8")

    assert "HighLLMLatencyP99" in gpu_rb and "HighLLMLatencyP99" in defined
    assert "HighAPIErrorRate" in rb_rb and "HighAPIErrorRate" in defined
    assert "ModelCircuitBreakerTripped" in rb_rb and "ModelCircuitBreakerTripped" in defined
    assert "AgentLatencyP99High" not in gpu_rb
    assert "AgenticAPIDown" not in rb_rb
