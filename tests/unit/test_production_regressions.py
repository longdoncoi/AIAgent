"""Regression tests for security and production-readiness findings."""

from pathlib import Path

import pytest

from src.agentic.a2a.cards import CODING_AGENT_CARD
from src.agentic.a2a.executor import A2AAgentExecutor, A2ATaskPayload, A2ATaskRequest
from src.agentic.api.routes.chat import _new_run_input
from src.agentic.orchestrator.nodes.planner import _infer_approval_tools
from src.agentic.rag.retriever import HybridRetriever
from src.agentic.safety.capabilities import issue_tool_capability, verify_tool_capability
from src.agentic.tools.execution_context import (
    ToolExecutionContext,
    reset_tool_execution_context,
    set_tool_execution_context,
)
from src.agentic.tools.servers.security import authorize_tool_execution


class _UnusedGraph:
    pass


def test_new_turn_resets_all_run_scoped_approval_state() -> None:
    first = _new_run_input(
        query="first", thread_id="thread-1", owner_id="tenant-a", metadata={}
    )
    second = _new_run_input(
        query="second", thread_id="thread-1", owner_id="tenant-a", metadata={}
    )
    assert first["run_id"] != second["run_id"]
    assert second["approved_step_ids"] == []
    assert second["approval_granted"] is None
    assert second["plan"] is None
    assert second["final_answer"] is None


def test_capability_is_signed_and_bound_to_every_execution_dimension() -> None:
    token = issue_tool_capability(
        run_id="run-1",
        step_id="step-1",
        owner_id="tenant-a",
        agent_name="coding_agent",
        allowed_tools=["repo_write_file"],
    )
    claims = verify_tool_capability(
        token,
        run_id="run-1",
        step_id="step-1",
        owner_id="tenant-a",
        agent_name="coding_agent",
    )
    assert claims is not None
    assert claims.allowed_tools == ("repo_write_file",)
    assert (
        verify_tool_capability(
            token,
            run_id="run-2",
            step_id="step-1",
            owner_id="tenant-a",
            agent_name="coding_agent",
        )
        is None
    )
    assert verify_tool_capability(
        token + "tampered",
        run_id="run-1",
        step_id="step-1",
        owner_id="tenant-a",
        agent_name="coding_agent",
    ) is None


def test_planner_infers_narrow_tool_scope_for_approval() -> None:
    assert _infer_approval_tools("write config file") == ["repo_write_file"]
    assert _infer_approval_tools("run bash tests") == ["code_exec_bash"]
    assert set(_infer_approval_tools("write a file then run shell tests")) == {
        "repo_write_file",
        "code_exec_bash",
    }


def test_a2a_executor_ignores_unsigned_hitl_boolean() -> None:
    executor = A2AAgentExecutor(CODING_AGENT_CARD, _UnusedGraph())
    request = A2ATaskRequest(
        task_id="step-1",
        target_agent="coding",
        payload=A2ATaskPayload(instruction="write"),
        metadata={
            "hitl_approved": True,
            "run_id": "run-1",
            "owner_id": "tenant-a",
        },
    )
    context = executor._execution_context(request)
    assert context.hitl_approved is False
    assert context.owner_id is None

    identity = issue_tool_capability(
        run_id="run-1",
        step_id="step-1",
        owner_id="tenant-a",
        agent_name="coding_agent",
        allowed_tools=[],
    )
    request.metadata["approval_capability"] = identity
    context = executor._execution_context(request)
    assert context.owner_id == "tenant-a"
    assert context.hitl_approved is False


def test_mcp_boundary_requires_tool_specific_capability() -> None:
    token = set_tool_execution_context(
        ToolExecutionContext(
            agent_name="coding_agent",
            step_id="step-1",
            hitl_approved=True,
            owner_id="tenant-a",
            run_id="run-1",
            allowed_tools=(),
        )
    )
    try:
        with pytest.raises(PermissionError, match="step-scoped capability"):
            authorize_tool_execution("repo_write_file")
    finally:
        reset_tool_execution_context(token)


def test_tenant_filter_only_allows_explicit_shared_or_matching_tenant() -> None:
    filters = {"tenant_id": "tenant-a"}
    assert HybridRetriever._metadata_permitted({}, filters) is False
    assert HybridRetriever._metadata_permitted({"visibility": "shared"}, filters) is True
    assert HybridRetriever._metadata_permitted({"tenant_id": "tenant-a"}, filters) is True
    assert HybridRetriever._metadata_permitted({"tenant_id": "tenant-b"}, filters) is False


def test_manifests_have_single_vault_owned_secret_and_matching_sandbox_selector() -> None:
    root = Path(__file__).resolve().parents[2]
    config = (root / "deploy/k8s/configmap-secrets.yaml").read_text(encoding="utf-8")
    external = (root / "deploy/k8s/external-secret.yaml").read_text(encoding="utf-8")
    kustomization = (root / "deploy/k8s/kustomization.yaml").read_text(encoding="utf-8")
    ingress = (root / "deploy/k8s/ingress.yaml").read_text(encoding="utf-8")
    assert "kind: Secret\n" not in config
    assert "creationPolicy: Owner" in external
    assert "POSTGRES_URI" in external and "POSTGRES_PASSWORD" in external
    assert "commonLabels:" not in kustomization
    assert "namespace: default" not in ingress


def test_runtime_images_are_lockfile_driven_and_exclude_state() -> None:
    root = Path(__file__).resolve().parents[2]
    dockerignore = (root / ".dockerignore").read_text(encoding="utf-8")
    assert "data/memory/" in dockerignore
    for name in ("Dockerfile.api", "Dockerfile.agent", "Dockerfile.mcp"):
        content = (root / "deploy/docker" / name).read_text(encoding="utf-8")
        assert "COPY pyproject.toml uv.lock README.md" in content
        assert "uv sync --frozen --no-dev --no-editable" in content
        assert "uv:latest" not in content
        assert "COPY --chown=appuser:appgroup . /app" not in content
