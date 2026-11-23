"""Planner Node decomposing queries into discrete execution steps."""

import logging
import threading
import time
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import HumanMessage

from src.agentic.cache.kv_cache import optimize_messages_for_kv_cache
from src.agentic.core.config import get_settings
from src.agentic.llm.gateway import get_llm
from src.agentic.memory.short_term import trim_messages_window
from src.agentic.observability.metrics import NODE_DURATION
from src.agentic.observability.tracing import traced_operation
from src.agentic.orchestrator.state import AgentState, Plan, Step
from src.agentic.safety.policy import get_policy_manager  # compatibility hook for tests

logger = logging.getLogger(__name__)

_planner_prompt_cache: dict[Path, tuple[int, str]] = {}
_planner_prompt_cache_lock = threading.Lock()


def _infer_approval_tools(query: str) -> list[str]:
    """Map explicit mutating intent to the narrowest approval-gated tool scope."""
    query_lower = query.lower()
    tools: list[str] = []
    if any(word in query_lower for word in ("run", "execute", "bash", "shell", "chạy")):
        tools.append("code_exec_bash")
    if any(
        word in query_lower
        for word in (
            "write",
            "create",
            "delete",
            "remove",
            "overwrite",
            "commit",
            "patch",
            "ghi",
            "xóa",
            "sửa",
            "tạo",
        )
    ):
        tools.append("repo_write_file")
    return tools


def load_cached_planner_prompt(prompt_path: Path) -> str:
    """Reads prompt template from markdown file with thread-safe mtime caching."""
    if not prompt_path.exists():
        return ""
    try:
        mtime = prompt_path.stat().st_mtime_ns
        with _planner_prompt_cache_lock:
            if prompt_path in _planner_prompt_cache:
                cached_mtime, content = _planner_prompt_cache[prompt_path]
                if cached_mtime == mtime:
                    return content
        with open(prompt_path, "r", encoding="utf-8") as f:
            content = f.read()
        with _planner_prompt_cache_lock:
            _planner_prompt_cache[prompt_path] = (mtime, content)
        return content
    except Exception as e:
        logger.debug("Could not read prompt from %s: %s", prompt_path, e)
        return ""


def _heuristic_planner(query: str) -> Plan:
    """Fallback heuristic planner for reliable deterministic planning."""
    query_lower = query.lower()
    steps: list[Step] = []

    # Check for RAG / policy keywords
    is_rag = any(
        k in query_lower
        for k in [
            "policy",
            "quy định",
            "chính sách",
            "tài liệu",
            "rule",
            "internal",
            "documentation",
        ]
    )
    # Check for active coding action keywords
    is_coding_action = any(
        k in query_lower
        for k in [
            "viết",
            "sửa",
            "chạy",
            "tạo",
            "write",
            "run",
            "debug",
            "refactor",
            "execute",
            "build",
        ]
    )
    # Check for coding keywords
    is_coding = any(
        k in query_lower
        for k in [
            "code",
            "codebase",
            "symbol",
            "python",
            "script",
            "test",
            "repo",
            "file",
            "bug",
            "directory",
            "folder",
        ]
    )

    if is_rag and not is_coding_action:
        steps.append(
            Step(
                id="step_1",
                description=f"Retrieve internal policy knowledge: {query[:60]}",
                agent="rag",
                input_query=query,
                risk_level="low",
            )
        )
    elif is_coding or is_coding_action:
        # Determine risk
        is_write = any(
            k in query_lower
            for k in [
                "write",
                "create",
                "delete",
                "remove",
                "overwrite",
                "commit",
                "patch",
                "execute",
                "run",
                "bash",
                "shell",
                "ghi",
                "xóa",
                "sửa",
                "tạo",
                "chạy",
            ]
        )
        risk: Literal["low", "medium", "high"] = "high" if is_write else "low"
        steps.append(
            Step(
                id="step_1",
                description=f"Analyze and execute coding task: {query[:60]}",
                agent="coding",
                input_query=query,
                risk_level=risk,
                approval_tools=_infer_approval_tools(query) if risk == "high" else [],
            )
        )
    else:
        steps.append(
            Step(
                id="step_1",
                description=f"Handle conversational query: {query[:60]}",
                agent="chat",
                input_query=query,
                risk_level="low",
            )
        )

    return Plan(goal=query, steps=steps)


@traced_operation("orchestrator.planner")
async def planner_node(state: AgentState) -> dict[str, Any]:
    """Generates execution plan from the initial user messages."""
    start_t = time.time()
    try:
        # Context window trimming
        trimmed_msgs = trim_messages_window(state.messages)

        # If plan already exists and has steps, do not overwrite unless replanning
        if state.plan and state.plan.steps and not state.reflection_notes:
            return {"plan": state.plan, "messages": trimmed_msgs}

        # Extract user query
        user_query = ""
        for msg in reversed(trimmed_msgs):
            if isinstance(msg, HumanMessage):
                user_query = str(msg.content)
                break

        if not user_query:
            user_query = "Process request"

        settings = get_settings()
        prompt_file = settings.prompts_dir / "planner.md"
        system_prompt = load_cached_planner_prompt(prompt_file)

        try:
            llm = get_llm(alias="reasoning-llm", temperature=0.1)
            structured_llm = llm.with_structured_output(Plan)
            raw_messages = [
                HumanMessage(
                    content=f"User Goal: {user_query}\nCreate a structured execution plan."
                )
            ]
            if state.reflection_notes:
                raw_messages.append(
                    HumanMessage(
                        content=f"Previous reflection feedback: {state.reflection_notes[-1]}. Please revise the plan."
                    )
                )

            messages = optimize_messages_for_kv_cache(raw_messages, system_prompt=system_prompt)

            plan = await structured_llm.ainvoke(messages)
            if isinstance(plan, Plan) and plan.steps:
                try:
                    policy_manager = get_policy_manager()
                except Exception as policy_error:
                    logger.warning("Policy unavailable during planning: %s", policy_error)
                    policy_manager = None
                # Never trust a model-provided low-risk label for mutating actions.
                for s in plan.steps:
                    s.approval_tools = _infer_approval_tools(s.input_query)
                    if any(
                        w in s.input_query.lower()
                        for w in [
                            "write",
                            "create",
                            "delete",
                            "remove",
                            "commit",
                            "patch",
                            "execute",
                            "run",
                            "bash",
                            "shell",
                            "ghi",
                            "xóa",
                            "sửa",
                            "tạo",
                            "chạy",
                        ]
                    ):
                        s.risk_level = "high"
                    elif policy_manager and policy_manager.agent_requires_approval(s.agent):
                        s.risk_level = "high"
                return {"plan": plan, "current_step_index": 0, "messages": trimmed_msgs}
        except Exception as e:
            logger.warning(f"Structured planning failed ({e}). Using heuristic planner fallback.")

        fallback_plan = _heuristic_planner(user_query)
        return {"plan": fallback_plan, "current_step_index": 0, "messages": trimmed_msgs}
    finally:
        NODE_DURATION.labels(node_name="planner").observe(time.time() - start_t)
