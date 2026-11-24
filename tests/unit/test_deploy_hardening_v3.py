"""P0 deployment/CI hardening regressions (v3).

Parses deployment manifests and the CI workflow directly from disk so the
checks run in a plain unit-test job and fail closed on unsafe regressions.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
K8S = ROOT / "deploy" / "k8s"
DOCKER = ROOT / "deploy" / "docker"
CI = ROOT / ".github" / "workflows" / "ci.yml"


def _load_all(path: Path) -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]


def _docs(*paths: Path) -> list[tuple[Path, dict[str, Any]]]:
    result: list[tuple[Path, dict[str, Any]]] = []
    for path in paths:
        result.extend((path, doc) for doc in _load_all(path))
    return result


def _deployment_docs() -> Iterator[tuple[Path, dict[str, Any]]]:
    for path, doc in _docs(*sorted(K8S.glob("*.yaml"))):
        yield path, doc


def _by_kind(kind: str) -> list[dict[str, Any]]:
    return [doc for _path, doc in _deployment_docs() if doc.get("kind") == kind]


def _named(kind: str, name: str) -> dict[str, Any]:
    for doc in _by_kind(kind):
        if doc.get("metadata", {}).get("name") == name:
            return doc
    raise AssertionError(f"no {kind} named {name!r} in deploy/k8s")


# ---------------------------------------------------------------------------
# 1. Immutable image references
# ---------------------------------------------------------------------------
def test_no_mutable_latest_tag_in_production_deployments() -> None:
    for path, doc in _deployment_docs():
        rendered = yaml.safe_dump(doc)
        assert not re.search(r"image:\s*\S+:latest\b", rendered), (
            f"{path.name} contains a mutable :latest image reference"
        )


def test_kustomization_uses_immutable_image_transformer() -> None:
    kust = yaml.safe_load((K8S / "kustomization.yaml").read_text(encoding="utf-8"))
    images = kust.get("images", [])
    names = {entry.get("name") for entry in images}
    assert {"ghcr.io/agentic-platform/api", "ghcr.io/agentic-platform/agent",
            "ghcr.io/agentic-platform/mcp"}.issubset(names)
    for entry in images:
        tag = str(entry.get("newTag", ""))
        assert tag and "latest" not in tag
        assert "REPLACE_WITH_COMMIT_SHA" in tag, "release CI must inject a reviewed SHA"
    assert "backup.yaml" in kust["resources"]


# ---------------------------------------------------------------------------
# 2. MCP probes are TCP (no /health route, auth required on HTTP)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,port",
    [
        ("agentic-mcp-code-exec", 9001),
        ("agentic-mcp-repo", 9002),
        ("agentic-mcp-web-search", 9003),
    ],
)
def test_mcp_probes_use_tcp_socket(name: str, port: int) -> None:
    deployment = _named("Deployment", name)
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    for probe_name in ("livenessProbe", "readinessProbe", "startupProbe"):
        probe = container[probe_name]
        assert "tcpSocket" in probe, f"{name}.{probe_name} must be a tcpSocket probe"
        assert probe["tcpSocket"]["port"] == port
        assert "httpGet" not in probe, f"{name}.{probe_name} must not use an HTTP route"
    assert container["startupProbe"]["failureThreshold"] >= 20


def test_api_healthz_probe_is_preserved() -> None:
    container = _named("Deployment", "agentic-api")["spec"]["template"]["spec"]["containers"][0]
    assert container["livenessProbe"]["httpGet"] == {"path": "/healthz", "port": 8000}


# ---------------------------------------------------------------------------
# 3. Namespace / ingress correctness
# ---------------------------------------------------------------------------
def test_platform_namespace_has_istio_injection_label() -> None:
    namespace = _named("Namespace", "agentic-platform")
    assert namespace["metadata"]["labels"]["istio-injection"] == "enabled"


def test_ingress_is_emitted_in_agentic_platform_namespace() -> None:
    ingress = _named("Ingress", "agentic-platform-ingress")
    assert ingress["metadata"].get("namespace") == "agentic-platform"
    assert ingress["metadata"].get("namespace") != "default"


def test_all_platform_resources_declare_their_namespace_explicitly() -> None:
    # The legacy builtin NamespaceTransformer is silently skipped by kubectl.
    exempt_kinds = {"Namespace", "CustomResourceDefinition"}
    exempt_names = {"agentic-platform-prod"}
    for path, doc in _deployment_docs():
        if path.name in {
            "namespace.yaml", "argocd-application.yaml",
            "kustomization.yaml", "namespace-transformer.yaml",
        }:
            continue
        if doc.get("kind") in exempt_kinds:
            continue
        name = doc.get("metadata", {}).get("name")
        if name in exempt_names:
            continue
        assert doc.get("metadata", {}).get("namespace"), (
            f"{path.name}: {doc.get('kind')}/{name} has no explicit namespace"
        )
    sandbox_policy = _named("AuthorizationPolicy", "deny-sandbox-egress-to-internal")
    assert sandbox_policy["metadata"]["namespace"] == "agentic-sandbox"


def test_ingress_backends_exist_in_the_same_kustomize_base() -> None:
    service_names = {doc["metadata"]["name"] for doc in _by_kind("Service")}
    ingress = _named("Ingress", "agentic-platform-ingress")
    for rule in ingress["spec"]["rules"]:
        for path in rule["http"]["paths"]:
            backend = path["backend"]["service"]["name"]
            assert backend in service_names, f"cross-namespace/missing backend: {backend}"


# ---------------------------------------------------------------------------
# 4. Network policies
# ---------------------------------------------------------------------------
def test_pgbouncer_has_ingress_and_postgres_egress_allow_rules() -> None:
    policies = [
        doc for doc in _by_kind("NetworkPolicy")
        if doc["spec"].get("podSelector", {}).get("matchLabels", {}).get("app") == "pgbouncer"
    ]
    assert policies, "default-deny-all would blackhole pgBouncer"
    policy = policies[0]
    assert policy["spec"]["podSelector"]["matchLabels"]["tier"] == "database-pooler"
    assert policy["spec"]["ingress"]
    assert any(5432 in [p.get("port") for p in rule.get("ports", [])]
               for rule in policy["spec"]["ingress"])
    assert policy["spec"]["egress"]
    assert any(5432 in [p.get("port") for p in rule.get("ports", [])]
               for rule in policy["spec"]["egress"])

    deployment = _named("Deployment", "pgbouncer")
    pod_labels = deployment["spec"]["template"]["metadata"]["labels"]
    assert pod_labels["tier"] == "database-pooler"
    service = _named("Service", "pgbouncer-svc")
    assert service["spec"]["ports"][0]["port"] == 6432
    assert service["spec"]["ports"][0]["targetPort"] == 5432


def test_tool_egress_is_agent_specific_and_has_scoped_destinations() -> None:
    policy = _named("NetworkPolicy", "allow-agentic-api")
    egress = policy["spec"]["egress"]
    api_ports = {p.get("port") for rule in egress for p in rule.get("ports", [])}
    assert not {9001, 9002, 9003}.intersection(api_ports)

    expected = {
        "allow-chat-to-web-search": {9003},
        "allow-coding-to-approved-tools": {9001, 9002},
    }
    for name, expected_ports in expected.items():
        tool_policy = _named("NetworkPolicy", name)
        actual_ports = {
            p.get("port")
            for rule in tool_policy["spec"]["egress"]
            for p in rule.get("ports", [])
        }
        assert actual_ports == expected_ports
        for rule in tool_policy["spec"]["egress"]:
            assert "to" in rule

    for rule in egress:
        assert "to" in rule, "egress rules must constrain destinations, not just ports"


def test_db_backup_and_otel_egress_policies_present() -> None:
    names = {doc["metadata"]["name"] for doc in _by_kind("NetworkPolicy")}
    assert "allow-otel-collector" in names
    assert "allow-db-backup" in names
    backup = _named("NetworkPolicy", "allow-db-backup")
    assert backup["spec"]["podSelector"]["matchLabels"]["app"] == "agentic-db-backup"
    assert any(5432 in {p.get("port") for p in r.get("ports", [])}
               for r in backup["spec"]["egress"])
    # The backup workload referenced by Kustomize must actually exist.
    assert (K8S / "backup.yaml").exists()


def test_checkpoint_retention_is_scheduled_and_network_scoped() -> None:
    cronjob = _named("CronJob", "agentic-checkpoint-retention")
    container = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"][
        "containers"
    ][0]
    assert container["args"] == ["--max-idle-seconds", "2592000"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["resources"]["limits"]

    policy = _named("NetworkPolicy", "allow-checkpoint-retention")
    assert policy["spec"]["podSelector"]["matchLabels"]["app"] == (
        "agentic-checkpoint-retention"
    )
    ports = {
        port["port"]
        for rule in policy["spec"]["egress"]
        for port in rule.get("ports", [])
    }
    assert ports == {5432}

    kust = yaml.safe_load((K8S / "kustomization.yaml").read_text(encoding="utf-8"))
    assert "checkpoint-retention.yaml" in set(kust["resources"])


# ---------------------------------------------------------------------------
# 5. OTel collector and monitoring are actually deployed
# ---------------------------------------------------------------------------
def test_otel_collector_service_and_deployment_present() -> None:
    service = _named("Service", "otel-collector-svc")
    assert 4317 in {port["port"] for port in service["spec"]["ports"]}
    deployment = _named("Deployment", "otel-collector")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert 4317 in {p["containerPort"] for p in container["ports"]}
    assert container["resources"]["limits"]


def test_otel_collector_runs_non_root_with_dropped_capabilities() -> None:
    deployment = _named("Deployment", "otel-collector")
    assert deployment["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"] is True
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert "ALL" in container["securityContext"]["capabilities"]["drop"]


def test_monitoring_manifests_included_in_kustomization() -> None:
    kust = yaml.safe_load((K8S / "kustomization.yaml").read_text(encoding="utf-8"))
    resources = set(kust["resources"])
    assert "otel-collector.yaml" in resources
    assert "monitoring.yaml" in resources
    kinds = {doc["kind"] for doc in _load_all(K8S / "monitoring.yaml")}
    assert {"ServiceMonitor", "PrometheusRule"}.issubset(kinds)


# ---------------------------------------------------------------------------
# 6. Alert expression must reference a metric that can exist
# ---------------------------------------------------------------------------
def test_pending_hitl_alert_uses_gauge_not_nonexistent_counter() -> None:
    alerts = yaml.safe_load(
        (ROOT / "deploy" / "monitoring" / "alerts.yaml").read_text(encoding="utf-8")
    )
    rules = [rule for group in alerts["groups"] for rule in group["rules"]]
    queue = next(r for r in rules if r["alert"] == "HighPendingApprovalsQueue")
    assert 'agent_hitl_approvals_total{decision="pending"}' not in queue["expr"]
    assert "agent_hitl_pending_approvals > 10" in queue["expr"]
    assert queue["for"] == "10m"
    assert any(
        "absent(agent_hitl_pending_approvals)" in r["expr"] and r["alert"] != queue["alert"]
        for r in rules
    )


# ---------------------------------------------------------------------------
# 7. Production configuration for fail-closed startup
# ---------------------------------------------------------------------------
def test_approval_keys_and_runtime_credentials_are_workload_scoped() -> None:
    external = (K8S / "external-secret.yaml").read_text(encoding="utf-8")
    for key in (
        "APPROVAL_PRIVATE_KEY",
        "APPROVAL_PUBLIC_KEY",
        "NEO4J_PASSWORD",
        "LITELLM_API_KEY",
        "POSTGRES_URI",
    ):
        assert f"secretKey: {key}" in external
    assert "property: approval_ed25519_private_key" in external
    assert "property: approval_ed25519_public_key" in external
    assert "name: agentic-secrets" not in external
    configmap = (K8S / "configmap-secrets.yaml").read_text(encoding="utf-8")
    assert "APPROVAL_PRIVATE_KEY" not in configmap
    assert "APPROVAL_PUBLIC_KEY" not in configmap
    assert "NEO4J_PASSWORD" not in configmap
    assert "LITELLM_API_KEY" not in configmap

    api = _named("Deployment", "agentic-api")
    api_container = api["spec"]["template"]["spec"]["containers"][0]
    assert api_container["envFrom"][1]["secretRef"]["name"] == "agentic-api-secrets"
    agent = _named("Deployment", "agentic-coding-agent")
    agent_container = agent["spec"]["template"]["spec"]["containers"][0]
    assert agent_container["envFrom"][1]["secretRef"]["name"] == "agentic-coding-secrets"
    mcp = _named("Deployment", "agentic-mcp-code-exec")
    mcp_container = mcp["spec"]["template"]["spec"]["containers"][0]
    assert mcp_container["envFrom"][1]["secretRef"]["name"] == "agentic-mcp-secrets"

    external_docs = _load_all(K8S / "external-secret.yaml")
    targets = {
        doc["spec"]["target"]["name"]: {
            item["secretKey"] for item in doc["spec"].get("data", [])
        }
        for doc in external_docs
        if doc["kind"] == "ExternalSecret"
    }
    assert "APPROVAL_PRIVATE_KEY" in targets["agentic-api-secrets"]
    assert "APPROVAL_PRIVATE_KEY" not in targets["agentic-chat-secrets"]
    assert "APPROVAL_PRIVATE_KEY" not in targets["agentic-rag-secrets"]
    assert "APPROVAL_PRIVATE_KEY" not in targets["agentic-coding-secrets"]
    assert "APPROVAL_PRIVATE_KEY" not in targets["agentic-mcp-secrets"]
    assert "QDRANT_API_KEY" not in targets["agentic-chat-secrets"]
    assert "QDRANT_API_KEY" in targets["agentic-rag-secrets"]
    assert "QDRANT_API_KEY" not in targets["agentic-coding-secrets"]
    assert "APPROVAL_PUBLIC_KEY" in targets["agentic-chat-secrets"]
    assert "APPROVAL_PUBLIC_KEY" in targets["agentic-rag-secrets"]
    assert "APPROVAL_PUBLIC_KEY" in targets["agentic-coding-secrets"]
    assert "APPROVAL_PUBLIC_KEY" in targets["agentic-mcp-secrets"]


def test_configmap_disables_kg_and_sets_narrow_trusted_proxies() -> None:
    config = yaml.safe_load((K8S / "configmap-secrets.yaml").read_text(encoding="utf-8"))
    data = config["data"]
    assert data["REQUIRE_AUTHENTICATION"] == "true"
    assert data["ENABLE_KNOWLEDGE_GRAPH"] == "false"
    proxies = yaml.safe_load(data["TRUSTED_PROXIES"])
    assert isinstance(proxies, list) and proxies
    assert "*" not in proxies
    assert all("/" in cidr for cidr in proxies), "trusted proxies must be CIDRs"


# ---------------------------------------------------------------------------
# 8. Dockerfiles are pinned, frozen and healthchecked
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["api", "agent", "mcp"])
def test_dockerfiles_are_pinned_frozen_and_healthchecked(name: str) -> None:
    text = (DOCKER / f"Dockerfile.{name}").read_text(encoding="utf-8")
    directives = [
        line for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    body = "\n".join(directives)
    assert not re.search(r"^FROM python:3\.12-slim\b", body, re.MULTILINE)
    assert "ghcr.io/astral-sh/uv:latest" not in body
    assert not re.search(r"^RUN .*apt-get upgrade", body, re.MULTILINE)
    assert "uv sync --frozen" in body
    assert "--extra neural" in body
    assert "COPY pyproject.toml uv.lock" in body
    assert "HEALTHCHECK" in body


def test_agent_and_mcp_healthchecks_match_runtime_routes() -> None:
    agent = (DOCKER / "Dockerfile.agent").read_text(encoding="utf-8")
    assert "/health" in agent
    mcp = (DOCKER / "Dockerfile.mcp").read_text(encoding="utf-8")
    assert "socket.create_connection" in mcp
    healthcheck = next(line for line in mcp.splitlines() if line.strip().startswith("CMD ["))
    assert "/health" not in healthcheck, "MCP has no HTTP /health route"


def test_dev_compose_renamed_and_auth_enabled() -> None:
    assert (DOCKER / "docker-compose.dev.yml").exists()
    assert not (DOCKER / "docker-compose.yml").exists()
    text = (DOCKER / "docker-compose.dev.yml").read_text(encoding="utf-8")
    assert "NOT FOR PRODUCTION" in text
    assert 'REQUIRE_AUTHENTICATION: "false"' not in text
    assert 'REQUIRE_AUTHENTICATION: "true"' in text


# ---------------------------------------------------------------------------
# 9. CI release gate and sign-off validation
# ---------------------------------------------------------------------------
def test_ci_invokes_strict_release_gate_without_continue_on_error() -> None:
    text = CI.read_text(encoding="utf-8")
    assert "python evals/release_gate.py" in text
    assert "release-gate:" in text
    assert "environment: production" in text
    assert "environment: production-release" not in text
    gate_block = text.split("name: Run Strict Release Gate", 1)[1].split("\n  # ---", 1)[0]
    assert not re.search(r"(?m)^\s*continue-on-error\s*:", gate_block)
    # ENVIRONMENT=test / CI=true are hard release blockers in release_blockers().
    assert not re.search(r"(?m)^\s*env:\s*$", gate_block), "release gate step must not set env"
    assert "ENVIRONMENT:" not in gate_block
    assert "CI:" not in gate_block


def test_ci_deploy_validation_installs_locked_test_runtime() -> None:
    text = CI.read_text(encoding="utf-8")
    validate_block = text.split("validate-deploy:", 1)[1].split("\n  # ---", 1)[0]
    assert "astral-sh/setup-uv@" in validate_block
    assert "uv sync --frozen --extra dev" in validate_block
    assert (
        "uv run --no-sync python -m pytest tests/unit/test_deploy_hardening_v3.py -v"
        in validate_block
    )
    assert validate_block.count("python - <<'PY'") == validate_block.count(
        "uv run --no-sync python - <<'PY'"
    ), "every deployment validation script must use the locked uv environment"


def test_sqlite_unit_tests_use_isolated_storage() -> None:
    offenders: list[str] = []
    for path in sorted((ROOT / "tests" / "unit").glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if function_name != "LongTermStore":
                continue
            keyword_names = {keyword.arg for keyword in node.keywords}
            force_sqlite = any(
                keyword.arg == "force_sqlite"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            )
            if force_sqlite and "storage_dir" not in keyword_names:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    assert not offenders, f"SQLite unit tests write tracked project storage: {offenders}"


def test_ci_signoff_validation_checks_commit_sha_and_signatures() -> None:
    text = CI.read_text(encoding="utf-8")
    for token in (
        "commit_sha", "production_ready is not True", "verdict",
        "release_tag", "required_signers", "signature", "sys.exit(1)",
    ):
        assert token in text, f"CI sign-off validation missing {token!r}"


def test_ci_publish_downloads_prr_evidence_from_the_current_workflow() -> None:
    text = CI.read_text(encoding="utf-8")
    publish_block = text.split("publish-and-deploy:", 1)[1]
    download_index = publish_block.index("name: Download PRR Evidence for This Commit")
    signoff_index = publish_block.index(
        "name: Verify Release Sign-off & PRR Attestation (fail closed)"
    )

    assert download_index < signoff_index
    assert (
        "uses: actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333"
        in publish_block
    )
    assert "name: prr-evidence-${{ github.sha }}" in publish_block
    assert "path: evidence/prr" in publish_block


def test_ci_actions_are_immutable_node24_compatible_pins() -> None:
    text = CI.read_text(encoding="utf-8")
    expected_actions = {
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1",
        "actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0 # v7.1.0",
        "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7.0.2",
        "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333 # v8.0.2",
        "astral-sh/setup-uv@1db35dd8eee11c5894872e0b383ae6c406cf738d # v10.4.0",
        "docker/setup-buildx-action@f87e5991a6d7451dcb8d9637bfbc97413f497069 # v4.4.1",
        "docker/login-action@8aa5831fb8d48d8f76594c7ddde61ae58019f550 # v4.8.0",
        "docker/build-push-action@c3c9e263c25d99ce0380d002d59b67737d91b0dc # v7.4.0",
        "aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25 # v0.36.0",
    }
    for action in expected_actions:
        assert action in text, f"CI action is not on the reviewed Node 24 pin: {action}"


def test_ci_signoff_uses_a_non_self_referential_two_commit_attestation() -> None:
    text = CI.read_text(encoding="utf-8")
    publish_block = text.split("publish-and-deploy:", 1)[1]
    checkout_block = publish_block.split("# ---- Sign-off verification", 1)[0]

    assert "fetch-depth: 2" in checkout_block
    assert "git', 'rev-parse', 'HEAD^'" in publish_block
    assert "git', 'rev-list', '--parents', '-n', '1', 'HEAD'" in publish_block
    assert "attestation commit must have exactly one parent" in publish_block
    assert "git', 'diff-tree', '--no-commit-id', '--name-only', '-r'" in publish_block
    assert "allowed_attestation_paths = {'evidence/signoff/release-board.json'}" in publish_block
    assert "board.get('commit_sha') != subject_sha" in publish_block
    assert "board.get('commit_sha') != expected_sha" not in publish_block


def test_ci_publish_and_deploy_is_tag_only_with_mandatory_signoff() -> None:
    text = CI.read_text(encoding="utf-8")
    publish_block = text.split("publish-and-deploy:", 1)[1]
    job_guard = publish_block.split("runs-on:", 1)[0]
    assert "github.ref == 'refs/heads/main'" not in job_guard
    assert "startsWith(github.ref, 'refs/tags/v')" in job_guard
    assert "needs.release-gate.result == 'success'" in job_guard

    signoff_header = publish_block.split(
        "name: Verify Release Sign-off & PRR Attestation (fail closed)", 1
    )[1].split("run: |", 1)[0]
    assert "if:" not in signoff_header, "sign-off must be mandatory for every publish job"


def test_ci_publishes_only_immutable_tags() -> None:
    text = CI.read_text(encoding="utf-8")
    assert "api:latest" not in text
    assert "agent:latest" not in text
    assert "mcp:latest" not in text
    assert "sha-${{ github.sha }}" in text


def test_all_deployment_and_workflow_yaml_parses() -> None:
    files = sorted(ROOT.glob("deploy/**/*.yaml")) + sorted(ROOT.glob(".github/workflows/*.yml"))
    assert files
    for path in files:
        docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        assert docs, f"{path} produced no YAML documents"


# ---------------------------------------------------------------------------
# 10. Dependency declarations
# ---------------------------------------------------------------------------
def test_otlp_exporter_and_deepeval_dependencies_declared() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    try:
        import tomllib
    except ImportError:  # pragma: no cover
        pytest.skip("tomllib unavailable")
    data = tomllib.loads(pyproject)
    deps = data["project"]["dependencies"]
    assert any(d.startswith("opentelemetry-exporter-otlp") for d in deps)
    extras = data["project"]["optional-dependencies"]
    assert any(d.startswith("deepeval") for d in extras.get("deepeval", []))
    assert any(d.startswith("opentelemetry-api") for d in deps)
    assert any(d.startswith("opentelemetry-sdk") for d in deps)
