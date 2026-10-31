import pytest
import json
from unittest.mock import MagicMock
from core.context import ExecutionContext
from router.master_router import MasterRouter
from datasets.registry import dataset_registry, DatasetEntry
import pandas as pd


@pytest.fixture
def mock_dataset():
    df = pd.DataFrame({
        "Product": ["Laptop", "Mouse", "Keyboard", "Monitor", "Headphones"],
        "Sales": [1200, 25, 75, 300, 150],
        "Category": ["Electronics", "Accessories", "Accessories", "Electronics", "Audio"],
    })
    entry = DatasetEntry(
        dataset_id="test_gen_ds",
        filename="sales.csv",
        file_path="",
        upload_timestamp="",
        row_count=len(df),
        column_count=len(df.columns),
        file_fingerprint="fp_sales",
        schema_fingerprint="sfp_sales",
        profile={},
        schema_profile={},
        column_profiles={},
        dataframe=df,
    )
    dataset_registry.register(entry)
    return df, entry


def test_llm_interpret_fallback_routes_to_data_operation_when_dataset_loaded(mock_dataset):
    """
    When the LLM provider experiences a transient error / rate limit and a dataset is loaded,
    Plexis must fall back to DATA_OPERATION (analytics engine planning via DuckDB/Pandas),
    NEVER dumping the user into a generic conversation that asks permission.
    """
    router = MasterRouter()
    ctx = ExecutionContext(
        session_id="test_fallback_session",
        message="what is the total sales amount by category?",
        dataset_id="test_gen_ds",
        dataset_filename="sales.csv",
    )
    semantic_ctx = {"dataset_loaded": True, "dataset_id": "test_gen_ds", "columns": [{"name": "Sales"}, {"name": "Category"}]}
    
    # Mock provider failure
    from providers.engine import provider_engine
    original_generate = provider_engine.generate
    
    mock_resp = MagicMock()
    mock_resp.success = False
    mock_resp.error = "429 Quota Exceeded (Test)"
    mock_resp.raw_response = {}
    provider_engine.generate = MagicMock(return_value=mock_resp)
    
    try:
        payload = router._llm_interpret(ctx, semantic_ctx)
        assert payload["intent"] == "DATA_OPERATION"
        assert "Fallback to analysis planner" in payload.get("reason", "")
    finally:
        provider_engine.generate = original_generate


def test_truncated_json_recovery_handles_data_operation():
    """
    If the LLM returns DATA_OPERATION but the response is slightly cut off at max_tokens,
    the router must recover the intent and operation instead of dropping to conversation.
    """
    router = MasterRouter()
    truncated_text = '{"intent": "DATA_OPERATION", "operations": [{"type": "GROUP_SUM", "column": "Category"'
    payload = router._parse_llm_json(truncated_text)
    assert payload["intent"] == "DATA_OPERATION"
    assert payload["operation"]["type"] == "GROUP_SUM"
    assert payload["operation"]["column"] == "Category"


def test_truncated_json_recovery_handles_transform():
    router = MasterRouter()
    truncated_text = '{"intent": "TRANSFORM", "operations": [{"type": "FILL_MISSING", "fill_value": 0'
    payload = router._parse_llm_json(truncated_text)
    assert payload["intent"] == "TRANSFORM"
    assert payload["operation"]["type"] == "FILL_MISSING"
    assert payload["operation"]["fill_value"] == 0
