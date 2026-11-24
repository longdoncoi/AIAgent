"""Application configuration using Pydantic Settings."""

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "staging", "production"]


class Settings(BaseSettings):
    """Centralized environment and configuration settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Base Paths
    project_root: Path = Field(default_factory=lambda: Path(__file__).resolve().parents[3])

    # API Configuration
    host: str = "0.0.0.0"
    port: int = 8000
    environment: Environment = "development"
    log_level: str = "INFO"
    debug: bool = True
    allowed_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:3000",
            "http://localhost:8000",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:8000",
        ]
    )

    # Model Serving & Gateway
    litellm_base_url: str = "http://localhost:4000/v1"
    litellm_api_key: str = "sk-dummy-key"
    default_reasoning_model: str = "reasoning-llm"
    default_fast_model: str = "fast-llm"
    default_coding_model: str = "coding-llm"
    default_embedding_model: str = "text-embedding-bge"
    default_embedding_dim: int = 1024

    # Direct Inference Endpoints (fallback / standalone)
    vllm_base_url: str = "http://localhost:8001/v1"
    llama_chat_base_url: str = "http://localhost:8080/v1"
    llama_embed_base_url: str = "http://localhost:8081/v1"

    # A2A Sub-Agents Endpoints & Resilience
    a2a_chat_agent_url: str = "http://localhost:9100/a2a"
    a2a_rag_agent_url: str = "http://localhost:9101/a2a"
    a2a_coding_agent_url: str = "http://localhost:9102/a2a"
    a2a_max_retries: int = 3
    a2a_retry_backoff_base_seconds: float = 0.5
    a2a_retry_backoff_max_seconds: float = 4.0
    a2a_max_concurrent_requests: int = 100

    # MCP Tool Servers
    mcp_code_exec_url: str = "http://localhost:9001/sse"
    mcp_repo_url: str = "http://localhost:9002/sse"
    mcp_web_search_url: str = "http://localhost:9003/sse"

    # Storage & Checkpointing
    postgres_uri: str = "postgresql://postgres:postgres@localhost:5432/agentic_db"
    use_postgres_checkpoint: bool = False  # Set true if postgres is active; fallback to MemorySaver
    use_redis_checkpoint: bool = False  # Set true if Redis checkpointer is active
    redis_checkpoint_ttl_seconds: int = 86400 * 7  # 7 days
    checkpoint_hmac_secret: str | None = None
    allow_legacy_unsigned_checkpoints: bool = False
    qdrant_host: str = "localhost"
    qdrant_port: int = 6333
    qdrant_api_key: str | None = None
    enable_qdrant: bool = False
    qdrant_collection_name: str = "agentic_knowledge"
    qdrant_shard_number: int = 1
    qdrant_replication_factor: int = 1
    qdrant_write_consistency_factor: int = 1

    # Knowledge Graph (Neo4j / NetworkX)
    enable_knowledge_graph: bool = True
    neo4j_url: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "agentic_neo4j_password"

    # Temporal Orchestration
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "agentic-tasks"

    # Authentication & Security
    jwt_secret_key: str = "agentic-secret-key-change-in-production-2026"
    jwt_algorithm: str = "HS256"
    require_authentication: bool = False
    enable_docker_sandbox: bool = True
    s2s_internal_token: str = "agentic-internal-s2s-token-2026"
    # Dedicated HITL signing key, never derived from the shared S2S credential.
    approval_signing_key: str | None = None
    approval_grant_ttl_seconds: int = 60
    jwt_audience: str = "agentic-api"
    jwt_issuer: str = "agentic-platform"
    # CIDRs of trusted reverse proxies. X-Forwarded-For is ignored entirely
    # unless the direct peer is in this list.
    trusted_proxies: list[str] = Field(default_factory=list)
    enable_mtls: bool = False
    mtls_cert_path: str | None = None
    mtls_key_path: str | None = None
    mtls_ca_cert_path: str | None = None
    rate_limit_per_minute: int = 120
    token_rate_limit_per_minute: int = 100_000
    enable_token_rate_limit: bool = True
    redis_url: str = "redis://localhost:6379/0"
    enable_redis_rate_limit: bool = False
    enable_redis_cache: bool = False
    enable_k8s_sandbox: bool = False
    allow_host_code_execution: bool = False
    allow_local_agent_fallback: bool = False
    use_remote_mcp: bool = True
    k8s_sandbox_namespace: str = "agentic-sandbox"
    k8s_sandbox_runtime_class: str = "gvisor"

    # Caching Architecture (RAG, Prompt, KV Prefix)
    enable_rag_cache: bool = True
    rag_cache_ttl_seconds: int = 3600
    enable_prompt_cache: bool = True
    prompt_cache_ttl_seconds: int = 3600
    enable_kv_cache_prefix_optimization: bool = True
    cache_max_entries: int = 2048
    enable_semantic_cache: bool = True
    semantic_cache_default_threshold: float = 0.92
    # Hybrid retrieval fusion weights (vector / lexical / knowledge graph)
    rag_vector_weight: float = 0.40
    rag_lexical_weight: float = 0.30
    rag_kg_weight: float = 0.30
    # Hard ceiling for generation; also used by the token quota estimator.
    max_output_tokens: int = 4096
    # Embedding service is a required dependency in production.
    require_embedding_service: bool = False
    require_neural_reranker: bool = False
    rag_default_tenant_id: str = "public"
    semantic_cache_domain_thresholds: dict[str, float] = {
        "legal": 0.96,
        "finance": 0.96,
        "compliance": 0.96,
        "security": 0.97,
        "medical": 0.97,
        "coding": 0.94,
        "technical": 0.93,
        "general": 0.90,
        "chat": 0.88,
    }

    # Safety & Guardrails
    enable_guardrails: bool = True
    pii_redaction_enabled: bool = True
    stream_hold_chars: int = (
        48  # Sliding window chars to prevent sliced PII/secrets across SSE chunks
    )
    max_execution_timeout_seconds: int = 60
    require_model_gateway_ready: bool = False

    # DeepEval & Advanced Quality Benchmarking
    eval_judge_model: str = "reasoning-llm"
    eval_groundedness_threshold: float = 0.85
    eval_faithfulness_threshold: float = 0.85
    eval_routing_threshold: float = 0.95

    # RAGAS Framework Quality Gate Thresholds
    ragas_faithfulness_threshold: float = 0.80
    ragas_answer_relevance_threshold: float = 0.80
    ragas_context_precision_threshold: float = 0.75
    ragas_context_recall_threshold: float = 0.75

    # Observability & LangSmith
    enable_tracing: bool = True
    langchain_tracing_v2: bool = False
    langchain_api_key: str | None = None
    langchain_project: str = "agentic-platform"
    langchain_endpoint: str = "https://api.smith.langchain.com"
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"
    enable_prometheus: bool = True
    prometheus_port: int = 9090

    @field_validator("environment", mode="before")
    @classmethod
    def normalize_environment(cls, value: Any) -> Any:
        """Normalize the environment label so a typo cannot disable prod guards."""
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @property
    def is_production(self) -> bool:
        """Single authoritative production flag derived from a validated label."""
        return self.environment == "production"

    @property
    def policies_path(self) -> Path:
        return self.project_root / "configs" / "policies.yaml"

    @property
    def litellm_config_path(self) -> Path:
        return self.project_root / "configs" / "litellm_config.yaml"

    @property
    def prompts_dir(self) -> Path:
        return self.project_root / "configs" / "prompts"

    @property
    def checkpoint_signing_key(self) -> str:
        """Return a dedicated domain-separated key for checkpoint signatures."""
        if self.checkpoint_hmac_secret:
            return self.checkpoint_hmac_secret
        material = f"agentic-chkpt-isolated\x00{self.s2s_internal_token}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @property
    def approval_key(self) -> str:
        """Dedicated HITL signing material; never derived from a transport token."""
        if self.approval_signing_key:
            return self.approval_signing_key
        material = f"agentic-approval-dev-only\x00{self.jwt_secret_key}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @model_validator(mode="after")
    def validate_production_inviolables(self) -> "Settings":
        """Enforces zero-trust production security invariants."""
        # Fail-closed: Never auto-enable host code execution even in development
        if self.is_production:
            self.allow_host_code_execution = False
            self.allow_local_agent_fallback = False
            self.debug = False
            self.enable_redis_rate_limit = True
            self.enable_redis_cache = True
            self.enable_tracing = True
            self.require_embedding_service = True
            self.require_neural_reranker = True
            self.require_model_gateway_ready = True
            insecure_keys = [
                "agentic-secret-key-change-in-production-2026",
                "secret",
                "changeme",
                "default",
            ]
            if self.jwt_secret_key in insecure_keys:
                raise ValueError(
                    "Production Inviolable Violated: jwt_secret_key cannot be the default insecure placeholder in production."
                )
            insecure_s2s = [
                "agentic-internal-s2s-token-2026",
                "secret",
                "changeme",
                "default",
            ]
            if self.s2s_internal_token in insecure_s2s:
                raise ValueError(
                    "Production Inviolable Violated: s2s_internal_token cannot be the default insecure placeholder in production."
                )
            if not self.require_authentication:
                raise ValueError(
                    "Production Inviolable Violated: require_authentication must be True in production environment."
                )
            if not self.use_postgres_checkpoint and not self.use_redis_checkpoint:
                raise ValueError(
                    "Production Inviolable Violated: use_postgres_checkpoint must be True (or use_redis_checkpoint must be True) in production to prevent state loss across replicas."
                )
            if self.allow_legacy_unsigned_checkpoints:
                raise ValueError(
                    "Production Inviolable Violated: unsigned legacy checkpoints are forbidden."
                )
            if self.allow_host_code_execution:
                raise ValueError(
                    "Production Inviolable Violated: host code execution is forbidden."
                )
            if self.allow_local_agent_fallback:
                raise ValueError(
                    "Production Inviolable Violated: local A2A fallback must be disabled."
                )
            if not self.use_remote_mcp:
                raise ValueError(
                    "Production Inviolable Violated: MCP tools must use remote protocol servers."
                )
            if "*" in self.allowed_origins:
                raise ValueError(
                    "Production Inviolable Violated: allowed_origins cannot contain wildcard '*' when allow_credentials is True in production."
                )
            if not self.approval_signing_key:
                raise ValueError(
                    "Production Inviolable Violated: approval_signing_key must be explicitly configured in production."
                )
            placeholder_secrets = {
                "litellm_api_key": (self.litellm_api_key, {"sk-dummy-key"}),
                "neo4j_password": (self.neo4j_password, {"agentic_neo4j_password"}),
                "postgres_uri": (
                    self.postgres_uri,
                    {"postgresql://postgres:postgres@localhost:5432/agentic_db"},
                ),
            }
            for name, (value, placeholders) in placeholder_secrets.items():
                if value in placeholders:
                    raise ValueError(
                        f"Production Inviolable Violated: {name} uses an insecure placeholder in production."
                    )

            # Auto-resolve internal Kubernetes cluster DNS service names if unchanged from default localhost
            if self.a2a_chat_agent_url == "http://localhost:9100/a2a":
                self.a2a_chat_agent_url = "http://agentic-chat-svc:9100/a2a"
            if self.a2a_rag_agent_url == "http://localhost:9101/a2a":
                self.a2a_rag_agent_url = "http://agentic-rag-svc:9101/a2a"
            if self.a2a_coding_agent_url == "http://localhost:9102/a2a":
                self.a2a_coding_agent_url = "http://agentic-coding-svc:9102/a2a"
            if self.mcp_code_exec_url == "http://localhost:9001/sse":
                self.mcp_code_exec_url = "http://agentic-mcp-code-exec-svc:9001/sse"
            if self.mcp_repo_url == "http://localhost:9002/sse":
                self.mcp_repo_url = "http://agentic-mcp-repo-svc:9002/sse"
            if self.mcp_web_search_url == "http://localhost:9003/sse":
                self.mcp_web_search_url = "http://agentic-mcp-web-search-svc:9003/sse"
            if self.enable_qdrant and self.qdrant_host == "localhost":
                self.qdrant_host = "qdrant-svc"
        return self


@lru_cache
def get_settings() -> Settings:
    """Return a cached singleton instance of Settings."""
    return Settings()
