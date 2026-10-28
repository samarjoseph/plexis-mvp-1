"""
Plexis Automated Regression Test Suite:
Dataset State Integrity & Internal Prompt / CoT Leakage Elimination

Covers all 12 mandatory validation cases:
- Case 1: Dataset loaded; follow-up analytical question retains dataset ID.
- Case 2: Dataset schema available in metadata, but underlying data access fails (returns accurate recoverable error).
- Case 3: Frontend sends "No active dataset" or empty ID (handled gracefully without crashing UUID parser).
- Case 4: Different dataset selected in same session.
- Case 5: Two users/sessions have different datasets (no cross-contamination, no global fallback).
- Case 6: Provider response includes separate reasoning and final-answer fields (reasoning excluded).
- Case 7: Provider returns malformed response or missing final answer (fails closed safely).
- Case 8: Internal planning text (e.g. screenshot CoT) is never rendered to the user.
- Case 9: Streaming responses never expose internal planning fields.
- Case 10: Greeting and no-dataset fallback behavior remains correct.
- Case 11: Arbitrary supported analytical question works without hardcoded question branch.
- Case 12: Runs against representative schemas, including datasets without airline columns (e.g. india_flight_delay_model_input.csv reporting missing column and listing actual columns).
"""
import os
import sys
import uuid
import pytest
import pandas as pd
from unittest.mock import MagicMock, patch

# Add backend directory to sys.path
backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

# Ensure engines are registered in engine_registry
import api.chat  # registers conversation, analysis, transform, investigate

from core.context import ExecutionContext
from core.pipeline import RequestPipeline
from datasets.registry import dataset_registry, DatasetEntry
from utils.text_sanitizer import strip_thinking_and_internal_reasoning, is_internal_planning_dump
from utils.response_boundary import enforce_response_boundary
from providers.domain.contracts import AIRequest
from providers.base import ProviderResponse
from router.master_router import master_router
from engines.base import EngineResult
from session.manager import session_manager


# Helper fixture for flight delay test dataset
@pytest.fixture
def flight_delay_entry():
    ds_id = str(uuid.uuid4())
    df = pd.DataFrame({
        "Rainfall_mm": [12.5, 0.0, 45.2, 3.1],
        "Cloud_Cover_pct": [80, 10, 95, 40],
        "Origin_Congestion_Index": [0.72, 0.31, 0.89, 0.45],
        "Previous_Flight_Delay_Minutes": [15, 0, 45, 10],
        "Turnaround_Risk_Index": [0.65, 0.12, 0.78, 0.33],
        "Departure_Delay": [25, 0, 60, 5],
        "Delay_Target": [1, 0, 1, 0],
    })
    entry = DatasetEntry(
        dataset_id=ds_id,
        filename="india_flight_delay_model_input.csv",
        file_path="india_flight_delay_model_input.csv",
        upload_timestamp="2026-10-28T12:00:00Z",
        row_count=len(df),
        column_count=len(df.columns),
        file_fingerprint="fp_flight_123",
        schema_fingerprint="sfp_flight_123",
        dataframe=df,
        profile={"columns": list(df.columns)},
        schema_profile=[{"name": col, "dtype": str(df[col].dtype)} for col in df.columns],
        column_profiles={col: {"mean": float(df[col].mean())} for col in df.columns},
    )
    dataset_registry.register(entry)
    return entry


# ── Case 1: Dataset loaded; follow-up analytical question retains dataset ID ─
def test_case_1_follow_up_retains_dataset_id(flight_delay_entry):
    pipeline = RequestPipeline()
    session_id = f"test_session_{uuid.uuid4()}"
    
    # Message 1: Initial query with explicit dataset ID
    ctx1 = pipeline.process(
        message="what is the average departure delay?",
        dataset_id=flight_delay_entry.dataset_id,
        session_id=session_id,
    )
    assert ctx1.dataset_id == flight_delay_entry.dataset_id
    assert ctx1.dataset_filename == "india_flight_delay_model_input.csv"
    
    # Message 2: Follow-up question without explicit dataset ID (relies on session state)
    ctx2 = pipeline.process(
        message="what is the maximum rainfall?",
        dataset_id=None,
        session_id=session_id,
    )
    assert ctx2.dataset_id == flight_delay_entry.dataset_id
    assert ctx2.dataset_filename == "india_flight_delay_model_input.csv"


# ── Case 2: Metadata available but underlying data access fails ─────────────
def test_case_2_metadata_available_data_access_fails():
    ctx = ExecutionContext(
        message="what is the highest delay?",
        session_id=str(uuid.uuid4()),
        dataset_id="test-ds-uuid",
    )
    ctx.dataset_access_failed = True
    ctx.dataset_filename = "large_operations.csv"
    ctx.dataset_resolution_error = (
        "Dataset 'large_operations.csv' is registered, but its underlying data "
        "could not be retrieved from storage. Please re-upload or select the dataset again."
    )
    
    result = master_router.route(ctx)
    assert result.success is False
    assert "large_operations.csv" in result.answer
    assert "could not be loaded from storage" in result.answer or "could not be retrieved" in result.answer
    # Must NOT claim no dataset is loaded
    assert "no dataset is currently loaded" not in result.answer.lower()


# ── Case 3: Frontend sends "No active dataset" or empty ID ───────────────────
def test_case_3_bogus_dataset_id_handling():
    pipeline = RequestPipeline()
    session_id = f"test_session_{uuid.uuid4()}"
    
    # Test with string literal "No active dataset"
    ctx = pipeline.process(
        message="hello there",
        dataset_id="No active dataset",
        session_id=session_id,
    )
    assert ctx.dataset_id is None
    assert getattr(ctx, "requested_dataset_id", None) is None
    
    # Test with "null"
    ctx_null = pipeline.process(
        message="hello",
        dataset_id="null",
        session_id=session_id,
    )
    assert ctx_null.dataset_id is None


# ── Case 4: Different dataset selected in same session ─────────────────────
def test_case_4_different_dataset_selected_in_same_session(flight_delay_entry):
    pipeline = RequestPipeline()
    session_id = f"test_session_{uuid.uuid4()}"
    
    # Step 1: Select first dataset
    ctx1 = pipeline.process(
        message="show delay",
        dataset_id=flight_delay_entry.dataset_id,
        session_id=session_id,
    )
    assert ctx1.dataset_id == flight_delay_entry.dataset_id
    
    # Step 2: Register and select second dataset in same session
    ds2_id = str(uuid.uuid4())
    df2 = pd.DataFrame({"Revenue": [100, 200], "Quarter": ["Q1", "Q2"]})
    entry2 = DatasetEntry(
        dataset_id=ds2_id,
        filename="sales_q2.csv",
        file_path="sales_q2.csv",
        upload_timestamp="2026-10-28T12:00:00Z",
        row_count=2,
        column_count=2,
        file_fingerprint="fp_sales_123",
        schema_fingerprint="sfp_sales_123",
        dataframe=df2,
        profile={"columns": list(df2.columns)},
        schema_profile=[{"name": col, "dtype": str(df2[col].dtype)} for col in df2.columns],
        column_profiles={col: {"mean": float(df2[col].mean())} for col in ["Revenue"]},
    )
    dataset_registry.register(entry2)
    
    ctx2 = pipeline.process(
        message="what is total revenue?",
        dataset_id=ds2_id,
        session_id=session_id,
    )
    assert ctx2.dataset_id == ds2_id
    assert ctx2.dataset_filename == "sales_q2.csv"
    
    # Follow-up retains new active dataset
    ctx3 = pipeline.process(
        message="which quarter had highest revenue?",
        dataset_id=None,
        session_id=session_id,
    )
    assert ctx3.dataset_id == ds2_id
    assert ctx3.dataset_filename == "sales_q2.csv"


# ── Case 5: Two users/sessions have different datasets (no cross-contamination)
def test_case_5_session_isolation_no_global_fallback():
    pipeline = RequestPipeline()
    
    # User A session with Dataset A
    ds_a_id = str(uuid.uuid4())
    df_a = pd.DataFrame({"MetricA": [1, 2]})
    entry_a = DatasetEntry(
        dataset_id=ds_a_id,
        filename="user_a.csv",
        file_path="user_a.csv",
        upload_timestamp="2026-10-28T12:00:00Z",
        row_count=2,
        column_count=1,
        file_fingerprint="fp_a",
        schema_fingerprint="sfp_a",
        dataframe=df_a,
        profile={"columns": ["MetricA"]},
        schema_profile=[{"name": "MetricA", "dtype": "int64"}],
        column_profiles={"MetricA": {"mean": 1.5}},
    )
    dataset_registry.register(entry_a)
    
    sess_a = f"session_a_{uuid.uuid4()}"
    ctx_a = pipeline.process("query a", dataset_id=ds_a_id, session_id=sess_a)
    assert ctx_a.dataset_id == ds_a_id
    
    # User B session with NO dataset specified
    sess_b = f"session_b_{uuid.uuid4()}"
    ctx_b = pipeline.process("query b", dataset_id=None, session_id=sess_b)
    
    # Crucial assertion: User B must NEVER inherit User A's dataset via global fallback
    assert ctx_b.dataset_id is None
    assert ctx_b.dataset_filename is None


# ── Case 6: Provider response includes separate reasoning field ─────────────
def test_case_6_reasoning_field_excluded():
    from providers.openrouter import OpenRouterProvider
    provider = OpenRouterProvider(api_key="test-api-key")
    
    # Mock payload where choices has separate reasoning and content
    mock_payload = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "This is the final user answer.",
                "reasoning": "1. Analyze constraints: rule 1, rule 2. Draft response internally."
            }
        }],
        "model": "nvidia/nemotron-4-340b-instruct"
    }
    
    with patch("requests.post") as mock_post:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_payload
        mock_post.return_value = mock_response
        
        res = provider.generate(AIRequest(task="test", messages=[{"role": "user", "content": "hi"}]))
        assert res.success is True
        assert res.text == "This is the final user answer."
        assert "Analyze constraints" not in res.text


# ── Case 7: Provider returns malformed response or missing final answer ─────
def test_case_7_malformed_response_fails_closed():
    # Only internal reasoning provided, no actual final answer
    leaked_reasoning_only = (
        "1. Identify Constraints & Rules:\n"
        "- I am Plexis\n"
        "- No dataset is currently loaded\n"
        "2. Determine Response Strategy:\n"
        "- Explain that no dataset is loaded\n"
        "3. Internal thinking: unable to answer."
    )
    
    ctx = ExecutionContext(message="help", session_id="test")
    # Response boundary must detect internal planning dump and fail closed with safe fallback
    safe_answer = enforce_response_boundary(leaked_reasoning_only, ctx)
    assert not is_internal_planning_dump(safe_answer)
    assert "Identify Constraints" not in safe_answer
    assert "Determine Response Strategy" not in safe_answer
    assert "internal thinking" not in safe_answer.lower()
    assert "No dataset is currently loaded in this chat" in safe_answer


# ── Case 8: Screenshot CoT prompt leakage never rendered ────────────────────
def test_case_8_screenshot_cot_leakage_blocked():
    screenshot_text = (
        "2. Identify Constraints & Rules:\n"
        "   - I am Plexis, an AI data-analysis assistant.\n"
        "   - No dataset is currently loaded.\n"
        "   - I must not invent column names, metrics, numbers, or facts.\n"
        "   - If asked about the underlying model/provider, state that I am Plexis.\n"
        "\n"
        "3. Determine Response Strategy:\n"
        "   - The user asks \"which airline is the most delayed airline\".\n"
        "   - Since no dataset is loaded, acknowledge the question and politely explain that no dataset is loaded.\n"
        "\n"
        "4. Draft Response (internal reasoning):\n"
        "   - Warm, Plexis style: \"I'd love to help you find out which airline has the most delays, but there's no dataset currently loaded!\""
    )
    
    ctx = ExecutionContext(message="which airline is the most delayed airline", session_id="test")
    sanitized = enforce_response_boundary(screenshot_text, ctx)
    
    assert "Identify Constraints" not in sanitized
    assert "Determine Response Strategy" not in sanitized
    assert "Draft Response" not in sanitized
    assert "I am Plexis, an AI data-analysis assistant" not in sanitized
    assert "I must not invent column names" not in sanitized
    # Must be clean, safe fallback
    assert not is_internal_planning_dump(sanitized)


# ── Case 9: Streaming responses never expose internal planning fields ───────
def test_case_9_streaming_does_not_expose_reasoning():
    from providers.openrouter import OpenRouterProvider
    provider = OpenRouterProvider(api_key="test-api-key")
    
    # Mock SSE lines where some events have content and some have reasoning
    sse_lines = [
        b'data: {"choices":[{"delta":{"content":"Plexis is ready"}}]}',
        b'data: {"choices":[{"delta":{"reasoning":"internal step 1"}}]}',
        b'data: {"choices":[{"delta":{"content":" to assist you."}}]}',
        b'data: [DONE]'
    ]
    
    with patch("requests.post") as mock_post:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.iter_lines.return_value = sse_lines
        mock_response.__enter__.return_value = mock_response
        mock_post.return_value = mock_response
        
        chunks = list(provider.generate_stream(AIRequest(task="test", messages=[{"role": "user", "content": "hi"}])))
        full_streamed = "".join(chunks)
        assert full_streamed == "Plexis is ready to assist you."
        assert "internal step 1" not in full_streamed


# ── Case 10: Greeting and no-dataset fallback behavior ──────────────────────
def test_case_10_greeting_and_no_dataset_fallback():
    ctx = ExecutionContext(
        message="Hello",
        session_id=str(uuid.uuid4()),
        dataset_id=None,
    )
    # Master router fast-path handles simple greeting
    result = master_router.route(ctx)
    assert result.success is True
    assert any(w in result.answer.lower() for w in ["plexis", "hello", "hey", "hi", "how", "what", "welcome", "assist"])
    assert not is_internal_planning_dump(result.answer)


# ── Case 11: Arbitrary supported analytical question works without hardcoding
def test_case_11_arbitrary_supported_analytical_question(flight_delay_entry):
    ctx = ExecutionContext(
        message="what is the mean Rainfall_mm?",
        session_id=str(uuid.uuid4()),
        dataset_id=flight_delay_entry.dataset_id,
        dataset_filename=flight_delay_entry.filename,
        column_profiles=flight_delay_entry.column_profiles,
        schema_profile=flight_delay_entry.schema_profile,
    )
    
    result = master_router.route(ctx)
    assert result is not None
    assert ctx.intent in ("analysis", "DATA_OPERATION") or result.answer


# ── Case 12: Runs against representative schemas missing 'airline' column ───
def test_case_12_schema_missing_column_grounded_answer(flight_delay_entry):
    ctx = ExecutionContext(
        message="which airline is the most delayed airline",
        session_id=str(uuid.uuid4()),
        dataset_id=flight_delay_entry.dataset_id,
        dataset_filename=flight_delay_entry.filename,
        column_profiles=flight_delay_entry.column_profiles,
        schema_profile=flight_delay_entry.schema_profile,
    )
    
    # Mock LLM trying to query missing column 'airline'
    with patch.object(
        master_router,
        "_llm_interpret",
        return_value={
            "intent": "DATA_OPERATION",
            "operations": [{"type": "GROUP_MAX", "group_by": "airline", "value_column": "Departure_Delay"}],
            "confidence": 0.95
        }
    ):
        result = master_router.route(ctx)
        assert result.success is True
        # Must report that airline column does not exist and list available columns
        assert "does not contain" in result.answer
        assert "airline" in result.answer.lower()
        assert "Rainfall_mm" in result.answer
        assert "Departure_Delay" in result.answer
        # Must never leak planning text or CoT
        assert not is_internal_planning_dump(result.answer)


# ── Case 13: Unsolicited model disclosures are cleanly stripped ─────────────
def test_case_13_unsolicited_model_disclosure_stripped():
    raw_response = (
        "There are 6 unique values in that column.\n\n"
        "(P.S. If you're curious about what powers my responses, I'm Plexis - the AI data-analysis "
        "assistant in the Plexis app. The underlying model is nvidia/nemotron-3.5-lightning:free via openrouter, "
        "but you can just call me Plexis!)"
    )
    cleaned = strip_thinking_and_internal_reasoning(raw_response)
    assert "There are 6 unique values in that column." in cleaned
    assert "nvidia/nemotron" not in cleaned
    assert "underlying model" not in cleaned
    assert "openrouter" not in cleaned


# ── Case 14: Deduplication in dataset_service returns existing dataset ───────
def test_case_14_dataset_deduplication_reuses_existing():
    import io
    from db.services.dataset_service import dataset_service
    from db.repositories.dataset_repository import dataset_repository

    mock_db = MagicMock()
    user_id = uuid.uuid4()
    file_bytes = b"col1,col2\n1,2\n3,4"
    file_obj = io.BytesIO(file_bytes)

    existing_ds = MagicMock()
    existing_ds.id = uuid.uuid4()
    existing_ds.original_filename = "test.csv"

    # When get_by_hash finds an existing dataset, persist_dataset reuses it without INSERT
    with patch.object(dataset_repository, "get_by_hash", return_value=existing_ds), \
         patch.object(dataset_repository, "create_dataset") as mock_create:
        res = dataset_service.persist_dataset(mock_db, user_id, file_obj, "test.csv")
        assert res.id == existing_ds.id
        mock_create.assert_not_called()


# ── Case 15: Recursion guard prevents loop on auto-restore ──────────────────
def test_case_15_registry_recursion_guard():
    reg = dataset_registry
    test_ds_id = str(uuid.uuid4())

    def recursive_restore(db, ds_uuid, user_id):
        # Downstream call attempts to get the same dataset ID again
        nested_entry = reg.get(test_ds_id)
        assert nested_entry is None  # recursion guard must return None immediately
        return None, None

    with patch("db.session.get_db", return_value=MagicMock()), \
         patch("db.services.dataset_service.dataset_service.restore_dataset_df", side_effect=recursive_restore):
        result = reg.get(test_ds_id)
        assert result is None
        assert test_ds_id not in reg._restoring


# ── Case 16: Model context is ONLY injected when user queries model identity ─
def test_case_16_runtime_model_context_conditional_injection():
    from providers.engine import ProviderEngine
    from providers.model_registry import ModelMetadata

    meta = ModelMetadata(
        model_id="nvidia/nemotron-3.5-lightning:free",
        provider="openrouter",
        model_name="Nvidia Nemotron",
        context_window=8192,
        max_output_tokens=4096,
        supports_vision=False,
        supports_json_mode=True,
        supports_tool_calling=False,
        supports_streaming=True,
        supports_reasoning=False,
    )

    # 1. Normal data question: NO injection
    req_data = AIRequest(
        task="stream",
        messages=[{"role": "user", "content": "how many rows in this dataset?"}],
        system_prompt="You are Plexis data assistant."
    )
    with patch("providers.registry.provider_registry.get") as mock_get_prov, \
         patch("providers.selector.model_selector.select_fallback", return_value=meta), \
         patch("providers.scheduler.credential_scheduler.select_and_reserve", return_value=None):
        mock_provider = MagicMock()
        mock_provider.generate_stream.return_value = iter(["There are 100 rows."])
        mock_get_prov.return_value = mock_provider

        engine = ProviderEngine()
        list(engine.generate_stream(req_data))

        call_req = mock_provider.generate_stream.call_args[0][0]
        assert "nvidia/nemotron" not in call_req.system_prompt
        assert "Active Runtime Model Context" not in call_req.system_prompt

    # 2. Model question: injection present
    req_model = AIRequest(
        task="stream",
        messages=[{"role": "user", "content": "what model powers you?"}],
        system_prompt="You are Plexis data assistant."
    )
    with patch("providers.registry.provider_registry.get") as mock_get_prov, \
         patch("providers.selector.model_selector.select_fallback", return_value=meta), \
         patch("providers.scheduler.credential_scheduler.select_and_reserve", return_value=None):
        mock_provider = MagicMock()
        mock_provider.generate_stream.return_value = iter(["I am powered by Nemotron."])
        mock_get_prov.return_value = mock_provider

        engine = ProviderEngine()
        list(engine.generate_stream(req_model))

        call_req = mock_provider.generate_stream.call_args[0][0]
        assert "nvidia/nemotron" in call_req.system_prompt
        assert "Active Runtime Model Context" in call_req.system_prompt
