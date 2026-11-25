"""Unit tests for the Planner Node."""

import pytest
from langchain_core.messages import HumanMessage

from src.agentic.orchestrator.nodes.planner import planner_node
from src.agentic.orchestrator.state import AgentState


@pytest.mark.asyncio
async def test_planner_routing():
    # Chat query
    chat_state = AgentState(
        messages=[HumanMessage(content="Xin chào bạn, hôm nay thời tiết thế nào?")]
    )
    chat_res = await planner_node(chat_state)
    assert chat_res["plan"] is not None
    assert chat_res["plan"].steps[0].agent == "chat"
    assert chat_res["plan"].steps[0].risk_level == "low"

    # Coding query
    code_state = AgentState(
        messages=[HumanMessage(content="Viết một script Python tính căn bậc hai")]
    )
    code_res = await planner_node(code_state)
    assert code_res["plan"] is not None
    assert code_res["plan"].steps[0].agent == "coding"

    # High-risk coding query
    risky_state = AgentState(
        messages=[HumanMessage(content="Ghi đè file script.py và xoá các hàm cũ")]
    )
    risky_res = await planner_node(risky_state)
    assert risky_res["plan"] is not None
    assert risky_res["plan"].steps[0].risk_level == "high"

    # RAG policy query
    rag_state = AgentState(
        messages=[HumanMessage(content="Tra cứu quy định chính sách bảo mật nội bộ")]
    )
    rag_res = await planner_node(rag_state)
    assert rag_res["plan"] is not None
    assert rag_res["plan"].steps[0].agent == "rag"
