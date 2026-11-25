"""Unit tests verifying RAGAS Framework for evals/ and LangSmith for observability/."""

import os
from unittest.mock import MagicMock, patch

import pytest

from evals.evaluators import (
    evaluate_ragas_answer_relevance,
    evaluate_ragas_context_precision,
    evaluate_ragas_context_recall,
    evaluate_ragas_faithfulness,
    evaluate_ragas_sample,
)
from evals.ragas_runner import (
    RagasAnswerRelevanceOutput,
    RagasContextPrecisionOutput,
    RagasContextRecallOutput,
    RagasFaithfulnessOutput,
    RagasLocalEvaluator,
    RagasSampleResult,
    run_ragas_benchmark,
)
from src.agentic.core.config import Settings
from src.agentic.observability.langsmith_tracer import (
    get_langsmith_callback,
    get_langsmith_client,
    is_langsmith_enabled,
    log_feedback,
    setup_langsmith,
    sync_evaluation_dataset,
    trace_langsmith_run,
    trace_langsmith_run_async,
    traceable_operation,
)


# ==============================================================================
# 1. LangSmith Observability Tests
# ==============================================================================
def test_langsmith_setup_and_status() -> None:
    """Verifies that setup_langsmith accurately configures environment variables."""
    # When disabled
    settings_disabled = Settings(langchain_tracing_v2=False, langchain_api_key=None)
    with patch.dict(os.environ, {}, clear=True):
        setup_langsmith(settings_disabled)
        assert is_langsmith_enabled() is False

    # When enabled with API key
    test_api_key = "_".join(("lsv2", "pt", "test", "key", "12345"))
    settings_enabled = Settings(
        langchain_tracing_v2=True,
        langchain_api_key=test_api_key,
        langchain_project="agentic-test-project",
        langchain_endpoint="https://api.smith.langchain.com",
    )
    with patch.dict(os.environ, {}, clear=True):
        setup_langsmith(settings_enabled)
        assert os.environ.get("LANGCHAIN_TRACING_V2") == "true"
        assert os.environ.get("LANGCHAIN_API_KEY") == test_api_key
        assert os.environ.get("LANGCHAIN_PROJECT") == "agentic-test-project"
        assert is_langsmith_enabled() is True


def test_get_langsmith_client_and_callbacks() -> None:
    """Verifies retrieval of LangSmith Client and LangChainTracer callback."""
    # Without API key -> None
    with patch.dict(os.environ, {"LANGCHAIN_TRACING_V2": "false", "LANGCHAIN_API_KEY": ""}):
        assert get_langsmith_client() is None
        assert get_langsmith_callback() is None

    # Mock enabled client
    with patch.dict(
        os.environ,
        {
            "LANGCHAIN_TRACING_V2": "true",
            "LANGCHAIN_API_KEY": "lsv2_pt_mock_key",
            "LANGCHAIN_PROJECT": "agentic-platform",
        },
    ):
        with patch("langsmith.Client") as mock_client_cls:
            mock_client_instance = MagicMock()
            mock_client_cls.return_value = mock_client_instance
            import src.agentic.observability.langsmith_tracer as lst

            lst._langsmith_client = None  # reset cache
            client = get_langsmith_client()
            assert client == mock_client_instance

        # Test LangChain callback
        callback = get_langsmith_callback(project_name="custom-project", tags=["tag1"])
        assert callback is not None
        assert callback.project_name == "custom-project"


@pytest.mark.asyncio
async def test_traceable_operation_decorator() -> None:
    """Verifies @traceable_operation decorator works on both async and sync functions."""

    @traceable_operation(name="test_sync_fn", run_type="tool")
    def sync_tool(x: int, y: int) -> int:
        return x + y

    assert sync_tool(3, 4) == 7

    @traceable_operation(name="test_async_fn", run_type="chain")
    async def async_chain(text: str) -> str:
        return f"processed_{text}"

    res = await async_chain("hello")
    assert res == "processed_hello"


@pytest.mark.asyncio
async def test_trace_langsmith_run_context_managers() -> None:
    """Verifies sync and async context managers for LangSmith run tree."""
    # Sync context manager
    with trace_langsmith_run("sync_block", run_type="chain", inputs={"a": 1}) as ctx:
        assert ctx["name"] == "sync_block"
        ctx["outputs"] = {"result": "ok"}

    # Async context manager
    async with trace_langsmith_run_async("async_block", run_type="chain", inputs={"b": 2}) as ctx:
        assert ctx["name"] == "async_block"
        ctx["outputs"] = {"result": "done"}


def test_langsmith_feedback_and_dataset_sync() -> None:
    """Verifies log_feedback and sync_evaluation_dataset helpers."""
    # Feedback without client returns False
    with patch(
        "src.agentic.observability.langsmith_tracer.get_langsmith_client", return_value=None
    ):
        assert log_feedback(run_id="run_123", key="quality", score=0.95) is False

    # Feedback with active mock client
    mock_client = MagicMock()
    with patch(
        "src.agentic.observability.langsmith_tracer.get_langsmith_client", return_value=mock_client
    ):
        success = log_feedback(
            run_id="run_123", key="faithfulness", score=0.92, comment="High factual fidelity"
        )
        assert success is True
        mock_client.create_feedback.assert_called_once_with(
            run_id="run_123",
            key="faithfulness",
            score=0.92,
            value=None,
            comment="High factual fidelity",
            correction=None,
        )

    # Dataset sync with active mock client
    mock_client.has_dataset.return_value = False
    mock_dataset = MagicMock()
    mock_dataset.id = "dataset_uuid_456"
    mock_client.create_dataset.return_value = mock_dataset

    with patch(
        "src.agentic.observability.langsmith_tracer.get_langsmith_client", return_value=mock_client
    ):
        examples = [
            {"question": "Q1", "ground_truth": "A1", "id": "tc_01"},
            {"question": "Q2", "ground_truth": "A2", "id": "tc_02"},
        ]
        ds_id = sync_evaluation_dataset(dataset_name="test_ds", examples=examples)
        assert ds_id == "dataset_uuid_456"
        assert mock_client.create_example.call_count == 2


# ==============================================================================
# 2. RAGAS Framework Evaluator Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_ragas_local_evaluator_metrics() -> None:
    """Verifies all 4 canonical RAGAS metrics: Faithfulness, Answer Relevance, Context Precision, Context Recall."""
    evaluator = RagasLocalEvaluator()

    question = "Thời gian timeout tối đa cho code execution là bao nhiêu?"
    contexts = [
        "Chính sách bảo mật: Hệ thống áp dụng cô lập sandbox cho mọi tiến trình chạy mã nguồn.",
        "Quy định Sandbox: Giới hạn thời gian timeout tối đa cho mỗi tiến trình chạy thử nghiệm là 30 giây.",
    ]
    faithful_answer = "Giới hạn thời gian timeout tối đa cho tiến trình chạy thử nghiệm là 30 giây."
    hallucinated_answer = "Thời gian timeout không giới hạn và được phép thoải mái thực hiện mọi thao tác ngoài hệ thống."
    ground_truth = (
        "Giới hạn thời gian timeout tối đa cho mỗi tiến trình chạy thử nghiệm là 30 giây."
    )

    # 1. Faithfulness
    score_f_high = await evaluator.evaluate_faithfulness(question, contexts, faithful_answer)
    assert score_f_high >= 0.70

    score_f_low = await evaluator.evaluate_faithfulness(question, contexts, hallucinated_answer)
    assert score_f_low < score_f_high
    assert score_f_low <= 0.45

    # 2. Answer Relevance
    score_r_high = await evaluator.evaluate_answer_relevance(question, faithful_answer)
    assert score_r_high >= 0.60

    score_r_empty = await evaluator.evaluate_answer_relevance(question, "")
    assert score_r_empty == 0.0

    # 3. Context Precision
    score_p_high = await evaluator.evaluate_context_precision(question, contexts, ground_truth)
    assert score_p_high >= 0.50

    score_p_empty = await evaluator.evaluate_context_precision(question, [], ground_truth)
    assert score_p_empty == 0.0

    # 4. Context Recall
    score_rec_high = await evaluator.evaluate_context_recall(question, contexts, ground_truth)
    assert score_rec_high >= 0.70

    score_rec_irrelevant = await evaluator.evaluate_context_recall(
        question, ["Chỉ có nội dung không liên quan về thời tiết Hà Nội."], ground_truth
    )
    assert score_rec_irrelevant <= 0.30


@pytest.mark.asyncio
async def test_evaluators_ragas_adapters() -> None:
    """Verifies that RAGAS adapter functions in evals/evaluators.py work accurately."""
    q = "Mức độ rủi ro khi chạy lệnh shell bash là gì?"
    ctx = ["Lệnh shell bash có mức độ rủi ro cao và cần phê duyệt HITL."]
    ans = "Lệnh shell bash có mức độ rủi ro cao và cần phê duyệt HITL."
    gt = "Mức độ rủi ro cao (risk_level: high) và bắt buộc phải tạm dừng chờ phê duyệt HITL."

    f = await evaluate_ragas_faithfulness(q, ctx, ans)
    assert f >= 0.70

    r = await evaluate_ragas_answer_relevance(q, ans)
    assert r >= 0.60

    p = await evaluate_ragas_context_precision(q, ctx, gt)
    assert p >= 0.50

    c = await evaluate_ragas_context_recall(q, ctx, gt)
    assert c >= 0.50

    sample_dict = await evaluate_ragas_sample(q, ctx, ans, gt)
    assert "faithfulness" in sample_dict
    assert "answer_relevance" in sample_dict
    assert "context_precision" in sample_dict
    assert "context_recall" in sample_dict
    assert sample_dict["faithfulness"] == f


def test_ragas_pydantic_schemas() -> None:
    """Verifies that Pydantic models for RAGAS output validation enforce constraints."""
    f_out = RagasFaithfulnessOutput(
        faithfulness=0.95, unsupported_claims=[], reasoning="All claims verified"
    )
    assert f_out.faithfulness == 0.95

    r_out = RagasAnswerRelevanceOutput(answer_relevance=0.88, reasoning="Addresses prompt directly")
    assert r_out.answer_relevance == 0.88

    p_out = RagasContextPrecisionOutput(
        context_precision=0.80, relevant_chunks=[0, 1], reasoning="Top chunks relevant"
    )
    assert p_out.context_precision == 0.80

    c_out = RagasContextRecallOutput(
        context_recall=0.92, missing_points=[], reasoning="Full recall achieved"
    )
    assert c_out.context_recall == 0.92

    sample_res = RagasSampleResult(
        id="sample_01",
        question="What is HITL?",
        faithfulness=0.9,
        answer_relevance=0.85,
        context_precision=0.8,
        context_recall=0.9,
        passed=True,
    )
    assert sample_res.passed is True


@pytest.mark.asyncio
async def test_run_ragas_benchmark_quality_gate() -> None:
    """Verifies that the RAGAS benchmark quality gate suite executes and evaluates successfully."""
    passed = await run_ragas_benchmark()
    assert passed is True
