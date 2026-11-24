"""FastAPI dependency providers with JWT validation and RBAC."""

import logging
from typing import Any, Callable, cast

import jwt
from fastapi import Depends, Header, HTTPException, Request, status

from src.agentic.core.config import Settings, get_settings
from src.agentic.orchestrator.graph import get_supervisor_graph
from src.agentic.safety.guardrails import GuardrailManager, get_guardrails

logger = logging.getLogger(__name__)


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
            "token": "",
        }

    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header format. Expected 'Bearer <token>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = authorization.split(" ")[1].strip()

    # Strictly restrict testing tokens to test/development environments (strictly blocked in production)
    if settings.environment in ("development", "test"):
        if token in ["admin_token", "test_token", "secret"]:
            return {
                "user_id": "test_admin",
                "role": "admin",
                "roles": ["admin"],
                "department": "engineering",
                "client_id": "test",
                "token": token,
            }

    try:
        # Validate the full claim set at every trust boundary. A token without
        # audience/issuer/expiry is not an authenticated identity.
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options=cast(Any, {"require": ["exp", "iat", "sub", "aud", "iss"]}),
        )
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
        if not department or len(department) > 128 or not department.replace("-", "").replace("_", "").isalnum():
            raise HTTPException(status_code=403, detail="Invalid department claim.")
        normalized_client_id = client_id.replace("-", "").replace("_", "").replace(".", "")
        if not client_id or len(client_id) > 128 or not normalized_client_id.isalnum():
            raise HTTPException(status_code=403, detail="Invalid client_id claim.")
        return {
            "user_id": user_id,
            "role": role,
            "roles": roles,
            "department": department,
            "client_id": client_id,
            "token": token,
        }
    except jwt.PyJWTError as e:
        # Never downgrade an invalid token into an authenticated identity.
        logger.warning("Rejected invalid JWT: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


def require_role(allowed_roles: list[str]) -> Callable[..., Any]:
    """Dependency factory checking user RBAC role."""

    async def _role_checker(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
        user_role = str(user.get("role", "guest")).lower()
        allowed_normalized = [r.lower() for r in allowed_roles]
        if user_role not in allowed_normalized and user_role != "admin":
            logger.warning(
                f"Access denied for user '{user.get('user_id')}' with role '{user_role}'. Required roles: {allowed_roles}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access forbidden: User role '{user_role}' lacks required privileges or permissions ({allowed_roles}).",
            )
        return user

    return _role_checker
