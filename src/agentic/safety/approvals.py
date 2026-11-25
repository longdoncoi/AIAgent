"""Short-lived, attenuated HITL approval capabilities.

Only the supervisor holds the Ed25519 private key and may mint a step grant.
Agents receive the public key plus a signed step capability.  They can narrow
that capability to one exact tool invocation, but cannot widen its tool set,
identity bindings, or expiry.  MCP servers verify both layers with public
material and atomically consume the call nonce.
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

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, Field

from src.agentic.core.config import Settings, get_settings, is_strict_env
from src.agentic.core.redis import get_redis_client
from src.agentic.observability.metrics import APPROVAL_GRANTS_TOTAL

logger = logging.getLogger(__name__)

_GRANT_VERSION = 3
_STEP_DOMAIN = b"agentic-approval-step-v3\x00"
_CALL_DOMAIN = b"agentic-approval-call-v3\x00"
_NONCE_PREFIX = "agentic:approval:nonce:"
_local_used_nonces: dict[str, int] = {}
_local_nonce_lock = threading.Lock()


class ApprovalGrantPayload(BaseModel):
    """Authenticated claims carried by a step or call capability."""

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
    approved_args_digests: list[str] | None = None
    kid: str | None = None


@dataclass(frozen=True)
class ApprovalGrant:
    """A verified grant with the cryptographic root needed for attenuation."""

    payload: ApprovalGrantPayload
    token: str
    root_signature: bytes

    @property
    def step_id(self) -> str:
        return self.payload.step_id

    @property
    def owner_id(self) -> str:
        return self.payload.owner_id


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _decode_raw_key(value: str, *, name: str) -> bytes:
    encoded = value.removeprefix("base64:").strip()
    try:
        raw = _b64decode(encoded)
    except Exception as exc:
        raise ValueError(f"{name} must be URL-safe base64") from exc
    if len(raw) != 32:
        raise ValueError(f"{name} must encode exactly 32 bytes")
    return raw


def _development_private_seed(settings: Settings) -> bytes:
    """Stable local-only key so development and unit tests stay ergonomic."""
    return hashlib.sha256(
        _STEP_DOMAIN + settings.approval_key.encode("utf-8")
    ).digest()


def _private_key(settings: Settings) -> Ed25519PrivateKey:
    if settings.approval_private_key:
        seed = _decode_raw_key(
            settings.approval_private_key, name="approval_private_key"
        )
        return Ed25519PrivateKey.from_private_bytes(seed)
    if is_strict_env(settings):
        raise ValueError("approval_private_key is unavailable in this workload")
    return Ed25519PrivateKey.from_private_bytes(_development_private_seed(settings))


def _public_key(settings: Settings) -> Ed25519PublicKey:
    if settings.approval_public_key:
        raw = _decode_raw_key(
            settings.approval_public_key, name="approval_public_key"
        )
        return Ed25519PublicKey.from_public_bytes(raw)
    if settings.approval_private_key or not is_strict_env(settings):
        return _private_key(settings).public_key()
    raise ValueError("approval_public_key is unavailable in this workload")


def _active_key_id(settings: Settings) -> str:
    explicit = getattr(settings, "approval_key_id", None)
    if explicit:
        return str(explicit).strip()
    raw = _public_key(settings).public_bytes_raw()
    return hashlib.sha256(raw).hexdigest()[:16]


def _verification_keys_for_kid(
    settings: Settings, kid: str | None
) -> list[Ed25519PublicKey]:
    """Resolve verification key(s) matching optional grant key identifier ``kid``."""
    active_key = _public_key(settings)
    active_kid = _active_key_id(settings)
    prev_map = getattr(settings, "approval_previous_public_keys", None) or {}
    if not kid:
        return [active_key]
    if kid == active_kid:
        return [active_key]
    prev_b64 = prev_map.get(kid)
    if prev_b64:
        raw = _decode_raw_key(prev_b64, name=f"approval_previous_public_keys[{kid}]")
        return [Ed25519PublicKey.from_public_bytes(raw)]
    raise InvalidSignature(f"unknown approval key id {kid!r}")


def _consume_nonce(nonce: str, expires_at: int, settings: Settings) -> bool:
    """Atomically consume a one-time nonce; Redis in prod/staging, local dict in dev."""
    ttl = max(1, expires_at - int(time.time()))
    if is_strict_env(settings):
        state_redis_url = settings.redis_state_url or settings.redis_url
        client = get_redis_client(state_redis_url)
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
        for key in [key for key, exp in _local_used_nonces.items() if exp <= now]:
            _local_used_nonces.pop(key, None)
        if nonce in _local_used_nonces:
            return False
        _local_used_nonces[nonce] = expires_at
        return True


def canonical_args_digest(tool_name: str, arguments: dict[str, Any]) -> str:
    """Return a stable digest binding a capability to one exact invocation."""
    canonical = json.dumps(
        {"tool": tool_name, "args": arguments},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
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
    approved_args_digests: list[str] | None = None,
    purpose: Literal["step", "call"] = "step",
    settings: Settings | None = None,
) -> str:
    """Mint an Ed25519 step capability; callable only by the supervisor."""
    cfg = settings or get_settings()
    if purpose != "step":
        if args_digest is None:
            raise ValueError("A call-scoped approval grant must bind an args_digest")
        raise ValueError("Call grants must attenuate a verified step grant")
    now = int(time.time())
    payload = ApprovalGrantPayload(
        step_id=step_id,
        fingerprint=fingerprint,
        owner_id=owner_id,
        agent_name=agent_name,
        tools=sorted(set(tools or [])),
        iat=now,
        exp=now
        + (ttl_seconds if ttl_seconds is not None else cfg.approval_grant_ttl_seconds),
        nonce=secrets.token_hex(16),
        approved_args_digests=sorted(set(approved_args_digests))
        if approved_args_digests is not None
        else None,
        kid=_active_key_id(cfg),
    )
    body = _b64encode(payload.model_dump_json().encode("utf-8"))
    signature = _private_key(cfg).sign(_STEP_DOMAIN + body.encode("ascii"))
    APPROVAL_GRANTS_TOTAL.labels(outcome="issued").inc()
    return f"{body}.{_b64encode(signature)}"


def issue_call_grant(
    *,
    step_grant: ApprovalGrant,
    tool_name: str,
    arguments: dict[str, Any],
    ttl_seconds: int | None = None,
    settings: Settings | None = None,
) -> str:
    """Attenuate a verified step grant to one exact, single-use tool call."""
    cfg = settings or get_settings()
    root = step_grant.payload
    now = int(time.time())
    if root.purpose != "step" or root.exp <= now:
        raise ValueError("Cannot attenuate an expired or non-step grant")
    if tool_name not in root.tools:
        raise ValueError(f"Tool {tool_name!r} is not covered by the step grant")
    digest = canonical_args_digest(tool_name, arguments)
    if root.approved_args_digests is not None and digest not in root.approved_args_digests:
        raise ValueError(f"Tool arguments for {tool_name!r} were not approved by HITL")
    requested_exp = now + (
        ttl_seconds if ttl_seconds is not None else cfg.approval_grant_ttl_seconds
    )
    payload = ApprovalGrantPayload(
        step_id=root.step_id,
        fingerprint=root.fingerprint,
        owner_id=root.owner_id,
        agent_name=root.agent_name,
        tools=[tool_name],
        iat=now,
        exp=min(root.exp, requested_exp),
        nonce=secrets.token_hex(16),
        purpose="call",
        args_digest=digest,
        kid=root.kid,
    )
    body = _b64encode(payload.model_dump_json().encode("utf-8"))
    signature = hmac.new(
        step_grant.root_signature,
        _CALL_DOMAIN + body.encode("ascii"),
        hashlib.sha256,
    ).digest()
    APPROVAL_GRANTS_TOTAL.labels(outcome="attenuated").inc()
    return f"{step_grant.token}.{body}.{_b64encode(signature)}"


def _verify_step_layer(
    body: str, signature_text: str, settings: Settings
) -> tuple[ApprovalGrantPayload, bytes]:
    signature = _b64decode(signature_text)
    payload = ApprovalGrantPayload.model_validate_json(_b64decode(body))
    if payload.v != _GRANT_VERSION or payload.purpose != "step":
        raise ValueError("invalid step grant version or purpose")
    signed_data = _STEP_DOMAIN + body.encode("ascii")
    last_exc: Exception | None = None
    for pub_key in _verification_keys_for_kid(settings, payload.kid):
        try:
            pub_key.verify(signature, signed_data)
            return payload, signature
        except InvalidSignature as exc:
            last_exc = exc
    raise last_exc or InvalidSignature("invalid step grant signature")


def _verify_call_layer(
    *,
    root: ApprovalGrantPayload,
    root_signature: bytes,
    body: str,
    signature_text: str,
) -> ApprovalGrantPayload:
    supplied = _b64decode(signature_text)
    expected = hmac.new(
        root_signature, _CALL_DOMAIN + body.encode("ascii"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(supplied, expected):
        raise InvalidSignature("invalid attenuated call signature")
    payload = ApprovalGrantPayload.model_validate_json(_b64decode(body))
    immutable_bindings = (
        payload.v == _GRANT_VERSION,
        payload.purpose == "call",
        payload.step_id == root.step_id,
        payload.fingerprint == root.fingerprint,
        payload.owner_id == root.owner_id,
        payload.agent_name == root.agent_name,
        payload.iat >= root.iat,
        payload.exp <= root.exp,
        set(payload.tools).issubset(root.tools),
        len(payload.tools) == 1,
    )
    if not all(immutable_bindings):
        raise ValueError("call grant widens or changes its parent capability")
    return payload


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
    """Verify all capability layers and caller bindings; fail closed."""
    if not token or not isinstance(token, str):
        return None
    cfg = settings or get_settings()
    try:
        parts = token.split(".")
        if len(parts) not in {2, 4}:
            return None
        step_body, step_signature_text = parts[:2]
        root, root_signature = _verify_step_layer(
            step_body, step_signature_text, cfg
        )
        payload = root
        if len(parts) == 4:
            payload = _verify_call_layer(
                root=root,
                root_signature=root_signature,
                body=parts[2],
                signature_text=parts[3],
            )

        now = int(time.time())
        if payload.exp <= now or payload.iat > now + 60:
            logger.warning("Rejected expired or future grant for step %s", payload.step_id)
            return None
        if payload.step_id != step_id or payload.fingerprint != fingerprint:
            return None
        if payload.agent_name not in (agent_name, agent_name.replace("_agent", "")):
            return None
        if (
            not owner_id
            or owner_id in {"anonymous_user", "unknown"}
            or not hmac.compare_digest(payload.owner_id, owner_id)
        ):
            return None
        if tool_name is not None and tool_name not in payload.tools:
            return None
        if payload.purpose == "call":
            if payload.args_digest is None or arguments is None or tool_name is None:
                return None
            expected_digest = canonical_args_digest(tool_name, arguments)
            if not hmac.compare_digest(payload.args_digest, expected_digest):
                return None
            if not _consume_nonce(payload.nonce, payload.exp, cfg):
                logger.warning("Replayed approval grant rejected for step %s", step_id)
                return None
        elif arguments is not None:
            return None
    except (InvalidSignature, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("Rejected malformed approval grant: %s", exc)
        APPROVAL_GRANTS_TOTAL.labels(outcome="rejected").inc()
        return None
    APPROVAL_GRANTS_TOTAL.labels(outcome="verified").inc()
    return ApprovalGrant(
        payload=payload,
        token=f"{step_body}.{step_signature_text}",
        root_signature=root_signature,
    )


def verify_tool_call_grant(
    token: str | None,
    *,
    tool_name: str,
    arguments: dict[str, Any],
    settings: Settings | None = None,
) -> ApprovalGrant | None:
    """Verify and consume an attenuated call capability at the MCP boundary."""
    if not token or not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 4:
        return None
    try:
        untrusted = ApprovalGrantPayload.model_validate_json(_b64decode(parts[2]))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if untrusted.purpose != "call" or not untrusted.args_digest:
        return None
    return verify_approval_grant(
        token,
        step_id=untrusted.step_id,
        fingerprint=untrusted.fingerprint,
        owner_id=untrusted.owner_id,
        agent_name=untrusted.agent_name,
        tool_name=tool_name,
        arguments=arguments,
        settings=settings,
    )
