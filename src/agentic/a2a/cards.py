"""AgentCard and SkillCard specifications for A2A discovery."""

from pydantic import BaseModel, Field


class SkillCard(BaseModel):
    name: str = Field(..., description="Unique skill name")
    description: str = Field(..., description="Description of the capability")


class AgentCard(BaseModel):
    name: str = Field(..., description="Agent identifier")
    description: str = Field(..., description="Detailed role and specialty of the agent")
    endpoint: str = Field(..., description="URL endpoint where the agent serves A2A requests")
    skills: list[str] = Field(
        default_factory=list, description="List of skills supported by the agent"
    )
    version: str = Field(default="1.0.0")


CHAT_AGENT_CARD = AgentCard(
    name="chat_agent",
    description="Handles natural language dialogue, summarization, query clarification, and general explanations.",
    endpoint="http://localhost:9100/a2a",
    skills=["general_chit_chat", "text_summarization", "query_disambiguation"],
)

RAG_AGENT_CARD = AgentCard(
    name="rag_agent",
    description="Queries enterprise knowledge bases, technical policies, and documentation to deliver grounded QA.",
    endpoint="http://localhost:9101/a2a",
    skills=["enterprise_knowledge_search", "policy_lookup", "grounded_qa"],
)

CODING_AGENT_CARD = AgentCard(
    name="coding_agent",
    description="Inspects source code repositories, writes programs, executes test scripts in sandbox, and debugs.",
    endpoint="http://localhost:9102/a2a",
    skills=["code_generation", "code_review", "sandbox_execution", "git_repo_inspection"],
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
