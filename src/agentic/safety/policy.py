"""Policy Manager reading configs/policies.yaml."""

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from src.agentic.core.config import get_settings
from src.agentic.core.errors import ConfigurationError


class PolicyManager:
    """Manages system policies, risk levels, and tool allowlists."""

    def __init__(self, policy_path: Path | None = None) -> None:
        self.settings = get_settings()
        self.policy_path = policy_path or self.settings.policies_path
        self.policy_data: dict[str, Any] = self._load_policy()

    def _load_policy(self) -> dict[str, Any]:
        if not self.policy_path.exists():
            # Return safe default policy if file not found
            return {
                "tools": {},
                "agent_allowlist": {
                    "chat_agent": ["web_search"],
                    "rag_agent": ["search_policy"],
                    "coding_agent": ["repo_list_files", "repo_read_file", "repo_search_symbol", "code_exec_python"],
                },
                "guardrails": {"max_output_tokens": 4096, "block_prompt_injection": True, "mask_pii": True},
            }
        try:
            with open(self.policy_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                return data or {}
        except Exception as e:
            raise ConfigurationError(f"Failed to parse policy file at {self.policy_path}: {e}") from e

    def get_tool_risk(self, tool_name: str) -> str:
        """Returns risk level: low, medium, or high."""
        tools_cfg = self.policy_data.get("tools", {})
        tool_cfg = tools_cfg.get(tool_name, {})
        return tool_cfg.get("risk_level", "low")

    def is_approval_required(self, tool_name: str) -> bool:
        """Checks if a tool explicitly requires HITL approval."""
        tools_cfg = self.policy_data.get("tools", {})
        tool_cfg = tools_cfg.get(tool_name, {})
        return bool(tool_cfg.get("require_approval", False))

    def is_tool_allowed(self, agent_name: str, tool_name: str) -> bool:
        """Checks if an agent is authorized to use a given tool."""
        return tool_name in self.get_agent_tools(agent_name)

    def get_agent_tools(self, agent_name: str) -> list[str]:
        """Returns the list of allowed tool names for a specific sub-agent (supports 'rag' and 'rag_agent')."""
        allowlists = self.policy_data.get("agent_allowlist", {})
        if agent_name in allowlists:
            return list(allowlists[agent_name])
        norm_key = f"{agent_name}_agent" if not agent_name.endswith("_agent") else agent_name.replace("_agent", "")
        return list(allowlists.get(norm_key, []))

    def get_guardrails_config(self) -> dict[str, Any]:
        """Returns the guardrails configuration section."""
        return self.policy_data.get("guardrails", {})


@lru_cache
def get_policy_manager() -> PolicyManager:
    """Singleton getter for PolicyManager."""
    return PolicyManager()
