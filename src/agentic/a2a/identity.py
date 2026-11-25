"""Ed25519 signatures for authorization-relevant A2A request envelopes."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from src.agentic.core.config import Settings, is_strict_env

_DOMAIN = b"agentic-a2a-envelope-v1\x00"
_FIELD = "_identity"


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _key_id(public_key: bytes) -> str:
    """Return the stable short fingerprint used to route rotated verifier keys."""
    return hashlib.sha256(public_key).hexdigest()[:16]


def _canonical(request: Any, issued_at: int) -> bytes:
    body = request.model_dump(mode="json")
    metadata = dict(body.get("metadata") or {})
    metadata.pop(_FIELD, None)
    body["metadata"] = metadata
    body["identity_issued_at"] = issued_at
    return _DOMAIN + json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sign_request(request: Any, settings: Settings) -> None:
    """Attach a short-lived signature covering identity, payload, and routing fields."""
    if not settings.approval_private_key:
        if is_strict_env(settings):
            raise ValueError("A2A envelope signing key is unavailable")
        return
    issued_at = int(time.time())
    private_key = Ed25519PrivateKey.from_private_bytes(_decode(settings.approval_private_key))
    signature = private_key.sign(_canonical(request, issued_at))
    request.metadata[_FIELD] = {
        "issued_at": issued_at,
        "kid": settings.approval_key_id or _key_id(private_key.public_key().public_bytes_raw()),
        "signature": base64.urlsafe_b64encode(signature).decode("ascii").rstrip("="),
    }


def verify_request(request: Any, settings: Settings) -> None:
    """Fail closed on missing, stale, or modified envelopes in strict environments."""
    identity = request.metadata.get(_FIELD)
    if not isinstance(identity, dict):
        if is_strict_env(settings):
            raise ValueError("missing signed A2A identity envelope")
        return
    issued_at = int(identity.get("issued_at") or 0)
    if abs(int(time.time()) - issued_at) > 120:
        raise ValueError("stale A2A identity envelope")
    raw_kid = identity.get("kid")
    if not isinstance(raw_kid, str) or not raw_kid:
        raise ValueError("A2A envelope key id is unavailable")

    public_key = settings.approval_public_key
    if public_key:
        active_kid = settings.approval_key_id or _key_id(_decode(public_key))
        if not hmac.compare_digest(raw_kid, active_kid):
            public_key = None
    if public_key is None:
        public_key = settings.approval_previous_public_keys.get(raw_kid)
    if not public_key:
        raise ValueError("A2A envelope verification key is unavailable or untrusted")

    signature = _decode(str(identity.get("signature") or ""))
    Ed25519PublicKey.from_public_bytes(_decode(public_key)).verify(
        signature, _canonical(request, issued_at)
    )
