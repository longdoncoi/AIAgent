"""Regression tests for repository-level production-readiness controls."""

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _yaml_documents(path: Path) -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]


def _alert_rules(path: Path) -> list[dict[str, Any]]:
    documents = _yaml_documents(path)
    prometheus_rule = next(
        (doc for doc in documents if doc.get("kind") == "PrometheusRule"),
        documents[0],
    )
    groups = prometheus_rule.get("spec", prometheus_rule)["groups"]
    return [rule for group in groups for rule in group["rules"]]


def test_production_vllm_schedule_is_explicit_and_bounded() -> None:
    overlay = ROOT / "deploy" / "overlays" / "production"
    kustomization = yaml.safe_load((overlay / "kustomization.yaml").read_text(encoding="utf-8"))
    assert "vllm-scheduled-scaling.yaml" in kustomization["resources"]

    documents = _yaml_documents(overlay / "vllm-scheduled-scaling.yaml")
    scaled_object = next(doc for doc in documents if doc["kind"] == "ScaledObject")
    spec = scaled_object["spec"]
    assert spec["scaleTargetRef"]["name"] == "vllm"
    assert spec["minReplicaCount"] == 1
    assert spec["maxReplicaCount"] == 4

    cron = next(trigger for trigger in spec["triggers"] if trigger["type"] == "cron")
    assert cron["metadata"] == {
        "timezone": "Asia/Ho_Chi_Minh",
        "start": "0 6 * * *",
        "end": "0 23 * * *",
        "desiredReplicas": "4",
    }


def test_canonical_and_deployed_alert_rules_are_identical() -> None:
    canonical = _alert_rules(ROOT / "deploy" / "monitoring" / "alerts.yaml")
    deployed = _alert_rules(ROOT / "deploy" / "k8s" / "monitoring.yaml")
    assert canonical == deployed

    required = {
        "HighAPIErrorRate",
        "HighLLMLatencyP99",
        "GPUOOMAlert",
        "ModelCircuitBreakerTripped",
        "HighPendingApprovalsQueue",
    }
    assert required.issubset({rule["alert"] for rule in canonical})
    gpu_oom = next(rule for rule in canonical if rule["alert"] == "GPUOOMAlert")
    assert "kube_pod_container_status_last_terminated_reason" in gpu_oom["expr"]
    assert 'container="vllm"' in gpu_oom["expr"]
    assert 'reason="OOMKilled"' in gpu_oom["expr"]


def test_prr_documents_match_manual_attested_argocd_policy() -> None:
    application = _yaml_documents(ROOT / "deploy" / "k8s" / "argocd-application.yaml")[0]
    assert "automated" not in application["spec"]["syncPolicy"]

    review = (
        ROOT / ".workflows" / "reviews" / "REVIEW_CI-CD & GITOPS.md"
    ).read_text(encoding="utf-8")
    assert "hỗ trợ self-healing và auto-prune" not in review
    assert "đồng bộ thủ công sau attestation" in review.lower()

    observability_review = (
        ROOT / ".workflows" / "reviews" / "REVIEW_OBSERVABILITY & ALERTING.md"
    ).read_text(encoding="utf-8")
    for alert in (
        "HighAPIErrorRate",
        "HighLLMLatencyP99",
        "GPUOOMAlert",
        "ModelCircuitBreakerTripped",
        "HighPendingApprovalsQueue",
    ):
        assert alert in observability_review


def test_ci_scans_full_history_for_secrets_with_pinned_gitleaks() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    lint_job = ci.split("lint-and-audit:", maxsplit=1)[1].split(
        "test-and-eval:", maxsplit=1
    )[0]
    assert "fetch-depth: 0" in lint_job
    assert 'GITLEAKS_VERSION: "8.30.1"' in lint_job
    assert "gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" in lint_job
    assert "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb" in lint_job
    assert '"$RUNNER_TEMP/gitleaks" git .' in lint_job
    assert "--redact" in lint_job
    assert (ROOT / ".gitleaks.toml").is_file()
    assert (ROOT / ".gitleaksignore").is_file()


def test_ci_generates_sbom_provenance_and_signs_release_digests() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    build_job = ci.split("build-and-scan:", maxsplit=1)[1].split(
        "release-gate:", maxsplit=1
    )[0]
    assert "Generate CycloneDX SBOM" in build_job
    assert "format: 'cyclonedx'" in build_job
    assert "Upload CycloneDX SBOM" in build_job

    publish_job = ci.split("publish-and-deploy:", maxsplit=1)[1]
    assert "id-token: write" in publish_job
    assert "sbom: true" in publish_job
    assert publish_job.count("provenance: mode=max") == 3
    installer = re.search(r"sigstore/cosign-installer@([0-9a-f]{40})", publish_job)
    assert installer is not None
    assert installer.group(1) == "756fe2d284c7292c4e9b62c8a28f1e5629c59532"
    for component in ("api", "agent", "mcp"):
        assert f"steps.build-{component}.outputs.digest" in publish_job
        assert f"ghcr.io/${{{{ env.REPO_LOWER }}}}/{component}@" in publish_job
    assert "cosign sign --yes" in publish_job
    assert "cosign verify" in publish_job
