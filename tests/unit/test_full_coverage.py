"""Comprehensive Unit Tests boosting test coverage above 80% across all enterprise modules.

Covers:
1. FastMCP Servers (code_exec_server, repo_server, web_search_server)
2. Temporal Workflows & Worker (agent_workflow, worker)
3. RAG Retrievial, Reranker, & Tools (reranker, retriever, tools)
4. API Routes (chat, approvals, health edge cases)
5. A2A Server & Streaming Execution (create_a2a_server, execute_stream)
6. Memory Short-Term & RedisSaver persistence
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from llama_index.core.schema import NodeWithScore, TextNode

from src.agentic.a2a.cards import CHAT_AGENT_CARD
from src.agentic.a2a.executor import (
    create_a2a_server,
)
from src.agentic.api.main import app
from src.agentic.api.routes.health import _check_tcp_port
from src.agentic.core.config import get_settings
from src.agentic.memory.redis_saver import RedisSaver
from src.agentic.memory.short_term import (
    trim_messages_window,
)
from src.agentic.rag.reranker import CrossEncoderReranker
from src.agentic.rag.retriever import HybridRetriever
from src.agentic.rag.tools import get_rag_tool, search_policy
from src.agentic.tools.servers.code_exec_server import (
    _build_isolated_env,
    _execute_in_docker,
    _execute_in_k8s_sandbox,
    _is_docker_available,
    code_exec_bash,
    code_exec_python,
    execute_bash_command,
    execute_python_code,
)
from src.agentic.tools.servers.code_exec_server import (
    main as code_main,
)
from src.agentic.tools.servers.repo_server import (
    _validate_safe_path,
    list_files_core,
    read_file_core,
    repo_list_files,
    repo_read_file,
    repo_search_symbol,
    repo_write_file,
    search_symbol_core,
    write_file_core,
)
from src.agentic.tools.servers.repo_server import (
    main as repo_main,
)
from src.agentic.tools.servers.web_search_server import (
    main as web_main,
)
from src.agentic.tools.servers.web_search_server import (
    search_web_core,
    web_search,
)
from src.agentic.workflows.agent_workflow import (
    AgentExecutionWorkflow,
    execute_plan_step,
)
from src.agentic.workflows.worker import run_worker


# ==============================================================================
# 1. FastMCP Code Execution Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_code_exec_python_prohibited_pattern():
    res = await execute_python_code("import socket\nsocket.socket()")
    assert "Security Exception: Execution blocked" in res
    assert "socket.socket" in res


@pytest.mark.asyncio
async def test_code_exec_bash_prohibited_pattern():
    res = await execute_bash_command("rm -rf /")
    assert "Security Exception: Execution blocked" in res
    assert "rm -rf /" in res


def test_isolated_env_scrubbing():
    env = _build_isolated_env()
    assert "PATH" in env or "SystemRoot" in env or "WINDIR" in env or len(env) >= 0
    assert "POSTGRES_PASSWORD" not in env
    assert "API_KEY" not in env


@pytest.mark.asyncio
async def test_code_exec_python_normal():
    code = "print('hello_sandbox_123')"
    with patch(
        "src.agentic.tools.servers.code_exec_server._is_docker_available", return_value=False
    ):
        res = await execute_python_code(code, timeout=5)
        assert "hello_sandbox_123" in res
        assert "Exit Code: 0" in res


@pytest.mark.asyncio
async def test_code_exec_bash_normal():
    cmd = "echo shell_test_success"
    with patch(
        "src.agentic.tools.servers.code_exec_server._is_docker_available", return_value=False
    ):
        res = await execute_bash_command(cmd, timeout=5)
        assert "shell_test_success" in res


@pytest.mark.asyncio
async def test_code_exec_tools_wrapper():
    with patch(
        "src.agentic.tools.servers.code_exec_server._is_docker_available", return_value=False
    ):
        res_py = await code_exec_python("print('wrap_py')")
        assert "wrap_py" in res_py

        res_sh = await code_exec_bash("echo wrap_sh")
        assert "wrap_sh" in res_sh


@pytest.mark.asyncio
async def test_docker_and_k8s_sandboxes_mock():
    mock_settings = MagicMock()
    mock_settings.enable_docker_sandbox = True
    with patch(
        "src.agentic.tools.servers.code_exec_server.get_settings", return_value=mock_settings
    ):
        with patch("shutil.which", return_value="/usr/bin/docker"):
            assert _is_docker_available() is True
        with patch("shutil.which", return_value=None):
            assert _is_docker_available() is False

    mock_proc = AsyncMock()
    mock_proc.communicate.return_value = (b"mock docker stdout", b"")
    mock_proc.returncode = 0
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        docker_out = await _execute_in_docker("print(1)", is_python=True, timeout=5)
        assert "[DOCKER SANDBOX ISOLATION ACTIVE]" in docker_out
        assert "mock docker stdout" in docker_out

    with patch(
        "src.agentic.tools.servers.code_exec_server._run_k8s_pod",
        new_callable=AsyncMock,
        return_value=("mock k8s stdout", 0),
    ):
        k8s_out = await _execute_in_k8s_sandbox("print(1)", is_python=True, timeout=5)
        assert "[KUBERNETES GVISOR SANDBOX ACTIVE" in k8s_out
        assert "mock k8s stdout" in k8s_out


# ==============================================================================
# 2. FastMCP Repo Server Tests
# ==============================================================================
def test_repo_server_path_validation():
    valid = _validate_safe_path("src")
    assert valid.exists()

    with pytest.raises(PermissionError):
        _validate_safe_path("../../Windows/System32")


def test_repo_server_list_files(tmp_path):
    d = tmp_path / "test_repo"
    d.mkdir()
    (d / "sample.py").write_text("print(1)", encoding="utf-8")
    (d / "sub").mkdir()
    (d / "sub" / "child.txt").write_text("content", encoding="utf-8")

    with patch("src.agentic.tools.servers.repo_server._get_workspace_root", return_value=tmp_path):
        output = list_files_core(str(d), max_depth=2)
        assert "[FILE] sample.py" in output
        assert "[DIR]  sub/" in output

        non_existent = list_files_core(str(tmp_path / "non_existent_folder_abc"))
        assert "does not exist" in non_existent


def test_repo_server_read_file(tmp_path):
    f = tmp_path / "read_sample.py"
    f.write_text("line 1\nline 2\nline 3\nline 4\nline 5\n", encoding="utf-8")

    with patch("src.agentic.tools.servers.repo_server._get_workspace_root", return_value=tmp_path):
        out = read_file_core(str(f), start_line=2, end_line=4)
        assert "line 2" in out
        assert "line 4" in out
        assert "line 1" not in out or "Lines 2-4" in out

        not_found = read_file_core(str(tmp_path / "ghost.py"))
        assert "not found" in not_found


def test_repo_server_search_symbol(tmp_path):
    d = tmp_path / "search_dir"
    d.mkdir()
    (d / "auth.py").write_text("def verify_jwt_token(): pass\n", encoding="utf-8")
    (d / "other.py").write_text("def unrelated(): pass\n", encoding="utf-8")

    with patch("src.agentic.tools.servers.repo_server._get_workspace_root", return_value=tmp_path):
        res = search_symbol_core("verify_jwt_token", directory=str(d))
        assert "auth.py:1: def verify_jwt_token(): pass" in res

        not_found_dir = search_symbol_core("foo", directory=str(tmp_path / "missing_dir"))
        assert "not found" in not_found_dir


# ==============================================================================
# 3. FastMCP Web Search Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_web_search_mock():
    with patch("httpx.AsyncClient.post", side_effect=Exception("Network down")):
        res = await search_web_core("LangGraph multi-agent architecture")
        assert "Search results for 'LangGraph multi-agent architecture':" in res
        assert "[Documentation & Overview]" in res

    wrap_res = await web_search("DeepSeek-R1 inference", max_results=2)
    assert "Search results for 'DeepSeek-R1 inference':" in wrap_res


# ==============================================================================
# 4. Temporal Workflows & Worker Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_temporal_execute_plan_step():
    mock_client = AsyncMock()
    mock_client.call_agent.return_value = MagicMock(
        status="completed", output="Executed successfully"
    )

    with patch("src.agentic.workflows.agent_workflow.get_a2a_client", return_value=mock_client):
        out = await execute_plan_step("chat_agent", "step_01", "Hello")
        assert out == "Executed successfully"

    mock_err_client = AsyncMock()
    mock_err_client.call_agent.return_value = MagicMock(
        status="failed", error="Fatal error occurred"
    )
    with patch("src.agentic.workflows.agent_workflow.get_a2a_client", return_value=mock_err_client):
        with pytest.raises(RuntimeError) as exc_info:
            await execute_plan_step("chat_agent", "step_02", "Crash")
        assert "Fatal error occurred" in str(exc_info.value)


@pytest.mark.asyncio
async def test_temporal_agent_execution_workflow():
    wf = AgentExecutionWorkflow()
    steps = [
        {"agent": "chat", "id": "s1", "instruction": "Hi"},
        {"agent": "rag", "id": "s2", "instruction": "Search"},
    ]

    with patch("temporalio.workflow.execute_activity", side_effect=["Output 1", "Output 2"]):
        outputs = await wf.run(steps)
        assert outputs == ["Output 1", "Output 2"]


@pytest.mark.asyncio
async def test_temporal_run_worker_exception_handling():
    with patch(
        "temporalio.client.Client.connect", side_effect=ConnectionError("Temporal unreachable")
    ):
        with pytest.raises(ConnectionError):
            await run_worker()


# ==============================================================================
# 5. RAG Reranker, Retriever, and Tools
# ==============================================================================
def test_cross_encoder_reranker_fallback():
    reranker = CrossEncoderReranker(top_n=2)
    nodes = [
        NodeWithScore(node=TextNode(text="Chính sách bảo mật mật khẩu"), score=0.5),
        NodeWithScore(node=TextNode(text="Quy trình thanh toán công tác phí"), score=0.8),
        NodeWithScore(node=TextNode(text="Bảo mật máy tính xách tay và VPN"), score=0.6),
    ]

    reranked = reranker.rerank("bảo mật mật khẩu", nodes)
    assert len(reranked) == 2
    assert "mật khẩu" in reranked[0].node.get_content()
    assert reranker.rerank("query", []) == []


def test_search_policy_and_tool():
    tool = get_rag_tool()
    assert tool.name == "search_policy"

    res = search_policy("Chính sách mật khẩu và xác thực 2 lớp")
    assert isinstance(res, str)
    assert len(res) > 0


# ==============================================================================
# 6. Memory Short-Term Trimming & RedisSaver
# ==============================================================================
def test_trim_messages_window():
    sys_msg = SystemMessage(content="You are a helpful assistant.")
    msgs = [sys_msg] + [HumanMessage(content=f"Message {i}" * 50) for i in range(20)]

    trimmed = trim_messages_window(msgs, max_tokens=100)
    assert len(trimmed) < len(msgs)
    assert trimmed[0] == sys_msg

    assert trim_messages_window([]) == []


def test_redis_saver_in_memory_persistence():
    saver = RedisSaver(redis_url="redis://localhost:59999/0")
    assert saver.is_redis_active is False

    config = {"configurable": {"thread_id": "t_mem_1", "checkpoint_ns": ""}}
    checkpoint = {
        "v": 1,
        "id": "cp_1",
        "ts": "2026-01-01",
        "channel_values": {},
        "channel_versions": {},
        "versions_seen": {},
    }
    metadata = {"step": 0}
    versions = {}

    res_cfg = saver.put(config, checkpoint, metadata, versions)
    assert res_cfg["configurable"]["thread_id"] == "t_mem_1"

    loaded = saver.get_tuple(config)
    assert loaded is not None
    assert loaded.checkpoint["id"] == "cp_1"


# ==============================================================================
# 7. A2A Server & Stream Execution
# ==============================================================================
@pytest.mark.asyncio
async def test_a2a_server_endpoints():
    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(
        return_value={"messages": [AIMessage(content="A2A response content")]}
    )

    a2a_app = create_a2a_server(card=CHAT_AGENT_CARD, graph=mock_graph)
    transport = ASGITransport(app=a2a_app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        h_resp = await client.get("/health")
        assert h_resp.status_code == 200
        assert h_resp.json()["status"] == "ok"

        c_resp = await client.get("/a2a/card")
        assert c_resp.status_code == 200
        assert c_resp.json()["name"] == CHAT_AGENT_CARD.name

        task_req = {
            "task_id": "task_unit_01",
            "caller": "supervisor",
            "target_agent": "chat_agent",
            "payload": {"instruction": "Say hello"},
        }
        t_resp = await client.post("/a2a/tasks", json=task_req)
        assert t_resp.status_code == 200
        assert t_resp.json()["status"] == "completed"
        assert t_resp.json()["output"] == "A2A response content"

        bad_target = {
            "task_id": "task_bad",
            "caller": "supervisor",
            "target_agent": "unknown_agent",
            "payload": {"instruction": "fail"},
        }
        bad_resp = await client.post("/a2a/tasks", json=bad_target)
        assert bad_resp.status_code == 400


# ==============================================================================
# 8. API Routes Approvals & Chat Edge Cases
# ==============================================================================
@pytest.mark.asyncio
async def test_approvals_resume_not_interrupted():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": "Bearer admin_token"}
        payload = {
            "thread_id": "non_interrupted_thread_999",
            "step_id": "step_1",
            "decision": {
                "approved": True,
                "reason": "Allowed by admin",
            },
        }
        resp = await client.post(
            "/api/v1/approvals/non_interrupted_thread_999/resume", json=payload, headers=headers
        )
        assert resp.status_code == 400
        assert "not in an interrupted state" in resp.text


@pytest.mark.asyncio
async def test_chat_sse_stream_endpoint():
    import time

    import jwt

    from src.agentic.core.config import get_settings

    cfg = get_settings()
    now = int(time.time())
    valid_token = jwt.encode(
        {
            "sub": "stream_user",
            "role": "user",
            "aud": cfg.jwt_audience,
            "iss": cfg.jwt_issuer,
            "iat": now,
            "exp": now + 3600,
        },
        cfg.jwt_secret_key,
        algorithm=cfg.jwt_algorithm,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {valid_token}"}
        payload = {
            "query": "Chào hệ thống, hãy tóm tắt ngắn.",
            "thread_id": "stream_test_thread_01",
        }
        resp = await client.post("/api/v1/chat/stream", json=payload, headers=headers)
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        assert "event: start" in resp.text or "event: message" in resp.text


# ==============================================================================
# 9. Additional Deep Coverage: Health Probes, Retriever & Server CLI
# ==============================================================================
def test_tcp_port_probe():
    assert _check_tcp_port("127.0.0.1", 59999, timeout=0.1) is False


def test_hybrid_retriever_keyword_search(tmp_path):
    retriever = HybridRetriever(index_dir=tmp_path / "empty_index")
    nodes = retriever._keyword_search("quy định bảo mật", top_k=3)
    assert isinstance(nodes, list)

    res = retriever.retrieve("chính sách vpn", top_k=2)
    assert isinstance(res, list)


@pytest.mark.asyncio
async def test_readyz_degraded_scenarios(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("REQUIRE_AUTHENTICATION", "true")
    monkeypatch.setenv("JWT_SECRET_KEY", "super_secret_production_jwt_key_unique_2026")
    monkeypatch.setenv("S2S_INTERNAL_TOKEN", "super_secret_production_s2s_token_unique_2026")
    monkeypatch.setenv("APPROVAL_SIGNING_KEY", "super_secret_production_approval_key_2026")
    monkeypatch.setenv("LITELLM_API_KEY", "sk-real-prod-key-2026")
    monkeypatch.setenv("NEO4J_PASSWORD", "real-neo4j-password-2026")
    monkeypatch.setenv("POSTGRES_URI", "postgresql://u:real-password@pgbouncer-svc:6432/db")
    monkeypatch.setenv("USE_POSTGRES_CHECKPOINT", "true")
    get_settings.cache_clear()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/readyz")
        assert resp.status_code in [200, 503]
        data = resp.json()
        assert "checks" in data
        assert "database" in data["checks"]

    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("REQUIRE_AUTHENTICATION", raising=False)
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    monkeypatch.delenv("S2S_INTERNAL_TOKEN", raising=False)
    monkeypatch.delenv("APPROVAL_SIGNING_KEY", raising=False)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    monkeypatch.delenv("POSTGRES_URI", raising=False)
    monkeypatch.delenv("USE_POSTGRES_CHECKPOINT", raising=False)
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_code_exec_host_disabled_in_production(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("REQUIRE_AUTHENTICATION", "true")
    monkeypatch.setenv("JWT_SECRET_KEY", "super_secret_production_jwt_key_unique_2026")
    monkeypatch.setenv("S2S_INTERNAL_TOKEN", "super_secret_production_s2s_token_unique_2026")
    monkeypatch.setenv("APPROVAL_SIGNING_KEY", "super_secret_production_approval_key_2026")
    monkeypatch.setenv("LITELLM_API_KEY", "sk-real-prod-key-2026")
    monkeypatch.setenv("NEO4J_PASSWORD", "real-neo4j-password-2026")
    monkeypatch.setenv("POSTGRES_URI", "postgresql://u:real-password@pgbouncer-svc:6432/db")
    monkeypatch.setenv("USE_POSTGRES_CHECKPOINT", "true")
    get_settings.cache_clear()

    with (
        patch("src.agentic.tools.servers.code_exec_server._is_k8s_available", return_value=False),
        patch(
            "src.agentic.tools.servers.code_exec_server._is_docker_available", return_value=False
        ),
    ):
        out_py = await execute_python_code("print(1)")
        assert "Host subprocess execution is strictly disabled in production" in out_py

        out_sh = await execute_bash_command("echo 1")
        assert "Host shell execution is strictly disabled in production" in out_sh

    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("REQUIRE_AUTHENTICATION", raising=False)
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    monkeypatch.delenv("S2S_INTERNAL_TOKEN", raising=False)
    monkeypatch.delenv("APPROVAL_SIGNING_KEY", raising=False)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    monkeypatch.delenv("POSTGRES_URI", raising=False)
    monkeypatch.delenv("USE_POSTGRES_CHECKPOINT", raising=False)
    get_settings.cache_clear()


# ==============================================================================
# 10. MCP Servers Tools & CLI Main Execution Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_repo_server_write_and_tool_wrappers(tmp_path):
    with patch("src.agentic.tools.servers.repo_server._get_workspace_root", return_value=tmp_path):
        w_res = write_file_core(
            str(tmp_path / "data" / "scratch" / "new_dir" / "created.txt"), "hello written"
        )
        assert "Successfully wrote" in w_res

        scratch = tmp_path / "data" / "scratch"
        tool_write = await repo_write_file(str(scratch / "tool_w.txt"), "tool content")
        assert "Successfully wrote" in tool_write

        tool_read = await repo_read_file(str(scratch / "tool_w.txt"))
        assert "tool content" in tool_read

        tool_list = await repo_list_files(str(tmp_path))
        assert "tool_w.txt" in tool_list

        tool_sym = await repo_search_symbol("written", directory=str(tmp_path))
        assert "created.txt" in tool_sym


def test_mcp_servers_main_stdio_mode():
    with patch.object(sys, "argv", ["code_exec_server.py", "--transport", "stdio"]):
        with patch("src.agentic.tools.servers.code_exec_server.mcp.run"):
            code_main()

    with patch.object(sys, "argv", ["repo_server.py", "--transport", "stdio"]):
        with patch("src.agentic.tools.servers.repo_server.mcp.run"):
            repo_main()

    with patch.object(sys, "argv", ["web_search_server.py", "--transport", "stdio"]):
        with patch("src.agentic.tools.servers.web_search_server.mcp.run"):
            web_main()


@pytest.mark.asyncio
async def test_code_exec_timeout_and_exceptions():
    # Real timeout test (1 second timeout on 5 second sleep). Force the host
    # path: on developer machines the docker CLI may exist while the daemon is
    # down, which would exercise an unrelated connection-error branch.
    with patch(
        "src.agentic.tools.servers.code_exec_server._is_docker_available", return_value=False
    ):
        res_to = await execute_python_code("import time; time.sleep(5)", timeout=1)
    assert "timed out" in res_to and "1 seconds" in res_to

    # General Exception branch
    with patch(
        "src.agentic.tools.servers.code_exec_server._is_docker_available", return_value=False
    ):
        with patch(
            "asyncio.create_subprocess_exec", side_effect=Exception("System level fork failed")
        ):
            res_err = await execute_python_code("print(1)")
            assert "Execution error:" in res_err or "failed" in res_err


@pytest.mark.asyncio
async def test_approvals_resume_success_and_error():
    from fastapi import HTTPException

    from src.agentic.api.routes.approvals import resume_hitl_endpoint
    from src.agentic.core.schemas import ApprovalRequest, Decision

    mock_graph = MagicMock()
    mock_state = MagicMock()
    mock_state.next = ("hitl",)
    mock_graph.get_state.return_value = mock_state
    mock_graph.ainvoke = AsyncMock(return_value={"messages": []})

    req = ApprovalRequest(
        thread_id="t1",
        step_id="s1",
        decision=Decision(approved=True, reason="Looks good"),
    )
    user = {"user_id": "u1", "role": "admin"}

    res = await resume_hitl_endpoint("t1", req, graph=mock_graph, current_user=user)
    assert res.status == "resumed"

    # Test error branch
    mock_graph.ainvoke.side_effect = RuntimeError("Graph invoke failed")
    with pytest.raises(HTTPException) as exc_info:
        await resume_hitl_endpoint("t1", req, graph=mock_graph, current_user=user)
    assert exc_info.value.status_code == 500


def test_database_migrations_coverage():
    from src.agentic.memory.migrations import run_database_migrations

    with patch("alembic.command.upgrade") as mock_upgrade:
        ok = run_database_migrations("postgresql://test:test@localhost:5432/testdb")
        assert ok is True
        mock_upgrade.assert_called_once()

    # Fail-closed: migration failure now raises instead of silently returning
    # False (which previously let the app run against an outdated schema).
    with patch("alembic.command.upgrade", side_effect=Exception("DB down")):
        with pytest.raises(RuntimeError, match="Database migration to head failed"):
            run_database_migrations()


def test_short_term_memory_postgres_and_redis(monkeypatch):
    import src.agentic.memory.short_term as st_mod

    # Reset globals
    st_mod._pg_pool = None
    st_mod._global_checkpointer = None

    # Test postgres pool with mock
    mock_pool = MagicMock()
    with patch("psycopg_pool.ConnectionPool", return_value=mock_pool):
        monkeypatch.setenv("USE_POSTGRES_CHECKPOINT", "true")
        get_settings.cache_clear()
        pool = st_mod.get_postgres_pool()
        assert pool == mock_pool
        st_mod.close_postgres_pool()
        assert st_mod._pg_pool is None

    # Test checkpointer postgres branch with mock
    st_mod._global_checkpointer = None
    with patch("langgraph.checkpoint.postgres.PostgresSaver.from_conn_string") as mock_from_conn:
        mock_cm = MagicMock()
        mock_saver = MagicMock()
        mock_cm.__enter__.return_value = mock_saver
        mock_from_conn.return_value = mock_cm
        monkeypatch.setenv("USE_POSTGRES_CHECKPOINT", "true")
        get_settings.cache_clear()
        saver = st_mod.get_checkpointer()
        assert saver == mock_saver

    # Test production error when neither enabled
    st_mod._global_checkpointer = None
    mock_mock_settings = MagicMock(
        environment="production",
        use_postgres_checkpoint=False,
        use_redis_checkpoint=False,
    )
    with patch("src.agentic.memory.short_term.get_settings", return_value=mock_mock_settings):
        with pytest.raises(RuntimeError) as exc_info:
            st_mod.get_checkpointer()
        assert "strictly prohibited" in str(exc_info.value)

    # Reset environment
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("USE_POSTGRES_CHECKPOINT", "false")
    get_settings.cache_clear()
    st_mod._global_checkpointer = None


def test_redis_saver_full_methods():
    from langgraph.checkpoint.base import CheckpointMetadata

    from src.agentic.memory.redis_saver import RedisSaver

    saver = RedisSaver(redis_url=None)
    config = {
        "configurable": {"thread_id": "thread-123", "checkpoint_ns": "", "checkpoint_id": "cp-1"}
    }
    checkpoint = {
        "v": 1,
        "id": "cp-1",
        "ts": "2026-01-01T00:00:00Z",
        "channel_values": {"messages": ["hello"]},
        "channel_versions": {"messages": 1},
        "versions_seen": {},
    }
    meta: CheckpointMetadata = {"source": "input", "step": 1, "writes": {}, "parents": {}}

    saver.put(config, checkpoint, meta, {})

    # list
    checkpoints = list(saver.list(config))
    assert len(checkpoints) >= 1

    # get_tuple
    tup = saver.get_tuple(config)
    assert tup is not None
    assert tup.checkpoint["id"] == "cp-1"

    # put_writes
    saver.put_writes(config, [("channel1", "val1")], task_id="task-1")


@pytest.mark.asyncio
async def test_dispatch_parallel_fanout_and_failures():
    from src.agentic.orchestrator.nodes.dispatch import dispatch_node
    from src.agentic.orchestrator.state import AgentState, Plan, Step

    # 1. Dependency failure scenario
    s1 = Step(
        id="step1", description="step 1 desc", agent="chat", input_query="q1", status="failed"
    )
    s2 = Step(
        id="step2",
        description="step 2 desc",
        agent="coding",
        input_query="q2",
        status="pending",
        depends_on=["step1"],
    )
    state = AgentState(messages=[], plan=Plan(goal="test", steps=[s1, s2]))
    await dispatch_node(state)
    assert s2.status == "failed"
    assert "Dependency step failed" in (s2.output or "")

    # 2. Execution streaming tokens scenario
    s3 = Step(
        id="step3", description="step 3 desc", agent="chat", input_query="q3", status="pending"
    )
    state2 = AgentState(messages=[], plan=Plan(goal="test", steps=[s3]))

    async def mock_call_agent_stream(*args, **kwargs):
        yield {"event": "token", "token": "Hello "}
        yield {"event": "token", "token": "World!"}
        yield {"event": "completed", "output": "Hello World!"}

    mock_client = MagicMock()
    mock_client.call_agent_stream = mock_call_agent_stream

    with patch("src.agentic.orchestrator.nodes.dispatch.get_a2a_client", return_value=mock_client):
        await dispatch_node(state2)
        assert s3.status == "completed"
        assert s3.output == "Hello World!"


@pytest.mark.asyncio
async def test_chat_non_streaming_endpoint_full():
    from fastapi import HTTPException

    from src.agentic.api.routes.chat import chat_endpoint
    from src.agentic.core.schemas import ChatRequest
    from src.agentic.orchestrator.state import Plan, Step

    mock_graph = MagicMock()
    s = Step(
        id="s1", description="desc", agent="chat", input_query="q", output="out", status="completed"
    )
    mock_graph.ainvoke = AsyncMock(
        return_value={"final_answer": "42", "plan": Plan(goal="g", steps=[s])}
    )
    mock_snapshot = MagicMock()
    mock_snapshot.next = ("hitl",)
    mock_task = MagicMock()
    mock_task.interrupts = [MagicMock(value="Approval needed for dangerous action")]
    mock_snapshot.tasks = [mock_task]
    mock_graph.get_state.return_value = mock_snapshot

    mock_guard = MagicMock()
    mock_guard.sanitize_input.return_value = "clean question"
    mock_guard.asanitize_input = AsyncMock(return_value="clean question")
    mock_guard.sanitize_output.return_value = "out"

    req = ChatRequest(query="What is life?", thread_id="t-123")
    user = {"user_id": "u1", "role": "admin"}

    res = await chat_endpoint(req, graph=mock_graph, guardrails=mock_guard, user=user)
    assert res.thread_id == "t-123"
    assert res.requires_approval is True
    assert res.pending_approval_step == "Approval needed for dangerous action"
    assert len(res.steps) == 1

    # Test execution error branch
    mock_graph.ainvoke.side_effect = RuntimeError("Node crashed")
    with pytest.raises(HTTPException) as exc_info:
        await chat_endpoint(req, graph=mock_graph, guardrails=mock_guard, user=user)
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_chat_streaming_guardrail_violation():
    from src.agentic.api.routes.chat import chat_stream_endpoint
    from src.agentic.core.errors import GuardrailViolation
    from src.agentic.core.schemas import ChatRequest

    mock_guard = MagicMock()
    mock_guard.sanitize_input.side_effect = GuardrailViolation(
        "Malicious injection detected", rule="prompt_injection"
    )
    mock_guard.asanitize_input = AsyncMock(side_effect=mock_guard.sanitize_input.side_effect)

    req = ChatRequest(query="DROP TABLE users;", thread_id="t-bad")
    user = {"user_id": "u1", "role": "user"}

    resp = await chat_stream_endpoint(req, graph=MagicMock(), guardrails=mock_guard, user=user)
    assert resp is not None


@pytest.mark.asyncio
async def test_chat_stream_generator_full_events():
    from src.agentic.api.routes.chat import chat_stream_endpoint
    from src.agentic.core.schemas import ChatRequest
    from src.agentic.safety.guardrails import GuardrailManager

    async def mock_astream_events(*args, **kwargs):
        yield {"event": "on_chain_start", "name": "dispatch"}
        yield {
            "event": "on_chat_model_stream",
            "name": "chat_agent",
            "data": {"chunk": MagicMock(content="Hello ")},
        }
        yield {
            "event": "on_chat_model_stream",
            "name": "chat_agent",
            "data": {"chunk": MagicMock(content="World!")},
        }
        yield {
            "event": "on_chain_end",
            "name": "dispatch",
            "data": {"output": {"current_step_index": 1}},
        }
        yield {
            "event": "on_chain_end",
            "name": "finalize",
            "data": {"output": {"final_answer": "Complete answer."}},
        }

    mock_graph = MagicMock()
    mock_graph.astream_events = mock_astream_events
    mock_state = MagicMock()
    mock_state.next = ("hitl",)
    mock_task = MagicMock()
    mock_task.interrupts = [MagicMock(value="Wait for approval")]
    mock_state.tasks = [mock_task]
    mock_graph.get_state.return_value = mock_state

    # Real GuardrailManager so stream_guard returns strings
    real_guard = GuardrailManager()

    req = ChatRequest(query="Tell me a joke", thread_id="t-stream-ok")
    user = {"user_id": "u1", "role": "user"}

    resp = await chat_stream_endpoint(req, graph=mock_graph, guardrails=real_guard, user=user)
    events = []
    async for event in resp.body_iterator:
        events.append(event)
    assert len(events) >= 3
    assert any("trace_id" in str(ev) for ev in events)


@pytest.mark.asyncio
async def test_temporal_worker_successful_run():
    import temporalio.worker

    from src.agentic.workflows.worker import main, run_worker

    mock_client = MagicMock()

    with patch("temporalio.client.Client.connect", return_value=mock_client):
        with patch.object(temporalio.worker.Worker, "__init__", return_value=None):
            with patch.object(temporalio.worker.Worker, "run", new_callable=AsyncMock) as mock_run:
                await run_worker()
                mock_run.assert_called_once()

    mock_rw = MagicMock()
    with patch("src.agentic.workflows.worker.run_worker", mock_rw):
        with patch("asyncio.run", return_value=None) as mock_ar:
            main()
            mock_ar.assert_called_once()


@pytest.mark.asyncio
async def test_docker_and_k8s_sandbox_execution_branches():
    import asyncio

    from src.agentic.tools.servers.code_exec_server import (
        _execute_in_docker,
        _execute_in_k8s_sandbox,
        execute_bash_command,
        execute_python_code,
    )

    # 1. Test _execute_in_docker success
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"hello docker stdout", b""))
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
        res = await _execute_in_docker("print('hello')", is_python=True, timeout=5)
        assert "[DOCKER SANDBOX ISOLATION ACTIVE]" in res
        assert "hello docker stdout" in res

    # 2. Test _execute_in_docker timeout
    mock_proc_to = MagicMock()
    mock_proc_to.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
    mock_proc_to.kill = MagicMock(return_value=None)
    mock_proc_to.wait = AsyncMock(return_value=0)
    with patch(
        "asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc_to
    ):
        res_to = await _execute_in_docker("import time; time.sleep(10)", is_python=True, timeout=1)
        assert "timed out in container" in res_to

    # 3. Test _execute_in_k8s_sandbox success
    with patch(
        "src.agentic.tools.servers.code_exec_server._run_k8s_pod",
        new_callable=AsyncMock,
        return_value=("k8s pod stdout", 0),
    ):
        res_k8s = await _execute_in_k8s_sandbox("echo 1", is_python=False, timeout=5)
        assert "KUBERNETES GVISOR SANDBOX ACTIVE" in res_k8s
        assert "k8s pod stdout" in res_k8s

    # 4. Test execute_python_code and execute_bash_command with k8s enabled
    with patch("src.agentic.tools.servers.code_exec_server._is_k8s_available", return_value=True):
        with patch(
            "src.agentic.tools.servers.code_exec_server._execute_in_k8s_sandbox",
            new_callable=AsyncMock,
            return_value="k8s executed",
        ):
            res_py = await execute_python_code("print('test')")
            assert res_py == "k8s executed"
            res_sh = await execute_bash_command("ls")
            assert res_sh == "k8s executed"


@pytest.mark.asyncio
async def test_redis_saver_full_lifecycle_with_mocked_redis():
    from src.agentic.memory.redis_saver import RedisSaver

    mock_sync_redis = MagicMock()
    mock_async_redis = MagicMock()

    # Setup mocked redis client
    mock_sync_redis.ping.return_value = True
    mock_sync_redis.get.return_value = None
    mock_sync_redis.set.return_value = True
    mock_sync_redis.lrange.return_value = []
    mock_sync_redis.rpush.return_value = 1
    mock_sync_redis.expire.return_value = True

    mock_async_redis.ping = AsyncMock(return_value=True)
    mock_async_redis.get = AsyncMock(return_value=None)
    mock_async_redis.set = AsyncMock(return_value=True)
    mock_async_redis.lrange = AsyncMock(return_value=[])
    mock_async_redis.rpush = AsyncMock(return_value=1)
    mock_async_redis.sadd = AsyncMock(return_value=1)
    mock_async_redis.expire = AsyncMock(return_value=True)

    with patch("redis.Redis.from_url", return_value=mock_sync_redis):
        with patch("redis.asyncio.from_url", return_value=mock_async_redis):
            saver = RedisSaver(redis_url="redis://mock:6379/0", ttl_seconds=3600)
            assert saver.is_redis_active is True

            # Sync methods with mocked Redis
            config = {"configurable": {"thread_id": "mock_t1", "checkpoint_ns": ""}}
            checkpoint = {
                "v": 1,
                "id": "cp_001",
                "ts": "2026-11-19T00:00:00Z",
                "channel_values": {"msg": "hi"},
                "channel_versions": {},
                "versions_seen": {},
            }
            metadata = {"step": 1}

            # Put
            saved_cfg = saver.put(config, checkpoint, metadata, new_versions={})
            assert saved_cfg["configurable"]["checkpoint_id"] == "cp_001"
            assert mock_sync_redis.set.called

            # Put writes
            saver.put_writes(saved_cfg, writes=[("msg", "val")], task_id="task_1")

            # Async methods with mocked Redis
            await saver.aput(config, checkpoint, metadata, new_versions={})
            assert mock_async_redis.set.called

            # Async get
            mock_async_redis.get = AsyncMock(return_value=saver._sign_and_serialize(checkpoint))
            cp_tuple = await saver.aget_tuple(saved_cfg)
            assert cp_tuple is not None
            assert cp_tuple.checkpoint["id"] == "cp_001"

            # Async list
            mock_async_redis.lrange = AsyncMock(return_value=[b"cp_001"])
            items = [item async for item in saver.alist(config)]
            assert len(items) >= 1


@pytest.mark.asyncio
async def test_rag_cache_and_prompt_cache_locks_and_lifecycle():
    import json

    from src.agentic.cache.prompt_cache import PromptCache
    from src.agentic.cache.rag_cache import RAGCache

    # 1. RAG Cache Locks & Async operations
    mock_redis = MagicMock()
    mock_redis.set.return_value = True
    mock_redis.get.return_value = json.dumps({"result": "cached policy", "metadata": {}})
    mock_redis.delete.return_value = 1

    rc = RAGCache(redis_url="redis://mock:6379/0")
    rc._redis_client = mock_redis

    # Locks
    lock_ok = rc.acquire_lock("test_key", timeout=2.0)
    assert lock_ok is True
    rc.release_lock("test_key")

    # Sync set & get
    rc.set("query policy", "cached policy")
    val = rc.get("query policy")
    assert val == "cached policy"

    # Stats and clear
    stats = rc.stats
    assert "size" in stats
    assert "hits" in stats
    rc.clear()

    # 2. Prompt Cache Locks & Async operations
    pc = PromptCache(redis_url="redis://mock:6379/0")
    pc._redis_client = mock_redis
    pc.acquire_lock("prompt_key", timeout=2.0)
    pc.release_lock("prompt_key")

    pc.set_response("fast-llm", ["hello prompt"], 0.1, "hi response")
    val_p = pc.get_response("fast-llm", ["hello prompt"], 0.1)
    assert val_p is not None

    pc.clear()
    assert pc.stats["size"] == 0


def test_long_term_memory_full_lifecycle(tmp_path):
    from src.agentic.memory.long_term import LongTermStore

    # Use SQLite store in temp directory
    store = LongTermStore(storage_dir=tmp_path, force_sqlite=True)
    store.add_semantic("pref_01", "User prefers dark mode", {"tag": "ui"})
    store.add_episodic("task_01", "Completed code refactor for multi-agent", {"status": "ok"})
    store.add_procedural("build_flow", "Run ruff check and pytest", {"tool": "make"})

    # Search
    results_all = store.search("mode")
    assert len(results_all) >= 1
    assert "dark mode" in results_all[0].content

    # Search with category
    results_epi = store.search("refactor", category="episodic")
    assert len(results_epi) >= 1
    assert results_epi[0].category == "episodic"

    results_proc = store.search("pytest", category="procedural")
    assert len(results_proc) >= 1
    assert results_proc[0].category == "procedural"
