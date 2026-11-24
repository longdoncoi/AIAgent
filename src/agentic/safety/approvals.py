"""Signed, short-lived HITL approval grants (capability tokens).

The previous design propagated a caller-controlled boolean
(``metadata["hitl_approved"]``) across the A2A boundary. Any caller able to
reach ``/a2a/tasks`` could therefore authorize ``code_exec_bash`` or
``repo_write_file`` without a human decision.

An :class:`ApprovalGrant` is instead minted **by the supervisor** after a
real HITL decision and is authenticated with an HMAC key derived from the
deployment secret. It is bound to:

* the exact step (``step_id`` + ``fingerprint``),
* the owning principal (``owner_id``),
* the sub-agent allowed to use it (``agent_name``),
* the set of approval-gated tools authorised for that step,
* a short expiration window.

Verification is fail-closed: a missing, malformed, expired, or mismatched
grant always denies execution.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.agentic.core.config import Settings, get_settings
from src.agentic.core.redis import get_redis_client
from src.agentic.observability.metrics import APPROVAL_GRANTS_TOTAL

logger = logging.getLogger(__name__)

_GRANT_VERSION = 2
_DOMAIN = b"agentic-approval-grant-v2\x00"
_NONCE_PREFIX = "agentic:approval:nonce:"
_local_used_nonces: dict[str, int] = {}
_local_nonce_lock = threading.Lock()


class ApprovalGrantPayload(BaseModel):
    """Signed claims carried by an approval grant."""

    v: int = _GRANT_VERSION
    step_id: str
    fingerprint: str
    owner_id: str
    agent_name: str
    tools: list[str] = Field(default_factory=list)
    iat: int
    exp: int
    nonce: str
    purpose: Literal["step", "call"] = "step"
    args_digest: str | None = None


@dataclass(frozen=True)
class ApprovalGrant:
    """A verified approval grant, safe to trust at the tool boundary."""

    payload: ApprovalGrantPayload

    @property
    def step_id(self) -> str:
        return self.payload.step_id

    @property
    def owner_id(self) -> str:
        return self.payload.owner_id


def _approval_key(settings: Settings) -> bytes:
    """Dedicated HITL key; never derived from the shared S2S transport token."""
    material = _DOMAIN + settings.approval_key.encode("utf-8")
    return hashlib.sha256(material).digest()


def _consume_nonce(nonce: str, expires_at: int, settings: Settings) -> bool:
    """Atomically consume a one-time nonce; Redis in prod, local dict in dev."""
    ttl = max(1, expires_at - int(time.time()))
    if settings.is_production:
        client = get_redis_client(settings.redis_url)
        if client is None:
            logger.error("Approval nonce store unavailable; failing closed")
            return False
        try:
            return bool(client.set(f"{_NONCE_PREFIX}{nonce}", "1", ex=ttl, nx=True))
        except Exception as exc:
            logger.error("Approval nonce store error; failing closed: %s", exc)
            return False
    now = int(time.time())
    with _local_nonce_lock:
        for key in [k for k, exp in _local_used_nonces.items() if exp <= now]:
            _local_used_nonces.pop(key, None)
        if nonce in _local_used_nonces:
            return False
        _local_used_nonces[nonce] = expires_at
        return True


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def canonical_args_digest(tool_name: str, arguments: dict[str, Any]) -> str:
    """Stable digest of a tool call, used for optional argument-level binding."""
    canonical = json.dumps(
        {"tool": tool_name, "args": arguments}, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def issue_approval_grant(
    *,
    step_id: str,
    fingerprint: str,
    owner_id: str,
    agent_name: str,
    tools: list[str] | None = None,
    ttl_seconds: int | None = None,
    args_digest: str | None = None,
    purpose: Literal["step", "call"] = "step",
    settings: Settings | None = None,
) -> str:
    """Mint a signed grant. Must only be called by the supervisor/HITL node."""
    cfg = settings or get_settings()
    if purpose == "call" and args_digest is None:
        raise ValueError("A call-scoped approval grant must bind an args_digest")
    now = int(time.time())
    payload = ApprovalGrantPayload(
        step_id=step_id,
        fingerprint=fingerprint,
        owner_id=owner_id,
        agent_name=agent_name,
        tools=sorted(set(tools or [])),
        iat=now,
        exp=now + (ttl_seconds if ttl_seconds is not None else cfg.approval_grant_ttl_seconds),
        nonce=secrets.token_hex(8),
        purpose=purpose,
        args_digest=args_digest,
    )
    body = _b64encode(payload.model_dump_json().encode("utf-8"))
    signature = _b64encode(hmac.new(_approval_key(cfg), body.encode("ascii"), hashlib.sha256).digest())
    APPROVAL_GRANTS_TOTAL.labels(outcome="issued").inc()
    return f"{body}.{signature}"


def issue_call_grant(
    *,
    step_grant: ApprovalGrant,
    tool_name: str,
    arguments: dict[str, Any],
    ttl_seconds: int | None = None,
    settings: Settings | None = None,
) -> str:
    """Issue a single-use grant bound to the exact tool call arguments."""
    return issue_approval_grant(
        step_id=step_grant.step_id,
        fingerprint=step_grant.payload.fingerprint,
        owner_id=step_grant.owner_id,
        agent_name=step_grant.payload.agent_name,
        tools=[tool_name],
        args_digest=canonical_args_digest(tool_name, arguments),
        purpose="call",
        ttl_seconds=ttl_seconds,
        settings=settings,
    )


def verify_approval_grant(
    token: str | None,
    *,
    step_id: str,
    fingerprint: str,
    owner_id: str | None,
    agent_name: str,
    tool_name: str | None = None,
    arguments: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> ApprovalGrant | None:
    """Verify a grant and all its bindings. Returns ``None`` on any failure."""
    if not token or not isinstance(token, str) or "." not in token:
        return None
    cfg = settings or get_settings()
    try:
        body, signature = token.rsplit(".", 1)
        expected = _b64encode(
            hmac.new(_approval_key(cfg), body.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(signature, expected):
            logger.warning("Rejected approval grant with invalid signature")
            return None

        payload = ApprovalGrantPayload.model_validate_json(_b64decode(body))
        if payload.v != _GRANT_VERSION:
            return None
        now = int(time.time())
        if payload.exp <= now or payload.iat > now + 60:
            logger.warning("Rejected expired or future approval grant for step %s", payload.step_id)
            return None
        if payload.step_id != step_id:
            return None
        if payload.fingerprint != fingerprint:
            logger.warning("Approval fingerprint mismatch for step %s", step_id)
            return None
        if payload.agent_name not in (agent_name, agent_name.replace("_agent", "")):
            return None
        if (
            not owner_id
            or owner_id in {"anonymous_user", "unknown"}
            or not hmac.compare_digest(payload.owner_id, owner_id)
        ):
            logger.warning("Approval owner mismatch for step %s", step_id)
            return None
        if tool_name is not None:
            allowed = {name.replace("_agent", "") for name in payload.tools}
            if tool_name not in payload.tools and tool_name not in allowed:
                logger.warning("Tool %s not covered by approval grant for step %s", tool_name, step_id)
                return None
        if payload.purpose == "call":
            if payload.args_digest is None or arguments is None:
                logger.warning("Call-scoped grant without argument binding rejected for step %s", step_id)
                return None
            if not hmac.compare_digest(
                payload.args_digest, canonical_args_digest(tool_name or "", arguments)
            ):
                return None
            if not _consume_nonce(payload.nonce, payload.exp, cfg):
                logger.warning("Replayed approval grant rejected for step %s", step_id)
                return None
        elif arguments is not None and payload.args_digest is not None:
            if not hmac.compare_digest(payload.args_digest, canonical_args_digest(tool_name or "", arguments)):
                return None
    except Exception as exc:  # malformed token must never grant access
        logger.warning("Rejected malformed approval grant: %s", exc)
        APPROVAL_GRANTS_TOTAL.labels(outcome="rejected").inc()
        return None
    APPROVAL_GRANTS_TOTAL.labels(outcome="verified").inc()
    return ApprovalGrant(payload=payload)
