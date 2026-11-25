"""Neutral request-context primitives shared across layers."""

from __future__ import annotations

from contextvars import ContextVar

REQUEST_ID_VAR: ContextVar[str] = ContextVar("agentic_request_id", default="-")


def get_request_id() -> str:
    return REQUEST_ID_VAR.get()


def set_request_id(request_id: str) -> object:
    return REQUEST_ID_VAR.set(request_id)


def reset_request_id(token: object) -> None:
    REQUEST_ID_VAR.reset(token)  # type: ignore[arg-type]
