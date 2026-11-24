---
name: evaluate-ai-quality
description: Evaluate prompt, Supervisor routing, agent behavior, groundedness, and RAG quality with the repository's eval runners and configured gates. Use when prompts, models, routing, retrieval, reranking, guardrails, or evaluation datasets change.
---

# Evaluate AI and RAG quality

1. Identify the affected dimensions and dataset; do not run every evaluator by default.
2. Read current thresholds from `configs/eval_thresholds.yaml`. Never copy threshold values into reports or skills without naming the evaluated commit/config.
3. Run the matching gate:

   ```bash
   make eval        # routing/general judge
   make eval-deep   # agent behavior/groundedness
   make eval-ragas  # retrieval and answer quality
   ```

4. Keep external model calls explicit; record provider/model, dataset revision, seed/config and whether tracing was enabled.
5. Compare against a baseline for regressions, not only absolute pass/fail. Inspect failed examples for routing, retrieval, judge or dataset defects before changing prompts.
6. Add a regression case when fixing a real failure. Do not lower a gate merely to make CI green without an approved decision and rationale.

Report unavailable credentials/services as not run, never as pass.
