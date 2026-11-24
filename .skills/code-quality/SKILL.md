---
name: code-quality
description: "Run and fix repository code-quality checks: Ruff lint/import ordering, Mypy strict typing, dependency audit, async blocking review, Pydantic boundary validation, and scoped formatting. Use before handoff or when static analysis/security gates fail."
---

# Check code quality

1. Run the narrowest relevant check while iterating:

   ```bash
   ruff check <paths>
   mypy <module-or-src>
   ```

2. Use `ruff check --fix <paths>` only for safe mechanical fixes; inspect the diff. Format only touched Python paths when formatting is required.
3. Fix types at the source. Avoid broad `Any`, unchecked casts and ignore comments unless the boundary is genuinely untyped and the reason is documented.
4. Review async code for sync network/DB/file calls, `time.sleep`, CPU-heavy work and per-request client/pool construction.
5. Keep DTO/state/request-response validation in Pydantic v2; use strict/forbid-extra behavior at security-sensitive boundaries when compatible with the contract.
6. Before handoff, run:

   ```bash
   make lint
   make audit   # when dependencies, build images, CI, or release scope changes
   ```

Report commands and outcomes. Do not claim repository-wide cleanliness from a path-scoped run.
