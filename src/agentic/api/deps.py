"""FastAPI dependency providers with JWT validation and RBAC."""

import logging
from typing import Any, Callable

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
            "token": "",
        }

    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header format. Expected 'Bearer <token>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = authorization.split(" ")[1].strip()

    # In dev/test, support well-known testing tokens
    if settings.environment in ["development", "test"]:
        if token in ["admin_token", "test_token", "secret"]:
            return {"user_id": "test_admin", "role": "admin", "token": token}

    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        user_id = str(payload.get("sub", payload.get("user_id", "authenticated_user")))
        role = str(payload.get("role", "user"))
        return {"user_id": user_id, "role": role, "token": token}
    except jwt.PyJWTError as e:
        if settings.require_authentication:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid or expired token: {str(e)}",
                headers={"WWW-Authenticate": "Bearer"},
            )
        logger.debug(f"JWT decode error ({e}). Falling back to authenticated_user role.")
        return {"user_id": "authenticated_user", "role": "user", "token": token}


def require_role(allowed_roles: list[str]) -> Callable:
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
