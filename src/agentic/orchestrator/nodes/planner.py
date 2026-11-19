"""Planner Node decomposing queries into discrete execution steps."""

import logging
import time
from typing import Any, Literal

from langchain_core.messages import HumanMessage

from src.agentic.cache.kv_cache import optimize_messages_for_kv_cache
from src.agentic.core.config import get_settings
from src.agentic.llm.gateway import get_llm
from src.agentic.memory.short_term import trim_messages_window
from src.agentic.observability.metrics import NODE_DURATION
from src.agentic.observability.tracing import traced_operation
from src.agentic.orchestrator.state import AgentState, Plan, Step
from src.agentic.safety.policy import get_policy_manager

logger = logging.getLogger(__name__)


def _heuristic_planner(query: str) -> Plan:
    """Fallback heuristic planner for reliable deterministic planning."""
    query_lower = query.lower()
    steps: list[Step] = []

    # Check for RAG / policy keywords
    is_rag = any(k in query_lower for k in ["policy", "quy định", "chính sách", "tài liệu", "rule", "internal", "documentation"])
    # Check for active coding action keywords
    is_coding_action = any(k in query_lower for k in ["viết", "sửa", "chạy", "tạo", "write", "run", "debug", "refactor", "execute", "build"])
    # Check for coding keywords
    is_coding = any(k in query_lower for k in ["code", "codebase", "symbol", "python", "script", "test", "repo", "file", "bug", "directory", "folder"])

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
        is_write = any(k in query_lower for k in ["write", "create", "delete", "remove", "overwrite", "ghi", "xóa"])
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
        system_prompt = ""
        if prompt_file.exists():
            try:
                with open(prompt_file, "r", encoding="utf-8") as f:
                    system_prompt = f.read()
            except Exception:
                pass

        try:
            llm = get_llm(alias="reasoning-llm", temperature=0.1)
            structured_llm = llm.with_structured_output(Plan)
            raw_messages = [
                HumanMessage(content=f"User Goal: {user_query}\nCreate a structured execution plan.")
            ]
            if state.reflection_notes:
                raw_messages.append(
                    HumanMessage(content=f"Previous reflection feedback: {state.reflection_notes[-1]}. Please revise the plan.")
                )

            messages = optimize_messages_for_kv_cache(raw_messages, system_prompt=system_prompt)

            plan = await structured_llm.ainvoke(messages)
            if isinstance(plan, Plan) and plan.steps:
                # Validate risk levels against policy
                policy_mgr = get_policy_manager()
                for s in plan.steps:
                    if any(w in s.input_query.lower() for w in ["write", "delete", "commit", "bash", "shell"]):
                        s.risk_level = "high"
                    elif policy_mgr.is_approval_required(s.agent):
                        s.risk_level = "high"
                return {"plan": plan, "current_step_index": 0, "messages": trimmed_msgs}
        except Exception as e:
            logger.warning(f"Structured planning failed ({e}). Using heuristic planner fallback.")

        fallback_plan = _heuristic_planner(user_query)
        return {"plan": fallback_plan, "current_step_index": 0, "messages": trimmed_msgs}
    finally:
        NODE_DURATION.labels(node_name="planner").observe(time.time() - start_t)
