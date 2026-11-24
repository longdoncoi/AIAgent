---
name: test-coverage
description: Select, run, debug, and report unit, integration, end-to-end, or coverage tests for this repository. Use when changing behavior, reproducing a bug, investigating failures, or validating coverage without unnecessary full-suite runs.
---

# Test and coverage

1. Map changed code to existing tests with `rg`; add a regression test when behavior changes or a bug is fixed.
2. Run the smallest useful test first:

   ```bash
   pytest path/to/test_file.py -q
   pytest tests/ -k "expression" -q
   ```

3. Mock external LLM/network services in unit tests. Use `AsyncMock`/HTTP fixtures for async boundaries and isolate database/cache state per test.
4. Prefer `asyncio_mode=auto` from `pyproject.toml`; add explicit markers only where the suite requires them.
5. Run the repository suite sequentially unless shared singleton/cache/SQLite state has been proven safe for xdist:

   ```bash
   make test
   ```

6. Treat coverage as a signal: inspect uncovered changed branches and failure paths, not only the total percentage.
7. Report the exact command, pass/fail/skip counts and coverage only from the current run. If infrastructure-dependent tests were skipped, name the missing dependency.
