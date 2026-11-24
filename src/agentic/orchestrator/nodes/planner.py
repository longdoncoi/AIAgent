"""Planner Node decomposing queries into discrete execution steps."""

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from src.agentic.cache.kv_cache import optimize_messages_for_kv_cache
from src.agentic.core.config import get_settings
from src.agentic.llm.gateway import get_llm
from src.agentic.memory.short_term import trim_messages_window
from src.agentic.observability.metrics import NODE_DURATION
from src.agentic.observability.tracing import traced_operation
from src.agentic.orchestrator.state import AgentState, Plan, Step
from src.agentic.safety.policy import get_policy_manager

logger = logging.getLogger(__name__)

_planner_prompt_cache: dict[Path, tuple[int, str]] = {}
_planner_prompt_cache_lock = threading.Lock()

# Single source of truth for mutating verbs used by both the heuristic planner
# and the deterministic risk escalation applied to model-generated plans.
MUTATING_KEYWORDS: tuple[str, ...] = (
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
)


def _contains_keyword(text: str, keywords: list[str] | tuple[str, ...]) -> bool:
    """Checks if any keyword matches as a distinct word in text, avoiding partial substring false positives."""
    text_lower = text.lower()
    for kw in keywords:
        kw_lower = kw.lower()
        pattern = r"(?<!\w)" + re.escape(kw_lower) + r"(?!\w)"
        if re.search(pattern, text_lower, re.UNICODE):
            return True
    return False


def _apply_risk_policy(plan: Plan) -> Plan:
    """Escalate risk deterministically; never trust a model-provided low-risk label.

    A step assigned to an agent that can invoke an approval-gated tool is also
    high-risk, so it is forced through HITL before any tool is granted.
    """
    policy = get_policy_manager()
    for step in plan.steps:
        coding_action = step.agent == "coding" and any(
            action in step.description.lower()
            for action in ("write", "modify", "execute", "run", "delete")
        )
        if (
            _contains_keyword(step.input_query, MUTATING_KEYWORDS)
            or coding_action
            or policy.agent_requires_approval(step.agent)
        ):
            step.risk_level = "high"
    return plan


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
    steps: list[Step] = []

    # Check for RAG / policy keywords with word boundaries
    is_rag = _contains_keyword(
        query,
        [
            "policy",
            "quy định",
            "chính sách",
            "tài liệu",
            "rule",
            "internal",
            "documentation",
        ],
    )
    # Check for active coding action keywords
    is_coding_action = _contains_keyword(
        query,
        [
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
        ],
    )
    # Check for coding keywords with word boundaries
    is_coding = _contains_keyword(
        query,
        [
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
        ],
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
        # Determine risk using word boundaries (shared mutating-verb list).
        is_write = _contains_keyword(query, MUTATING_KEYWORDS)
        risk: Literal["low", "medium", "high"] = "high" if is_write else "low"
        steps.append(
            Step(
                id="step_1",
                description=f"Analyze and execute coding task: {query[:60]}",
                agent="coding",
                input_query=query,
                risk_level=risk,
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

    return _apply_risk_policy(Plan(goal=query, steps=steps))


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

        if os.environ.get("FAST_HEURISTIC_PLANNER") == "true":
            fallback_plan = _heuristic_planner(user_query)
            heuristic_res: dict[str, Any] = {
                "plan": fallback_plan,
                "current_step_index": 0,
                "messages": trimmed_msgs,
                "degraded": False,
                "degraded_reason": None,
            }
            if state.reflection_notes:
                heuristic_res["approved_step_ids"] = []
                heuristic_res["approved_step_fingerprints"] = {}
                heuristic_res["approval_granted"] = None
            return heuristic_res

        try:
            llm = get_llm(alias="reasoning-llm", temperature=0.1)
            structured_llm = llm.with_structured_output(Plan)
            # Inject the system prompt at the caller. KV-cache optimization may
            # be disabled, in which case the optimizer passes messages through
            # untouched — the system message must still be present.
            raw_messages: list[BaseMessage] = []
            if system_prompt:
                raw_messages.append(SystemMessage(content=system_prompt.strip()))
            raw_messages.append(
                HumanMessage(
                    content=f"User Goal: {user_query}\nCreate a structured execution plan."
                )
            )
            if state.reflection_notes:
                raw_messages.append(
                    HumanMessage(
                        content=f"Previous reflection feedback: {state.reflection_notes[-1]}. Please revise the plan."
                    )
                )

            messages = optimize_messages_for_kv_cache(raw_messages, system_prompt=system_prompt)

            plan = await structured_llm.ainvoke(messages)
            if isinstance(plan, Plan) and plan.steps:
                _apply_risk_policy(plan)

                res: dict[str, Any] = {
                    "plan": plan,
                    "current_step_index": 0,
                    "messages": trimmed_msgs,
                    "degraded": False,
                    "degraded_reason": None,
                }
                if state.reflection_notes:
                    # Invalidate previous approvals when replanning to prevent privilege escalation
                    res["approved_step_ids"] = []
                    res["approved_step_fingerprints"] = {}
                    res["approval_granted"] = None
                return res
        except Exception as e:
            logger.error(f"Structured planning failed ({e}). Using heuristic planner fallback.")

        fallback_plan = _heuristic_planner(user_query)
        fallback_res: dict[str, Any] = {
            "plan": fallback_plan,
            "current_step_index": 0,
            "messages": trimmed_msgs,
            "degraded": True,
            "degraded_reason": "planner_model_failure_heuristic_fallback",
        }
        if state.reflection_notes:
            fallback_res["approved_step_ids"] = []
            fallback_res["approved_step_fingerprints"] = {}
            fallback_res["approval_granted"] = None
        return fallback_res
    finally:
        NODE_DURATION.labels(node_name="planner").observe(time.time() - start_t)
