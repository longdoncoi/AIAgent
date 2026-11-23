"""AgentCard / AgentSkill specifications for A2A discovery.

The wire format follows the Google A2A protocol ``AgentCard`` schema so that a
sub-agent can be discovered through ``/.well-known/agent.json``. The legacy
fields (``endpoint`` and string-based ``skills``) are still accepted so that
existing callers and tests keep working.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# Well-known discovery path mandated by the A2A specification.
AGENT_CARD_DISCOVERY_PATH = "/.well-known/agent.json"


class AgentCapabilities(BaseModel):
    """Optional capabilities advertised by an A2A agent."""

    streaming: bool = False
    pushNotifications: bool = False
    stateTransitionHistory: bool = False


class AgentProvider(BaseModel):
    """Organization operating the agent (optional A2A metadata)."""

    organization: str = "Agentic Platform"
    url: str = "https://agentic-platform.local"


class AgentSecurityScheme(BaseModel):
    """OpenAPI-compatible security scheme used by the agent."""

    model_config = ConfigDict(populate_by_name=True)

    type: str = Field(..., description="OpenAPI scheme type: http, apiKey, oauth2, ...")
    description: str | None = None
    scheme: str | None = None
    bearerFormat: str | None = None
    in_: str | None = Field(default=None, alias="in")
    name: str | None = None


class AgentSkill(BaseModel):
    """A2A ``AgentSkill``: a single addressable capability of an agent."""

    id: str = Field(..., description="Unique skill identifier within the agent card")
    name: str = Field(..., description="Human readable skill name")
    description: str = Field(..., description="What this skill does")
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    inputModes: list[str] | None = None
    outputModes: list[str] | None = None


class SkillCard(BaseModel):
    """Legacy skill descriptor kept for backwards compatibility."""

    name: str = Field(..., description="Unique skill name")
    description: str = Field(..., description="Description of the capability")


class SkillList(list[AgentSkill]):
    """List of :class:`AgentSkill` that also accepts legacy string membership.

    Historical callers do ``"general_chit_chat" in card.skills``. The A2A schema
    requires objects instead; this container keeps both behaviours working.
    """

    def __contains__(self, item: object) -> bool:
        if isinstance(item, str):
            return any(skill.id == item or skill.name == item for skill in self)
        return super().__contains__(item)


def _coerce_skill(value: Any) -> Any:
    """Normalise one legacy string / SkillCard into an A2A AgentSkill."""
    if isinstance(value, AgentSkill):
        return value
    if isinstance(value, SkillCard):
        return AgentSkill(
            id=value.name,
            name=value.name.replace("_", " ").title(),
            description=value.description,
            tags=[value.name],
        )
    if isinstance(value, str):
        readable = value.replace("_", " ").strip().title()
        return AgentSkill(
            id=value,
            name=readable or value,
            description=f"Capability: {readable or value}",
            tags=[value],
        )
    if isinstance(value, dict):
        data = dict(value)
        data.setdefault("id", data.get("name", ""))
        data.setdefault("tags", [])
        data.setdefault("examples", [])
        return data
    return value


def _coerce_skills(value: Any) -> Any:
    """Field-level validator: accept a single skill or any iterable of skills."""
    if value is None:
        return []
    if isinstance(value, (str, AgentSkill, SkillCard, dict)):
        return [_coerce_skill(value)]
    if isinstance(value, (list, tuple, set)):
        return [_coerce_skill(item) for item in value]
    return value


Skills = Annotated[list[AgentSkill], BeforeValidator(_coerce_skills), AfterValidator(SkillList)]


_INTERNAL_SECURITY_SCHEME: dict[str, AgentSecurityScheme] = {
    "internalServiceToken": AgentSecurityScheme.model_validate(
        {
            "type": "apiKey",
            "name": "X-Internal-Service-Token",
            "in": "header",
            "description": "Service-to-service token issued to the Supervisor.",
        }
    )
}


def _default_security() -> list[dict[str, list[str]]]:
    return [{"internalServiceToken": []}]


def _legacy_skills(*names: str) -> list[AgentSkill]:
    """Build A2A skills from the legacy string skill names."""
    return [_coerce_skill(name) for name in names]


class AgentCard(BaseModel):
    """A2A-compliant discovery card for one sub-agent."""

    name: str = Field(..., description="Agent identifier")
    description: str = Field(..., description="Detailed role and specialty of the agent")
    endpoint: str = Field(default="", description="Legacy alias for the A2A base URL")
    url: str = Field(default="", description="Base URL where the agent serves A2A requests")
    version: str = Field(default="1.0.0")
    documentationUrl: str | None = None
    provider: AgentProvider = Field(default_factory=AgentProvider)
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    defaultInputModes: list[str] = Field(
        default_factory=lambda: ["text/plain", "application/json"]
    )
    defaultOutputModes: list[str] = Field(
        default_factory=lambda: ["text/plain", "application/json"]
    )
    skills: Skills = Field(default_factory=SkillList)
    securitySchemes: dict[str, AgentSecurityScheme] = Field(
        default_factory=lambda: dict(_INTERNAL_SECURITY_SCHEME)
    )
    security: list[dict[str, list[str]]] = Field(default_factory=_default_security)
    supportsAuthenticatedExtendedCard: bool = False

    @field_validator("capabilities", mode="before")
    @classmethod
    def _coerce_capabilities(cls, value: Any) -> Any:
        """Accept the legacy ``capabilities=['streaming']`` / ``[]`` form."""
        if isinstance(value, (list, tuple, set)):
            flags = {str(item).lower() for item in value}
            return AgentCapabilities(
                streaming=bool(flags & {"streaming", "stream"}),
                pushNotifications=bool(flags & {"push", "pushnotifications"}),
                stateTransitionHistory=bool(flags & {"history", "statetransitionhistory"}),
            )
        return value

    @model_validator(mode="after")
    def _sync_endpoint_and_url(self) -> AgentCard:
        """Keep the legacy ``endpoint`` and the spec ``url`` in sync."""
        if not self.url and self.endpoint:
            self.url = self.endpoint
        if not self.endpoint and self.url:
            self.endpoint = self.url
        return self


_STREAMING_CAPABILITIES = AgentCapabilities(streaming=True)

CHAT_AGENT_CARD = AgentCard(
    name="chat_agent",
    description="Handles natural language dialogue, summarization, query clarification, and general explanations.",
    endpoint="http://localhost:9100/a2a",
    capabilities=_STREAMING_CAPABILITIES,
    skills=_legacy_skills("general_chit_chat", "text_summarization", "query_disambiguation"),
)

RAG_AGENT_CARD = AgentCard(
    name="rag_agent",
    description="Queries enterprise knowledge bases, technical policies, and documentation to deliver grounded QA.",
    endpoint="http://localhost:9101/a2a",
    capabilities=_STREAMING_CAPABILITIES,
    skills=_legacy_skills("enterprise_knowledge_search", "policy_lookup", "grounded_qa"),
)

CODING_AGENT_CARD = AgentCard(
    name="coding_agent",
    description="Inspects source code repositories, writes programs, executes test scripts in sandbox, and debugs.",
    endpoint="http://localhost:9102/a2a",
    capabilities=_STREAMING_CAPABILITIES,
    skills=_legacy_skills(
        "code_generation", "code_review", "sandbox_execution", "git_repo_inspection"
    ),
)

_CARDS: dict[str, AgentCard] = {
    "chat": CHAT_AGENT_CARD,
    "chat_agent": CHAT_AGENT_CARD,
    "rag": RAG_AGENT_CARD,
    "rag_agent": RAG_AGENT_CARD,
    "coding": CODING_AGENT_CARD,
    "coding_agent": CODING_AGENT_CARD,
}


def get_agent_card(agent_name: str) -> AgentCard | None:
    """Retrieves the AgentCard for a given agent name."""
    return _CARDS.get(agent_name)


def agent_card_discovery_document(card: AgentCard) -> dict[str, Any]:
    """Build the JSON document served at ``/.well-known/agent.json``."""
    document: dict[str, Any] = card.model_dump(mode="json", by_alias=True, exclude_none=True)
    document.setdefault("url", card.endpoint or card.url)
    return document


def add_agent_card_discovery_route(app: "FastAPI", card: AgentCard) -> None:
    """Register ``GET /.well-known/agent.json`` on an existing A2A FastAPI app.

    The live route currently lives in ``a2a/executor.py`` which is owned by
    another stream. Once that file is free, call this helper from
    ``create_a2a_server`` (right after the ``/a2a/card`` route) to expose the
    standard A2A discovery endpoint.
    """

    @app.get(AGENT_CARD_DISCOVERY_PATH, include_in_schema=True)
    async def _well_known_agent_json() -> dict[str, Any]:
        return agent_card_discovery_document(card)
