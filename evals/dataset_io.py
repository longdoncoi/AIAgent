"""Non-blocking, validated readers for evaluation datasets."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw_line.strip():
            continue
        value = json.loads(raw_line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: JSONL record must be an object")
        records.append(value)
    return records


async def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL dataset without blocking the event loop."""
    return await asyncio.to_thread(_read_jsonl, path)
