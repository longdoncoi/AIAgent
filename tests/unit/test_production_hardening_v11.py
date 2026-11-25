"""Regression tests for the 2026-11-27 production-readiness remediations."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace

import yaml

from tests.load.quality_gate import load_gate_failures

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"
MEMORY_LIFECYCLE = ROOT / "src" / "agentic" / "memory" / "data_lifecycle.py"
CODE_EXEC = ROOT / "src" / "agentic" / "tools" / "servers" / "code_exec_server.py"


def test_production_overlay_is_a_sibling_of_the_kustomize_base() -> None:
    overlay = ROOT / "deploy" / "overlays" / "production" / "kustomization.yaml"
    legacy_overlay = ROOT / "deploy" / "k8s" / "overlays" / "production"
    assert overlay.is_file()
    assert not (legacy_overlay / "kustomization.yaml").exists()
    document = yaml.safe_load(overlay.read_text(encoding="utf-8"))
    assert document["resources"] == [
        "../../k8s",
        "vllm-scheduled-scaling.yaml",
    ]


def test_ci_renders_the_exact_production_overlay() -> None:
    ci = CI.read_text(encoding="utf-8")
    command = "kubectl kustomize deploy/overlays/production"
    assert ci.count(command) >= 2


def test_memory_layer_has_no_reverse_dependency_on_rag() -> None:
    tree = ast.parse(MEMORY_LIFECYCLE.read_text(encoding="utf-8"))
    forbidden: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("src.agentic.rag"):
            forbidden.append(node.module or "")
        elif isinstance(node, ast.Import):
            forbidden.extend(
                alias.name for alias in node.names if alias.name.startswith("src.agentic.rag")
            )
    assert forbidden == []


def test_async_code_exec_has_no_direct_file_io() -> None:
    tree = ast.parse(CODE_EXEC.read_text(encoding="utf-8"))
    violations: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for child in ast.walk(node):
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "open"
            ):
                violations.append(f"{node.name}:open")
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr in {"read_text", "write_text"}
            ):
                    parent_is_to_thread = any(
                        isinstance(candidate, ast.Call)
                        and isinstance(candidate.func, ast.Attribute)
                        and candidate.func.attr == "to_thread"
                        and child in ast.walk(candidate)
                        for candidate in ast.walk(node)
                    )
                    if not parent_is_to_thread:
                        violations.append(f"{node.name}:{child.func.attr}")
    assert violations == []


def test_ci_enforces_declared_line_branch_and_core_coverage() -> None:
    checker = ROOT / "scripts" / "check_coverage_thresholds.py"
    assert checker.is_file()
    ci = CI.read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "scripts/check_coverage_thresholds.py" in ci
    assert "scripts/check_coverage_thresholds.py" in makefile
    assert "--cov-report=json:coverage.json" in ci


def test_make_test_prepares_the_rag_index_and_uses_locked_python() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "test: test-prepare" in makefile
    assert "test-prepare:" in makefile
    assert "scripts/ingest.py" in makefile
    assert "PYTHON := uv run --no-sync python" in makefile
    assert "PYTEST := uv run --no-sync pytest" in makefile


def test_external_production_images_are_digest_pinned() -> None:
    image_pattern = re.compile(r"^\s*image:\s*([^\s]+)", re.MULTILINE)
    for manifest in (
        ROOT / "deploy" / "k8s" / "datastores.yaml",
        ROOT / "deploy" / "k8s" / "serving.yaml",
        ROOT / "deploy" / "infra" / "docker-compose.stack.yml",
    ):
        images = image_pattern.findall(manifest.read_text(encoding="utf-8"))
        assert images, f"no images found in {manifest}"
        for image in images:
            if image.startswith("ghcr.io/agentic-platform/"):
                continue
            assert re.search(r"@sha256:[0-9a-f]{64}$", image), (
                f"{manifest}: external image is not digest pinned: {image}"
            )


def test_release_quality_thresholds_match_the_strictest_governance_gate() -> None:
    config = yaml.safe_load(
        (ROOT / "configs" / "eval_thresholds.yaml").read_text(encoding="utf-8")
    )
    assert config["quality_gates"]["groundedness"] >= 0.90
    assert config["ragas_gates"] == {
        "faithfulness": 0.90,
        "answer_relevance": 0.85,
        "context_precision": 0.80,
        "context_recall": 0.80,
    }


def test_load_gate_fails_on_error_or_latency_budget_breach() -> None:
    passing = SimpleNamespace(
        fail_ratio=0.0009,
        get_response_time_percentile=lambda percentile: {0.95: 2499, 0.99: 4499}[percentile],
    )
    failing = SimpleNamespace(
        fail_ratio=0.001,
        get_response_time_percentile=lambda percentile: {0.95: 2500, 0.99: 4500}[percentile],
    )
    assert load_gate_failures(passing) == []
    failures = load_gate_failures(failing)
    assert len(failures) == 3
    assert any("error rate" in failure for failure in failures)
    assert any("P95" in failure for failure in failures)
    assert any("P99" in failure for failure in failures)
