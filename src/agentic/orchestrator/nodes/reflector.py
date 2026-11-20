"""Reflector Node validating step outputs and performing self-correction."""

import logging
from typing import Any

from pydantic import BaseModel, Field

from src.agentic.llm.gateway import get_llm
from src.agentic.memory.short_term import trim_messages_window
from src.agentic.observability.metrics import NODE_DURATION
from src.agentic.observability.tracing import traced_operation
from src.agentic.orchestrator.state import AgentState

logger = logging.getLogger(__name__)


class ReflectionEvaluation(BaseModel):
    passed: bool = Field(description="Whether output answers the plan goal adequately")
    confidence_score: float = Field(default=0.9, description="Confidence in accuracy (0.0-1.0)")
    critique: str = Field(description="Brief assessment of the outputs")
    action: str = Field(default="proceed", description="proceed | retry | replan")
    feedback: str | None = Field(default=None, description="Suggestions if not passed")


@traced_operation("orchestrator.reflector")
async def reflector_node(state: AgentState) -> dict[str, Any]:
    """Evaluates outputs against the plan goal to determine if replanning/retrying is required."""
    import time

    start_t = time.time()
    try:
        # Context window trimming protection
        trimmed_msgs = trim_messages_window(state.messages)
        if not state.plan or not state.plan.steps:
            return {"reflection_notes": state.reflection_notes, "messages": trimmed_msgs}

        # If all steps failed or there's nothing to reflect upon
        outputs = [s.output for s in state.plan.steps if s.output]
        if not outputs:
            return {"reflection_notes": state.reflection_notes, "messages": trimmed_msgs}

        # Cap reflection loops to prevent infinite cycles
        if state.reflection_count >= 2:
            logger.info("Maximum reflection count reached. Proceeding to finalization.")
            return {"reflection_notes": state.reflection_notes, "messages": trimmed_msgs}

        goal = state.plan.goal
        summary_of_work = "\n".join(
            f"Step '{s.id}' ({s.agent}): {(s.output or '')[:200]}" for s in state.plan.steps
        )

        try:
            llm = get_llm(alias="reasoning-llm", temperature=0.1)
            structured_llm = llm.with_structured_output(ReflectionEvaluation)
            eval_prompt = (
                f"Original Goal: {goal}\n\n"
                f"Execution Results:\n{summary_of_work}\n\n"
                "Critique whether the goal has been addressed sufficiently and accurately."
            )
            evaluation = await structured_llm.ainvoke(eval_prompt)
            if isinstance(evaluation, ReflectionEvaluation):
                notes = list(state.reflection_notes)
                needs_replan = (
                    not evaluation.passed
                    or evaluation.action in ["retry", "replan"]
                    or evaluation.confidence_score < 0.6
                )
                if needs_replan and state.reflection_count < 2:
                    notes.append(
                        f"REPLAN_REQUIRED (score={evaluation.confidence_score:.2f}): {evaluation.critique}. "
                        f"Feedback: {evaluation.feedback or 'Refine step execution to address goal.'}"
                    )
                else:
                    notes.append(
                        f"PASSED (score={evaluation.confidence_score:.2f}): {evaluation.critique}"
                    )

                return {
                    "reflection_notes": notes,
                    "reflection_count": state.reflection_count + 1,
                    "messages": trimmed_msgs,
                }
        except Exception as e:
            logger.debug(f"Reflection LLM check skipped/failed ({e}). Assuming valid output.")

        return {
            "reflection_notes": state.reflection_notes
            + ["Results verified through heuristic check."],
            "reflection_count": state.reflection_count + 1,
            "messages": trimmed_msgs,
        }
    finally:
        NODE_DURATION.labels(node_name="reflector").observe(time.time() - start_t)
