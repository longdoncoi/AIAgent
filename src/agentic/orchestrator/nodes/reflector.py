"""Reflector Node validating step outputs and performing self-correction.

Fail-closed contract:
    The LLM judge is the only component allowed to declare outputs "verified".
    If it raises or returns an unusable result, the reflector NEVER claims a
    heuristic check ran: it flags the state as degraded, emits a metric, and
    asks for a bounded re-plan. A degraded run is routed to manual review if
    the retry budget is exhausted (see ``orchestrator.graph`` and ``finalize``).
"""

import logging
import time
from typing import Any, Literal

from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field

from src.agentic.llm.gateway import get_llm
from src.agentic.memory.short_term import trim_messages_window
from src.agentic.observability.metrics import NODE_DURATION, REFLECTION_FALLBACK_TOTAL
from src.agentic.observability.tracing import traced_operation
from src.agentic.orchestrator.state import MAX_REFLECTION_ATTEMPTS, AgentState, Step

logger = logging.getLogger(__name__)

# REFLECTION_FALLBACK_TOTAL is defined in observability/metrics.py (owned by the
# observability stream). Import it directly; do not register a duplicate Counter.


class ReflectionEvaluation(BaseModel):
    passed: bool = Field(description="Whether output answers the plan goal adequately")
    confidence_score: float = Field(default=0.9, description="Confidence in accuracy (0.0-1.0)")
    critique: str = Field(description="Brief assessment of the outputs")
    action: Literal["proceed", "retry", "replan"] = Field(
        default="proceed",
        description=(
            "proceed = accept outputs; retry = re-run failed steps keeping the plan; "
            "replan = discard the plan and generate a new one"
        ),
    )
    feedback: str | None = Field(default=None, description="Suggestions if not passed")


def _judge_fallback(
    state: AgentState,
    trimmed_msgs: list[BaseMessage],
    reason: str,
    exc: Exception | None = None,
) -> dict[str, Any]:
    """Fail closed when the judge cannot be trusted. Never claims verification."""
    logger.warning(
        "Reflection judge unavailable (%s). Failing closed; outputs remain UNVERIFIED.%s",
        reason,
        f" Cause: {exc}" if exc else "",
    )
    REFLECTION_FALLBACK_TOTAL.labels(reason=reason).inc()
    notes = list(state.reflection_notes)
    notes.append(
        f"REPLAN_REQUIRED (degraded, reason={reason}): "
        "Reflection judge could not verify these outputs; they are NOT approved for "
        "finalization. Manual review is required if the retry budget is exhausted."
    )
    return {
        "reflection_notes": notes,
        "reflection_count": state.reflection_count + 1,
        "degraded": True,
        "degraded_reason": reason,
        "messages": trimmed_msgs,
    }


def _reset_failed_steps(state: AgentState) -> tuple[list[Step], set[str]] | None:
    """Return a plan with failed steps reset to pending, plus the reset step IDs."""
    failed_ids = {s.id for s in state.plan.steps if s.status == "failed"} if state.plan else set()
    if not failed_ids or state.plan is None:
        return None
    reset_steps = [
        s.model_copy(update={"status": "pending", "output": None}) if s.id in failed_ids else s
        for s in state.plan.steps
    ]
    return reset_steps, failed_ids


@traced_operation("orchestrator.reflector")
async def reflector_node(state: AgentState) -> dict[str, Any]:
    """Evaluates outputs against the plan goal; never auto-approves on judge failure."""
    start_t = time.time()
    try:
        # Context window trimming protection
        trimmed_msgs = trim_messages_window(state.messages)
        if not state.plan or not state.plan.steps:
            return {"reflection_notes": state.reflection_notes, "messages": trimmed_msgs}

        # If all steps failed or there is nothing to reflect upon
        outputs = [s.output for s in state.plan.steps if s.output]
        if not outputs:
            logger.info("Reflector skipped: no step outputs available to evaluate.")
            return {"reflection_notes": state.reflection_notes, "messages": trimmed_msgs}

        # Cap reflection loops to prevent infinite cycles
        if state.reflection_count >= MAX_REFLECTION_ATTEMPTS:
            logger.info("Maximum reflection count reached. Proceeding to finalization.")
            return {"reflection_notes": state.reflection_notes, "messages": trimmed_msgs}

        goal = state.plan.goal
        summary_of_work = "\n".join(
            f"Step '{s.id}' ({s.agent}) [status={s.status}]: {(s.output or '')[:200]}"
            for s in state.plan.steps
        )

        try:
            llm = get_llm(alias="reasoning-llm", temperature=0.1)
            structured_llm = llm.with_structured_output(ReflectionEvaluation)
            eval_prompt = (
                f"Original Goal: {goal}\n\n"
                f"Execution Results:\n{summary_of_work}\n\n"
                "Critique whether the goal has been addressed sufficiently and accurately. "
                "Use action='retry' only when re-running failed steps can fix the issue while "
                "keeping the current plan; use action='replan' when the plan itself is wrong."
            )
            evaluation = await structured_llm.ainvoke(eval_prompt)
        except Exception as exc:
            return _judge_fallback(state, trimmed_msgs, "judge_exception", exc)

        if not isinstance(evaluation, ReflectionEvaluation):
            logger.warning("Judge returned an unusable result: %r", evaluation)
            return _judge_fallback(state, trimmed_msgs, "judge_invalid_result")

        notes = list(state.reflection_notes)
        score = evaluation.confidence_score
        needs_revision = (
            not evaluation.passed
            or evaluation.action in ("retry", "replan")
            or score < 0.6
        )
        has_budget = state.reflection_count + 1 < MAX_REFLECTION_ATTEMPTS

        delta: dict[str, Any] = {
            "reflection_notes": notes,
            "reflection_count": state.reflection_count + 1,
            "messages": trimmed_msgs,
            # A successful judge result clears any earlier degraded signal.
            "degraded": False,
            "degraded_reason": None,
        }

        if needs_revision and has_budget:
            if evaluation.action == "retry":
                reset = _reset_failed_steps(state)
                if reset is not None:
                    reset_steps, failed_ids = reset
                    delta["plan"] = state.plan.model_copy(update={"steps": reset_steps})
                    # Re-running a step requires a fresh HITL decision: drop any
                    # grants issued for the previous attempt.
                    delta["approved_step_ids"] = [
                        sid for sid in state.approved_step_ids if sid not in failed_ids
                    ]
                    delta["approved_step_fingerprints"] = {
                        sid: fp
                        for sid, fp in state.approved_step_fingerprints.items()
                        if sid not in failed_ids
                    }
                    delta["approval_granted"] = None
                    notes.append(
                        f"RETRY_REQUIRED (score={score:.2f}): {evaluation.critique}. "
                        f"Feedback: {evaluation.feedback or 'Re-run failed steps.'}"
                    )
                else:
                    # Nothing to retry: escalate to a full re-plan instead of
                    # burning a loop on an unchanged plan.
                    notes.append(
                        f"REPLAN_REQUIRED (score={score:.2f}, retry had no failed steps): "
                        f"{evaluation.critique}. "
                        f"Feedback: {evaluation.feedback or 'Refine step execution to address goal.'}"
                    )
                    delta["approved_step_ids"] = []
                    delta["approved_step_fingerprints"] = {}
                    delta["approval_granted"] = None
            else:
                notes.append(
                    f"REPLAN_REQUIRED (score={score:.2f}): {evaluation.critique}. "
                    f"Feedback: {evaluation.feedback or 'Refine step execution to address goal.'}"
                )
                # New plan => old approvals are no longer bound to its fingerprints.
                delta["approved_step_ids"] = []
                delta["approved_step_fingerprints"] = {}
                delta["approval_granted"] = None
        elif needs_revision:
            notes.append(
                f"UNRESOLVED (score={score:.2f}, budget exhausted): {evaluation.critique}. "
                "Proceeding to finalization with explicit limitations."
            )
        else:
            notes.append(f"PASSED (score={score:.2f}): {evaluation.critique}")

        return delta
    finally:
        NODE_DURATION.labels(node_name="reflector").observe(time.time() - start_t)
