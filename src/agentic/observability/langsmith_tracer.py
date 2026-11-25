"""LangSmith Observability, Distributed Tracing, Dataset Management, and Run Feedback."""

import functools
import logging
import os
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Literal, cast

from src.agentic.cache.redaction import redact_pii
from src.agentic.core.config import Settings, get_settings

RunType = Literal["tool", "chain", "llm", "retriever", "embedding", "prompt", "parser"]

logger = logging.getLogger(__name__)

_langsmith_client: Any | None = None
_langsmith_initialized: bool = False


def setup_langsmith(settings: Settings | None = None) -> None:
    """Configures LangSmith environment variables and tracing parameters."""
    global _langsmith_initialized
    if settings is None:
        settings = get_settings()

    env_tracing = os.getenv("LANGCHAIN_TRACING_V2")
    if env_tracing is not None:
        tracing_enabled = env_tracing.strip().lower() in ("true", "1")
    else:
        tracing_enabled = bool(settings.langchain_tracing_v2)

    env_api_key = os.getenv("LANGCHAIN_API_KEY")
    api_key = env_api_key.strip() if env_api_key is not None else settings.langchain_api_key or ""

    if tracing_enabled and api_key:
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGCHAIN_API_KEY"] = api_key
        os.environ["LANGCHAIN_PROJECT"] = settings.langchain_project or os.getenv(
            "LANGCHAIN_PROJECT", "agentic-platform"
        )
        if settings.langchain_endpoint:
            os.environ["LANGCHAIN_ENDPOINT"] = settings.langchain_endpoint
        _langsmith_initialized = True
        logger.info(
            f"LangSmith Tracing v2 enabled for project '{os.environ['LANGCHAIN_PROJECT']}' at endpoint '{os.environ.get('LANGCHAIN_ENDPOINT', 'default')}'"
        )
    else:
        _langsmith_initialized = False
        logger.debug("LangSmith Tracing v2 is disabled or missing LANGCHAIN_API_KEY.")


def is_langsmith_enabled() -> bool:
    """Returns True if LangSmith tracing is active with valid API key configuration."""
    env_tracing = os.getenv("LANGCHAIN_TRACING_V2")
    if env_tracing is not None:
        tracing_v2 = env_tracing.strip().lower() in ("true", "1")
    else:
        tracing_v2 = bool(get_settings().langchain_tracing_v2)

    env_api_key = os.getenv("LANGCHAIN_API_KEY")
    if env_api_key is not None:
        api_key = env_api_key.strip()
    else:
        api_key = get_settings().langchain_api_key or ""

    return bool(tracing_v2 and api_key)


def get_langsmith_client() -> Any | None:
    """Returns a singleton instance of the LangSmith Client if enabled, else None."""
    global _langsmith_client
    if not is_langsmith_enabled():
        return None

    if _langsmith_client is None:
        try:
            from langsmith import Client

            settings = get_settings()
            _langsmith_client = Client(
                api_key=settings.langchain_api_key or os.getenv("LANGCHAIN_API_KEY"),
                api_url=settings.langchain_endpoint
                or os.getenv("LANGCHAIN_ENDPOINT", "https://api.smith.langchain.com"),
            )
            logger.info("Initialized LangSmith Client successfully.")
        except Exception as e:
            logger.warning(f"Failed to initialize LangSmith Client: {e}")
            _langsmith_client = None

    return _langsmith_client


def get_langsmith_callback(
    project_name: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Any | None:
    """Instantiates a LangChainTracer callback handler for explicit invocation tracing."""
    if not is_langsmith_enabled():
        return None

    try:
        from langchain_core.tracers.langchain import LangChainTracer

        settings = get_settings()
        effective_project = project_name or settings.langchain_project or "agentic-platform"
        tracer = LangChainTracer(
            project_name=effective_project,
            tags=tags or ["agentic-platform"],
        )
        return tracer
    except Exception as e:
        logger.debug(f"LangChainTracer not initialized: {e}")
        return None


def traceable_operation(
    name: str | None = None,
    run_type: RunType | str = "chain",
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Callable[..., Any]:
    """Decorator to instrument async or sync functions with LangSmith traceable tracing.

    Gracefully falls back to normal execution if LangSmith is disabled or unconfigured.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        op_name = name or func.__name__
        op_tags = list(tags) if tags else ["agentic-platform"]
        op_metadata = dict(metadata) if metadata else {}

        # If LangSmith is available, wrap with langsmith.traceable
        if is_langsmith_enabled():
            try:
                from langsmith import traceable

                wrapped_func = traceable(
                    run_type=cast(Any, run_type),
                    name=op_name,
                    tags=op_tags,
                    metadata=op_metadata,
                )(func)
                return wrapped_func
            except Exception as e:
                logger.debug(
                    f"LangSmith traceable wrapper failed for '{op_name}': {e}. Using fallback."
                )

        # Fallback wrapper
        if hasattr(func, "__await__") or asyncio_iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                return await func(*args, **kwargs)

            return async_wrapper
        else:

            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                return func(*args, **kwargs)

            return sync_wrapper

    return decorator


def asyncio_iscoroutinefunction(fn: Any) -> bool:
    """Helper to detect coroutine functions safely."""
    import inspect

    return inspect.iscoroutinefunction(fn)


_SENSITIVE_TRACE_KEYS = frozenset(
    {"token", "api_key", "secret", "password", "authorization", "approval_token", "cookie"}
)


def redact_trace_payload(value: Any, *, _depth: int = 0) -> Any:
    """Recursively scrub secrets and PII before sending payloads to external LangSmith SaaS."""
    if _depth > 6:
        return "[REDACTED_DEPTH]"
    if isinstance(value, str):
        return redact_pii(value)
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for k, v in value.items():
            key_s = str(k)
            if any(secret_k in key_s.lower() for secret_k in _SENSITIVE_TRACE_KEYS):
                cleaned[key_s] = "[REDACTED_SECRET]"
            else:
                cleaned[key_s] = redact_trace_payload(v, _depth=_depth + 1)
        return cleaned
    if isinstance(value, list):
        return [redact_trace_payload(item, _depth=_depth + 1) for item in value]
    return value


@contextmanager
def trace_langsmith_run(
    name: str,
    run_type: RunType | str = "chain",
    inputs: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Context manager for tracing custom sync code blocks with LangSmith run tree."""
    context_data: dict[str, Any] = {"name": name, "run_id": None}
    client = get_langsmith_client()
    if client and is_langsmith_enabled():
        try:
            from langsmith.run_trees import RunTree

            settings = get_settings()
            rt = cast(Any, RunTree)(
                name=name,
                run_type=cast(Any, run_type),
                inputs=redact_trace_payload(inputs or {}),
                tags=tags or ["agentic-platform"],
                extra={"metadata": redact_trace_payload(metadata or {})},
                session_name=settings.langchain_project,
                ls_client=client,
            )
            rt.post()
            context_data["run_id"] = str(rt.id)
            context_data["run_tree"] = rt
            try:
                yield context_data
                rt.end(outputs=redact_trace_payload(context_data.get("outputs", {})))
                rt.patch()
            except Exception as ex:
                rt.end(error=str(ex))
                rt.patch()
                raise
            return
        except Exception as e:
            logger.debug(f"LangSmith RunTree context failed: {e}")

    yield context_data


@asynccontextmanager
async def trace_langsmith_run_async(
    name: str,
    run_type: str = "chain",
    inputs: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Context manager for tracing custom async code blocks with LangSmith run tree."""
    with trace_langsmith_run(
        name=name, run_type=run_type, inputs=inputs, tags=tags, metadata=metadata
    ) as ctx:
        yield ctx


def log_feedback(
    run_id: str,
    key: str,
    score: float,
    value: Any = None,
    comment: str | None = None,
    correction: dict[str, Any] | None = None,
) -> bool:
    """Logs evaluation metrics or user feedback to a specific LangSmith run."""
    client = get_langsmith_client()
    if client is None or not run_id:
        return False

    try:
        client.create_feedback(
            run_id=run_id,
            key=key,
            score=score,
            value=value,
            comment=comment,
            correction=correction,
        )
        logger.info(f"Logged LangSmith feedback: run_id={run_id}, key={key}, score={score}")
        return True
    except Exception as e:
        logger.warning(f"Failed to log feedback to LangSmith ({e})")
        return False


def sync_evaluation_dataset(
    dataset_name: str,
    examples: list[dict[str, Any]],
    description: str = "Evaluation Benchmark Dataset",
) -> str | None:
    """Creates or updates a benchmark dataset in LangSmith from local examples."""
    client = get_langsmith_client()
    if client is None:
        logger.debug("LangSmith client not available for dataset sync.")
        return None

    try:
        # Check if dataset already exists
        if client.has_dataset(dataset_name=dataset_name):
            dataset = client.read_dataset(dataset_name=dataset_name)
            logger.info(f"Found existing LangSmith dataset '{dataset_name}' (id={dataset.id})")
        else:
            dataset = client.create_dataset(
                dataset_name=dataset_name,
                description=description,
            )
            logger.info(f"Created new LangSmith dataset '{dataset_name}' (id={dataset.id})")

        # Sync examples
        for item in examples:
            inputs = {
                k: v for k, v in item.items() if k in ("query", "question", "input", "instruction")
            }
            outputs = {
                k: v
                for k, v in item.items()
                if k in ("expected_agent", "ground_truth", "output", "expected_tools")
            }
            metadata = {k: v for k, v in item.items() if k not in inputs and k not in outputs}
            client.create_example(
                inputs=redact_trace_payload(inputs),
                outputs=redact_trace_payload(outputs),
                metadata=redact_trace_payload(metadata),
                dataset_id=dataset.id,
            )

        return str(dataset.id)
    except Exception as e:
        logger.warning(f"Failed to sync dataset '{dataset_name}' to LangSmith: {e}")
        return None
