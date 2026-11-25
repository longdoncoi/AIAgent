"""FastAPI dependency providers with JWT/OIDC validation, revocation check, and RBAC."""

import asyncio
import logging
from collections.abc import Callable
from contextvars import ContextVar
from functools import lru_cache
from typing import Any, cast

import jwt
from fastapi import Depends, Header, HTTPException, Request, status

from src.agentic.core.config import Settings, get_settings, is_strict_env
from src.agentic.orchestrator.graph import get_supervisor_graph
from src.agentic.safety.guardrails import GuardrailManager, get_guardrails

logger = logging.getLogger(__name__)
_VERIFIED_JWT_CTX: ContextVar[tuple[str, dict[str, Any]] | None] = ContextVar(
    "agentic_verified_jwt_payload", default=None
)


class TokenRevocationUnavailable(RuntimeError):
    """Raised in production when JWT JTI revocation store cannot be reached."""


def set_verified_jwt_context(token: str, payload: dict[str, Any]) -> object:
    """Cache a verified JWT payload for the active HTTP request lifecycle."""
    return _VERIFIED_JWT_CTX.set((token, payload))


def reset_verified_jwt_context(ctx_token: object) -> None:
    """Reset per-request verified JWT cache."""
    _VERIFIED_JWT_CTX.reset(ctx_token)  # type: ignore[arg-type]


@lru_cache(maxsize=8)
def _get_jwks_client(jwks_url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=300)


def _is_jti_revoked(jti: str, settings: Settings) -> bool:
    """Check optional Redis revocation list for a JWT ID (`jti`).

    Fails closed in production when revocation enforcement is enabled and Redis
    is unavailable or raises an error.
    """
    if not jti or not settings.jwt_revocation_check or settings.environment == "test":
        return False
    try:
        from src.agentic.core.redis import get_redis_client

        client = get_redis_client(settings.redis_state_url or settings.redis_url)
        if client is None:
            if is_strict_env(settings):
                raise TokenRevocationUnavailable("JWT revocation Redis store unavailable.")
            return False
        return bool(client.exists(f"agentic:jwt:revoked:{jti}"))
    except TokenRevocationUnavailable:
        raise
    except Exception as exc:
        if is_strict_env(settings):
            raise TokenRevocationUnavailable(
                f"JWT revocation check failed against Redis store: {exc}"
            ) from exc
        return False


def decode_bearer_jwt(token: str, settings: Settings) -> dict[str, Any]:
    """Verify a Bearer JWT via OIDC JWKS (when configured) or shared secret."""
    if settings.jwt_jwks_url:
        signing_key = _get_jwks_client(settings.jwt_jwks_url).get_signing_key_from_jwt(token).key
        algorithms = list(settings.jwt_allowed_algorithms or ["RS256", "ES256"])
    else:
        signing_key = settings.jwt_secret_key
        algorithms = [settings.jwt_algorithm]

    payload = jwt.decode(
        token,
        signing_key,
        algorithms=algorithms,
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
        options=cast(Any, {"require": ["exp", "iat", "sub", "aud", "iss"]}),
    )
    jti = payload.get("jti")
    if isinstance(jti, str) and _is_jti_revoked(jti, settings):
        raise jwt.InvalidTokenError("Token has been revoked.")
    return payload


async def decode_bearer_jwt_async(token: str, settings: Settings) -> dict[str, Any]:
    """Async non-blocking wrapper offloading JWKS HTTP and Redis JTI checks."""
    return await asyncio.to_thread(decode_bearer_jwt, token, settings)


async def get_settings_dep() -> Settings:
    """Provides application settings."""
    return get_settings()


async def get_guardrails_dep() -> GuardrailManager:
    """Provides guardrails manager."""
    return get_guardrails()


async def get_graph(request: Request) -> Any:
    """Retrieves compiled Supervisor graph from application state or singleton."""
    if hasattr(request.app.state, "graph") and request.app.state.graph is not None:
        return request.app.state.graph
    return get_supervisor_graph()


async def get_current_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """Authenticates the incoming request token using JWT or secure fallback."""
    settings = get_settings()

    if not authorization:
        if settings.require_authentication:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication credentials were not provided.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return {
            "user_id": "anonymous_user",
            "role": "admin" if settings.environment == "test" else "guest",
            "roles": ["admin"] if settings.environment == "test" else ["guest"],
            "department": "public",
            "client_id": "anonymous",
            "tenant_id": settings.rag_default_tenant_id,
            "token": "",
        }

    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header format. Expected 'Bearer <token>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = authorization.split(" ")[1].strip()

    # Strictly restrict hardcoded testing tokens to test environments only
    if settings.environment == "test" and token in ["admin_token", "test_token", "secret"]:
        return {
            "user_id": "test_admin",
            "role": "admin",
            "roles": ["admin"],
            "department": "engineering",
            "client_id": "test",
            "tenant_id": settings.rag_default_tenant_id,
            "token": token,
        }

    try:
        # Reuse request-scoped verified payload from middleware when available so
        # JWKS/Redis JTI checks execute at most once per HTTP request off-loop.
        cached_ctx = _VERIFIED_JWT_CTX.get()
        if cached_ctx is not None and cached_ctx[0] == token:
            payload = cached_ctx[1]
        else:
            payload = await decode_bearer_jwt_async(token, settings)
        user_id = str(payload.get("sub") or payload.get("user_id") or "authenticated_user")
        role = str(payload.get("role", "user")).lower()
        raw_roles = payload.get("roles", [role])
        if isinstance(raw_roles, str):
            raw_roles = [raw_roles]
        if not isinstance(raw_roles, list):
            raise HTTPException(status_code=403, detail="Invalid roles claim.")
        roles = sorted({str(item).lower() for item in raw_roles} | {role})
        allowed_roles = {"admin", "supervisor", "operator", "user", "guest"}
        if role not in allowed_roles or not set(roles).issubset(allowed_roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Unrecognized role claim.",
            )
        department = str(payload.get("department") or "public").strip().lower()
        # Cost allocation must use a bounded tenant/application identifier, not
        # fall back to the per-user subject (which would create unbounded metric
        # cardinality and misattribute spend).
        client_id = str(
            payload.get("client_id")
            or payload.get("tenant_id")
            or payload.get("azp")
            or "unattributed"
        ).strip().lower()
        tenant_id = str(
            payload.get("tenant_id")
            or payload.get("org_id")
            or settings.rag_default_tenant_id
        ).strip().lower()
        if not department or len(department) > 128 or not department.replace("-", "").replace("_", "").isalnum():
            raise HTTPException(status_code=403, detail="Invalid department claim.")
        normalized_client_id = client_id.replace("-", "").replace("_", "").replace(".", "")
        if not client_id or len(client_id) > 128 or not normalized_client_id.isalnum():
            raise HTTPException(status_code=403, detail="Invalid client_id claim.")
        normalized_tenant_id = tenant_id.replace("-", "").replace("_", "").replace(".", "")
        if not tenant_id or len(tenant_id) > 128 or not normalized_tenant_id.isalnum():
            raise HTTPException(status_code=403, detail="Invalid tenant_id claim.")
        return {
            "user_id": user_id,
            "role": role,
            "roles": roles,
            "department": department,
            "client_id": client_id,
            "tenant_id": tenant_id,
            "token": token,
        }
    except TokenRevocationUnavailable as e:
        logger.error("JWT revocation store unavailable: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication revocation service unavailable.",
        ) from e
    except jwt.PyJWTError as e:
        # Never downgrade an invalid token into an authenticated identity.
        logger.warning("Rejected invalid JWT: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


def require_role(allowed_roles: list[str]) -> Callable[..., Any]:
    """Dependency factory checking user RBAC role(s)."""

    async def _role_checker(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
        allowed_normalized = {r.lower() for r in allowed_roles}
        user_roles: list[str] = []
        if user.get("role"):
            user_roles.append(str(user["role"]).lower())
        if isinstance(user.get("roles"), (list, tuple, set)):
            user_roles.extend(str(role).lower() for role in user["roles"] if role)
        if not user_roles:
            user_roles = ["guest"]
        if "admin" not in user_roles and not any(role in allowed_normalized for role in user_roles):
            logger.warning(
                f"Access denied for user '{user.get('user_id')}' with roles {user_roles}. Required roles: {allowed_roles}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access forbidden: User role(s) {user_roles} lacks required privileges or permissions ({allowed_roles}).",
            )
        return user

    return _role_checker
