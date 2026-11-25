"""API route modules."""

from src.agentic.api.routes.admin import router as admin_router
from src.agentic.api.routes.approvals import router as approvals_router
from src.agentic.api.routes.chat import router as chat_router
from src.agentic.api.routes.health import router as health_router

__all__ = ["admin_router", "approvals_router", "chat_router", "health_router"]
