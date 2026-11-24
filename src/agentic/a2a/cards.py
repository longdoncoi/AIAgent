"""A2A agent-card registry and conversion to the official A2A v1 schema."""

from a2a.types import (
    AgentCapabilities,
    AgentInterface,
    AgentSkill,
    APIKeySecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)
from a2a.types import (
    AgentCard as ProtocolAgentCard,
)
from pydantic import BaseModel, ConfigDict, Field

from src.agentic.core.config import get_settings


class SkillCard(BaseModel):
    """Internal typed description used to construct an A2A ``AgentSkill``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(..., min_length=1, description="Unique skill name")
    description: str = Field(..., min_length=1, description="Description of the capability")


class AgentCard(BaseModel):
    """Configuration-facing card with an official A2A v1 wire representation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(..., min_length=1, description="Agent identifier")
    description: str = Field(..., min_length=1, description="Detailed role and specialty")
    endpoint: str = Field(..., description="A2A JSON-RPC endpoint")
    skills: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    version: str = Field(default="1.0.0", min_length=1)

    def to_protocol_card(self) -> ProtocolAgentCard:
        """Build the standards-compliant protobuf card served on the wire."""
        skills = self.skills or self.capabilities
        security_requirement = SecurityRequirement(
            schemes={"internalServiceToken": StringList(list=[])}
        )
        return ProtocolAgentCard(
            name=self.name,
            description=self.description,
            version=self.version,
            default_input_modes=["text/plain"],
            default_output_modes=["text/plain"],
            capabilities=AgentCapabilities(streaming=True),
            supported_interfaces=[
                AgentInterface(
                    protocol_binding="JSONRPC",
                    protocol_version="1.0",
                    url=self.endpoint,
                )
            ],
            skills=[
                AgentSkill(
                    id=skill,
                    name=skill,
                    description=f"{self.name} capability: {skill.replace('_', ' ')}.",
                    tags=[self.name, skill],
                    input_modes=["text/plain"],
                    output_modes=["text/plain"],
                    security_requirements=[security_requirement],
                )
                for skill in skills
            ],
            security_schemes={
                "internalServiceToken": SecurityScheme(
                    api_key_security_scheme=APIKeySecurityScheme(
                        description="Internal service-to-service authentication token.",
                        location="header",
                        name="X-Internal-Service-Token",
                    )
                )
            },
            security_requirements=[security_requirement],
        )


def _build_cards() -> dict[str, AgentCard]:
    settings = get_settings()
    chat = AgentCard(
        name="chat_agent",
        description="Handles dialogue, summarization, clarification, and general explanations.",
        endpoint=settings.a2a_chat_agent_url,
        skills=["general_chit_chat", "text_summarization", "query_disambiguation"],
    )
    rag = AgentCard(
        name="rag_agent",
        description="Queries enterprise knowledge bases and returns grounded answers.",
        endpoint=settings.a2a_rag_agent_url,
        skills=["enterprise_knowledge_search", "policy_lookup", "grounded_qa"],
    )
    coding = AgentCard(
        name="coding_agent",
        description="Inspects repositories, writes code, executes sandboxed tests, and debugs.",
        endpoint=settings.a2a_coding_agent_url,
        skills=["code_generation", "code_review", "sandbox_execution", "git_repo_inspection"],
    )
    return {
        "chat": chat,
        "chat_agent": chat,
        "rag": rag,
        "rag_agent": rag,
        "coding": coding,
        "coding_agent": coding,
    }


_CARDS = _build_cards()
CHAT_AGENT_CARD = _CARDS["chat"]
RAG_AGENT_CARD = _CARDS["rag"]
CODING_AGENT_CARD = _CARDS["coding"]


def get_agent_card(agent_name: str) -> AgentCard | None:
    """Return the configured internal card for an agent alias."""
    return _CARDS.get(agent_name)
