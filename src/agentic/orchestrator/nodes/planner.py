"""Planner Node decomposing queries into discrete execution steps."""

import asyncio
import hashlib
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
    """Escalate risk deterministically based on mutating actions / approval-gated tools.

    Avoids blanket-flagging conversational/RAG queries (e.g. "How do I create a
    ticket?") or read-only coding steps (e.g. ``repo_list_files`` or
    ``repo_read_file``) as high-risk while still enforcing HITL whenever a step
    targets an agent with mutating tools or explicitly names approval-gated
    tools (``code_exec_bash``, ``repo_write_file``, ``code_exec_python``).
    Tool-boundary Ed25519 grants independently prevent unapproved tool calls.
    """
    policy = get_policy_manager()
    gated_tools = tuple(
        name
        for name in policy.policy_data.get("tools", {})
        if policy.is_approval_required(name)
    )
    for step in plan.steps:
        desc_lower = step.description.lower()
        query_lower = step.input_query.lower()
        agent_tools = policy.get_agent_tools(step.agent)
        agent_can_mutate = step.agent == "coding" or any(
            policy.is_approval_required(tool) for tool in agent_tools
        )
        coding_action = step.agent == "coding" and _contains_keyword(
            desc_lower,
            ("write", "modify", "execute", "run", "delete", "create", "overwrite"),
        )
        explicit_gated_tool = any(
            tool in query_lower or tool in desc_lower for tool in gated_tools
        )
        if explicit_gated_tool or coding_action or (
            agent_can_mutate and _contains_keyword(step.input_query, MUTATING_KEYWORDS)
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
        with open(prompt_path, encoding="utf-8") as f:
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
        desc_prefix = "Analyze and execute coding task" if is_write else "Inspect repository"
        steps.append(
            Step(
                id="step_1",
                description=f"{desc_prefix}: {query[:60]}",
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

        # Extract latest human query and message ID to distinguish new conversation turns
        # from intra-turn replanning triggered by the Reflector.
        user_query = ""
        latest_human_id: str | None = None
        for msg in reversed(trimmed_msgs):
            if isinstance(msg, HumanMessage):
                user_query = str(msg.content)
                msg_id = getattr(msg, "id", None)
                latest_human_id = (
                    str(msg_id)
                    if msg_id
                    else hashlib.sha256(user_query.encode("utf-8")).hexdigest()
                )
                break

        is_new_turn = (
            latest_human_id is not None
            and state.last_turn_human_message_id is not None
            and latest_human_id != state.last_turn_human_message_id
        )

        if not user_query:
            user_query = "Process request"

        # Reuse an existing plan only within the SAME turn when not replanning.
        if (
            not is_new_turn
            and state.plan
            and state.plan.steps
            and not state.reflection_notes
        ):
            out: dict[str, Any] = {"plan": state.plan, "messages": trimmed_msgs}
            if latest_human_id and state.last_turn_human_message_id is None:
                out["last_turn_human_message_id"] = latest_human_id
            return out

        active_reflection_notes = [] if is_new_turn else list(state.reflection_notes)
        turn_marker = latest_human_id or state.last_turn_human_message_id

        def _with_turn_state(payload: dict[str, Any]) -> dict[str, Any]:
            if turn_marker:
                payload["last_turn_human_message_id"] = turn_marker
                if is_new_turn or not state.metadata.get("turn_id"):
                    payload["metadata"] = {**state.metadata, "turn_id": turn_marker}
            if is_new_turn:
                payload.update(
                    {
                        "reflection_notes": [],
                        "reflection_count": 0,
                        "requires_approval": False,
                        "pending_approval_step_id": None,
                        "approval_granted": None,
                        "approved_step_ids": [],
                        "approved_step_fingerprints": {},
                        "pending_tool_approval": None,
                        "approved_tool_call_digests": {},
                        "approved_tool_calls": {},
                        "final_answer": None,
                    }
                )
            elif active_reflection_notes:
                payload["approved_step_ids"] = []
                payload["approved_step_fingerprints"] = {}
                payload["approval_granted"] = None
            return payload

        settings = get_settings()
        prompt_file = settings.prompts_dir / "planner.md"
        system_prompt = load_cached_planner_prompt(prompt_file)
        strict_env = (
            getattr(settings, "environment", None) in ("production", "staging")
            or getattr(settings, "is_production", False) is True
            or getattr(settings, "is_strict_environment", False) is True
        )

        if not strict_env and (
            settings.fast_heuristic_planner is True
            or os.environ.get("FAST_HEURISTIC_PLANNER") == "true"
        ):
            fallback_plan = _heuristic_planner(user_query)
            return _with_turn_state(
                {
                    "plan": fallback_plan,
                    "current_step_index": 0,
                    "messages": trimmed_msgs,
                    "degraded": False,
                    "degraded_reason": None,
                }
            )

        try:
            llm = get_llm(alias="reasoning-llm", temperature=0.1)
            structured_llm = llm.with_structured_output(Plan)
            # Inject the system prompt at the caller. KV-cache optimization may
            # be disabled, in which case the optimizer passes messages through
            # untouched — the system message must still be present.
            raw_messages: list[BaseMessage] = []
            if system_prompt:
                raw_messages.append(SystemMessage(content=system_prompt.strip()))
            ltm_user_id = str(state.metadata.get("user_id") or "").strip()
            if ltm_user_id and ltm_user_id not in {"anonymous_user", "unknown"}:
                try:
                    from src.agentic.memory.long_term import get_long_term_store

                    memories = await asyncio.to_thread(
                        get_long_term_store().search,
                        user_query,
                        None,
                        3,
                        ltm_user_id,
                    )
                    if memories:
                        from src.agentic.safety.guardrails import get_guardrails

                        mem_lines = "\n".join(
                            f"- [{m.category}] "
                            f"{get_guardrails().sanitize_input(m.content[:240])}"
                            for m in memories
                        )
                        raw_messages.append(
                            HumanMessage(content=f"Relevant Long-Term Memory:\n{mem_lines}")
                        )
                except Exception as mem_exc:
                    logger.debug("Optional LTM recall skipped in planner: %s", mem_exc)
            raw_messages.append(
                HumanMessage(
                    content=f"User Goal: {user_query}\nCreate a structured execution plan."
                )
            )
            if active_reflection_notes:
                raw_messages.append(
                    HumanMessage(
                        content=f"Previous reflection feedback: {active_reflection_notes[-1]}. Please revise the plan."
                    )
                )

            messages = optimize_messages_for_kv_cache(raw_messages, system_prompt=system_prompt)

            plan = await structured_llm.ainvoke(messages)
            if isinstance(plan, Plan) and plan.steps:
                _apply_risk_policy(plan)
                return _with_turn_state(
                    {
                        "plan": plan,
                        "current_step_index": 0,
                        "messages": trimmed_msgs,
                        "degraded": False,
                        "degraded_reason": None,
                    }
                )
        except Exception as e:
            logger.error(f"Structured planning failed ({e}). Using heuristic planner fallback.")

        fallback_plan = _heuristic_planner(user_query)
        return _with_turn_state(
            {
                "plan": fallback_plan,
                "current_step_index": 0,
                "messages": trimmed_msgs,
                "degraded": True,
                "degraded_reason": "planner_model_failure_heuristic_fallback",
            }
        )
    finally:
        NODE_DURATION.labels(node_name="planner").observe(time.time() - start_t)
