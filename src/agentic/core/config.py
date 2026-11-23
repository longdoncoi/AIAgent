"""Application configuration using Pydantic Settings."""

import hashlib
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    environment: str = "development"
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

    # Direct Inference Endpoints (fallback / standalone)
    vllm_base_url: str = "http://localhost:8001/v1"
    llama_chat_base_url: str = "http://localhost:8080/v1"
    llama_embed_base_url: str = "http://localhost:8081/v1"

    # A2A Sub-Agents Endpoints
    a2a_chat_agent_url: str = "http://localhost:9100/a2a"
    a2a_rag_agent_url: str = "http://localhost:9101/a2a"
    a2a_coding_agent_url: str = "http://localhost:9102/a2a"

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
        """Return a domain-separated key without requiring another plaintext secret."""
        if self.checkpoint_hmac_secret:
            return self.checkpoint_hmac_secret
        material = f"agentic-checkpoint\x00{self.jwt_secret_key}\x00{self.s2s_internal_token}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @model_validator(mode="after")
    def validate_production_inviolables(self) -> "Settings":
        """Enforces zero-trust production security invariants."""
        if self.environment != "production":
            if "allow_host_code_execution" not in self.model_fields_set:
                self.allow_host_code_execution = True
            if "allow_local_agent_fallback" not in self.model_fields_set:
                self.allow_local_agent_fallback = True
            if "use_remote_mcp" not in self.model_fields_set:
                self.use_remote_mcp = False
        if self.environment == "production":
            if "require_model_gateway_ready" not in self.model_fields_set:
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
