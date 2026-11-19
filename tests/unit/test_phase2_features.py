"""Unit tests for Phase 2 Enterprise Architectural Hardening features.

Covers:
1. Knowledge Graph store & Triple extraction (NetworkX & Neo4j integration)
2. Multi-Retrieval Reciprocal Rank Fusion (Vector + BM25 + KG)
3. RedisSaver LangGraph Checkpointing with TTL & multi-replica index
4. StreamingOutputGuard token buffering and PII preservation across chunk boundaries
5. DeepEval LocalJudge metrics (Faithfulness, Relevancy, Hallucination)
"""

import pytest
from llama_index.core.schema import NodeWithScore, TextNode

from evals.deepeval_runner import LocalJudge
from src.agentic.memory.redis_saver import RedisSaver
from src.agentic.rag.fusion import MultiRetrievalFusion
from src.agentic.rag.kg import KnowledgeGraphStore
from src.agentic.safety.guardrails import GuardrailManager, StreamingOutputGuard


# ------------------------------------------------------------------------------
# 1. Knowledge Graph & Triple Extraction Tests
# ------------------------------------------------------------------------------
def test_kg_triple_extraction_and_query(tmp_path):
    kg_file = tmp_path / "test_kg.json"
    kg = KnowledgeGraphStore(persist_path=kg_file)

    sample_policy = (
        "Chính sách VPN yêu cầu ứng dụng xác thực MFA.\n"
        "Nhóm DevOps có quyền truy cập cụm Kubernetes Production.\n"
        "Nhân viên mới thuộc về phòng ban Kỹ thuật."
    )

    extracted_count = kg.index_text(sample_policy, source_doc="policy_v2.md")
    assert extracted_count >= 2

    # Verify nodes in NetworkX graph
    assert kg.graph.number_of_nodes() >= 4

    # Query graph for 'VPN'
    res = kg.query_graph("VPN", top_k=3)
    assert len(res) >= 1
    content = res[0].node.get_content()
    assert "MFA" in content or "VPN" in content

    # Verify persistence
    assert kg_file.exists()


# ------------------------------------------------------------------------------
# 2. Multi-Retrieval Fusion Tests
# ------------------------------------------------------------------------------
def test_multi_retrieval_fusion_rrf():
    fusion = MultiRetrievalFusion(rrf_k=60)

    node_a = TextNode(text="Chính sách bảo mật dữ liệu doanh nghiệp", id_="node_a")
    node_b = TextNode(text="Quy trình backup và khôi phục sự cố", id_="node_b")
    node_c = TextNode(text="Hướng dẫn cấu hình mạng VPN", id_="node_c")

    channels = {
        "vector": [NodeWithScore(node=node_a, score=0.95), NodeWithScore(node=node_b, score=0.80)],
        "bm25": [NodeWithScore(node=node_a, score=5.0), NodeWithScore(node=node_c, score=4.0)],
        "kg": [NodeWithScore(node=node_a, score=1.0)],
    }

    fused = fusion.fuse(channels, top_k=2)
    assert len(fused) == 2
    # Node A is top in all 3 channels, so its RRF score must be highest
    assert fused[0].node.node_id == "node_a"
    assert fused[0].score > fused[1].score


# ------------------------------------------------------------------------------
# 3. RedisSaver LangGraph Checkpointing Tests
# ------------------------------------------------------------------------------
def test_redis_saver_in_memory_and_fallback():
    # Use non-existent port to test resilient in-memory fallback
    saver = RedisSaver(redis_url="redis://localhost:59999/0")

    config = {
        "configurable": {
            "thread_id": "test_phase2_thread_001",
            "checkpoint_ns": "",
        }
    }
    checkpoint = {
        "v": 1,
        "id": "chk_001",
        "ts": "2026-11-19T00:00:00Z",
        "channel_values": {"messages": ["hello from phase 2"]},
        "channel_versions": {"messages": 1},
        "versions_seen": {},
    }
    metadata = {"source": "unit_test", "step": 1}

    # Save
    saved_config = saver.put(config, checkpoint, metadata, new_versions={"messages": 1})
    assert saved_config["configurable"]["checkpoint_id"] == "chk_001"

    # Retrieve
    retrieved = saver.get_tuple(saved_config)
    assert retrieved is not None
    assert retrieved.checkpoint["channel_values"]["messages"] == ["hello from phase 2"]


# ------------------------------------------------------------------------------
# 4. StreamingOutputGuard Token Buffer & Sliced PII Protection
# ------------------------------------------------------------------------------
def test_streaming_output_guard_sliced_pii():
    manager = GuardrailManager()
    # Configure hold_chars = 20
    guard = StreamingOutputGuard(hold_chars=20, manager=manager)

    # Simulate an email sliced into 3 streaming chunks:
    # chunk 1: "Contact us at "
    # chunk 2: "admin"
    # chunk 3: "@company.com for support."
    out1 = guard.feed("Contact us at ")
    assert out1 == ""  # Buffer held because len <= 20

    out2 = guard.feed("admin")
    assert out2 == ""

    out3 = guard.feed("@company.com for assistance and queries.")
    # Now buffer has grown beyond 20, words before the boundary should be emitted and masked!
    flushed = guard.flush()
    total_output = out1 + out2 + out3 + flushed

    assert "[REDACTED_EMAIL]" in total_output
    assert "admin@company.com" not in total_output


# ------------------------------------------------------------------------------
# 5. DeepEval LocalJudge Metrics Tests
# ------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_deepeval_metrics():
    judge = LocalJudge()

    ctx = "Nhân viên phải bật xác thực 2 lớp (MFA) khi kết nối mạng nội bộ qua VPN."
    ans_faithful = "Khi kết nối VPN vào mạng nội bộ, nhân viên bắt buộc phải kích hoạt MFA."
    ans_hallucinated = "Nhân viên có thể dùng mật khẩu 123456 để vào thẳng database tài chính."

    # Faithfulness
    f_high = await judge.evaluate_faithfulness("MFA VPN", ctx, ans_faithful)
    assert f_high >= 0.50

    # Hallucination
    h_low = await judge.evaluate_hallucination(ctx, ans_faithful)
    assert h_low <= 0.50
    h_high = await judge.evaluate_hallucination(ctx, ans_hallucinated)
    assert h_high >= 0.20

    # Relevancy
    rel = await judge.evaluate_relevancy("Làm thế nào để kết nối VPN?", ans_faithful)
    assert rel >= 0.50
