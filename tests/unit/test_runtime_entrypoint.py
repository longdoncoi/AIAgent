"""Tests for the shell-less production image entrypoint."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from src.agentic import runtime_entrypoint


@pytest.mark.parametrize(
    ("role", "module", "port", "expected_argv"),
    [
        (
            "agent",
            "src.agentic.agents.rag.server",
            "9101",
            ["src.agentic.agents.rag.server", "--host", "0.0.0.0", "--port", "9101"],
        ),
        (
            "mcp",
            "src.agentic.tools.servers.repo_server",
            "9002",
            [
                "src.agentic.tools.servers.repo_server",
                "--port",
                "9002",
                "--transport",
                "streamable-http",
            ],
        ),
    ],
)
def test_main_dispatches_only_supported_modules(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    module: str,
    port: str,
    expected_argv: list[str],
) -> None:
    called: list[list[str]] = []
    fake_module = SimpleNamespace(main=lambda: called.append(sys.argv.copy()))
    monkeypatch.setenv("AGENT_MODULE" if role == "agent" else "MCP_SERVER_MODULE", module)
    monkeypatch.setenv("AGENT_PORT" if role == "agent" else "MCP_PORT", port)
    monkeypatch.setattr(sys, "argv", ["runtime-entrypoint-test"])
    monkeypatch.setattr(runtime_entrypoint.importlib, "import_module", lambda value: fake_module)

    runtime_entrypoint.main([role])

    assert called == [expected_argv]


@pytest.mark.parametrize(
    ("role", "env_name", "value"),
    [
        ("agent", "AGENT_MODULE", "malicious.module"),
        ("mcp", "MCP_SERVER_MODULE", "malicious.module"),
        ("agent", "AGENT_PORT", "0"),
        ("mcp", "MCP_PORT", "not-a-port"),
    ],
)
def test_main_fails_closed_for_invalid_runtime_configuration(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    env_name: str,
    value: str,
) -> None:
    monkeypatch.setenv(env_name, value)
    monkeypatch.setattr(
        runtime_entrypoint.importlib,
        "import_module",
        lambda value: pytest.fail(f"must not import {value}"),
    )

    with pytest.raises(SystemExit):
        runtime_entrypoint.main([role])


def test_main_rejects_unknown_role(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runtime_entrypoint.importlib,
        "import_module",
        lambda value: pytest.fail(f"must not import {value}"),
    )

    with pytest.raises(SystemExit):
        runtime_entrypoint.main(["unknown"])
