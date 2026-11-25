"""Regression tests for fail-fast, lockfile-reproducible release evaluation."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

import evals.run_eval as run_eval
from src.agentic.rag import reranker, tools

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_bounded_evaluation_times_out_fail_closed() -> None:
    with pytest.raises(run_eval.EvaluationTimeout, match="sample timed out"):
        await run_eval._bounded(asyncio.sleep(0.05), label="sample", timeout=0.001)


@pytest.mark.asyncio
async def test_smoke_mode_forces_heuristics_and_restores_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, str | None] = {}

    async def fake_impl(mode: str, gateway_online: bool) -> bool:
        assert mode == "smoke"
        assert gateway_online is True
        observed["planner"] = os.environ.get("FAST_HEURISTIC_PLANNER")
        observed["judge"] = os.environ.get("SKIP_LLM_JUDGE")
        return True

    monkeypatch.setattr(run_eval, "gateway_reachable", lambda: True)
    monkeypatch.setattr(run_eval, "_run_benchmark_impl", fake_impl)
    monkeypatch.setenv("FAST_HEURISTIC_PLANNER", "previous")
    monkeypatch.delenv("SKIP_LLM_JUDGE", raising=False)

    assert await run_eval.run_benchmark("smoke") is True
    assert observed == {"planner": "true", "judge": "true"}
    assert os.environ["FAST_HEURISTIC_PLANNER"] == "previous"
    assert "SKIP_LLM_JUDGE" not in os.environ


def test_explicit_smoke_reranker_does_not_load_neural_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reranker, "_require_neural", lambda: False)
    monkeypatch.setattr(
        reranker,
        "_get_cross_encoder",
        lambda _name: pytest.fail("smoke mode attempted to load CrossEncoder weights"),
    )
    instance = reranker.CrossEncoderReranker(use_neural=False)
    assert instance._cross_encoder is None
    assert instance.mode == "heuristic"


def test_explicit_heuristic_reranker_cannot_bypass_strict_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reranker, "_require_neural", lambda: True)
    instance = reranker.CrossEncoderReranker(use_neural=False)
    with pytest.raises(RuntimeError, match="explicit_heuristic_mode"):
        _ = instance._cross_encoder


@pytest.mark.asyncio
async def test_async_search_forwards_internal_reranker_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_to_thread(function: object, *args: object, **kwargs: object) -> str:
        captured.update({"function": function, "args": args, "kwargs": kwargs})
        return "context"

    monkeypatch.setattr(tools.asyncio, "to_thread", fake_to_thread)
    result = await tools.asearch_policy("query", force_heuristic_reranker=True)
    assert result == "context"
    assert captured["function"] is tools.search_policy
    assert captured["kwargs"] == {"force_heuristic_reranker": True}


def test_release_workflow_and_prr_are_fail_closed_and_lockfile_reproducible() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    evidence = (ROOT / "scripts" / "generate_prr_evidence.py").read_text(encoding="utf-8")
    gate = (ROOT / "evals" / "release_gate.py").read_text(encoding="utf-8")

    release_job = ci.split("  release-gate:", 1)[1].split("  # ----", 1)[0]
    assert "uv sync --frozen --extra dev --extra eval --extra neural --extra deepeval" in release_job
    assert "uv run --no-sync python evals/release_gate.py" in release_job
    assert "timeout-minutes: 120" in release_job
    assert "PRR all_checks_passed is not true" in ci
    assert "PRR contains failed checks" in ci
    assert "return 0 if all_passed else 1" in evidence
    assert "if not DEEPEVAL_INSTALLED" in gate
    assert "asyncio.wait_for(operation(), timeout=timeout)" in gate
