"""
Tests for ADD_COLUMN transformation, follow-up parsing, and provider resiliency.
"""
import pytest
import pandas as pd
from unittest.mock import MagicMock

from router.master_router import _parse_transform_from_message, MasterRouter
from transformation.planner import TransformationPlanner
from transformation.executor import TransformationExecutor
from core.context import ExecutionContext


def test_parse_add_column_quoted():
    op = _parse_transform_from_message('add a column "departure_percent"')
    assert op is not None
    assert op.get("type") == "ADD_COLUMN"
    assert op.get("column") == "departure_percent"
    assert op.get("value") is None


def test_parse_add_column_bare():
    op = _parse_transform_from_message('add column departure_percent')
    assert op is not None
    assert op.get("type") == "ADD_COLUMN"
    assert op.get("column") == "departure_percent"
    assert op.get("value") is None


def test_parse_add_column_with_value():
    op = _parse_transform_from_message('create a new column "score" with 100')
    assert op is not None
    assert op.get("type") == "ADD_COLUMN"
    assert op.get("column") == "score"
    assert op.get("value") == 100


def test_parse_add_column_followup_from_history():
    ctx = ExecutionContext(message="leave that thing but just add that column")
    ctx.conversation_history = [
        {"role": "user", "content": 'add a column "departure_percent"'},
        {
            "role": "assistant",
            "content": "Okay, I can definitely help you add a new column called departure_percent! To make sure I get it just right, could you tell me how you'd like to calculate that percentage? What should departure_percent represent? 😊"
        }
    ]
    op = _parse_transform_from_message(ctx.message, context=ctx)
    assert op is not None
    assert op.get("type") == "ADD_COLUMN"
    assert op.get("column") == "departure_percent"


def test_planner_and_executor_add_column():
    df = pd.DataFrame({
        "flight_id": [101, 102, 103],
        "delay": [15, 0, 45]
    })
    planner = TransformationPlanner()
    plan = planner.plan(df, {"type": "ADD_COLUMN", "column": "departure_percent"}, dataset_id="test_ds")

    assert plan.operation_type == "ADD_COLUMN"
    assert plan.affected_columns == ["departure_percent"]
    assert plan.affected_rows_count == 3
    assert plan.operation_label == "Add column 'departure_percent'"

    executor = TransformationExecutor()
    new_df = executor._execute(df.copy(), plan)
    assert "departure_percent" in new_df.columns
    assert len(new_df) == 3
    assert new_df["departure_percent"].isnull().all()

    # Verify postconditions pass
    executor._verify_postconditions(df, new_df, plan)


def test_semantic_cache_dataset_loaded_no_unbound_error():
    router = MasterRouter()
    ctx = ExecutionContext(message="hi")
    semantic_ctx = {
        "dataset_id": "ds_123",
        "dataset_fingerprint": "fp_abc",
        "dataset_loaded": True,
        "columns": [{"name": "colA", "type": "int"}, {"name": "colB", "type": "str"}],
        "referenced_columns": []
    }
    cache_key = f"hi::{semantic_ctx['dataset_id']}::{semantic_ctx['dataset_fingerprint']}"
    router._semantic_cache[cache_key] = {"intent": "conversation", "confidence": 0.9}

    # Should not raise UnboundLocalError
    # (Because dataset_loaded is True and cached intent is conversation, cache should be bypassed)
    # We mock fallback_engine to return a dummy response
    mock_engine = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = '{"intent": "conversation", "confidence": 0.95}'
    mock_engine.execute.return_value = mock_resp
    router._fallback_engine = mock_engine

    res = router._llm_interpret(ctx, semantic_ctx)
    assert res is not None
