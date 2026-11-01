import uuid
import pandas as pd
import pytest

from core.context import ExecutionContext
from datasets.registry import dataset_registry, DatasetEntry
from engines.transform import TransformEngine
from router.master_router import MasterRouter
from transformation.executor import transformation_executor
from transformation.store import transformation_store
from transformation.ir import TransformationIR, TransformStep


from datetime import datetime, timezone

@pytest.fixture
def sample_dataset():
    df = pd.DataFrame({
        'Airline': ['IndiGo', 'Air India', 'SpiceJet'],
        'Departure_Delay': [15.0, 30.0, 0.0],
        'Arrival_Delay': [10.0, 25.0, -5.0]
    })
    ds_id = str(uuid.uuid4())
    entry = DatasetEntry(
        dataset_id=ds_id,
        filename="test_flights.csv",
        file_path="/tmp/test_flights.csv",
        upload_timestamp=datetime.now(timezone.utc).isoformat(),
        dataframe=df,
        row_count=len(df),
        column_count=len(df.columns),
        file_fingerprint="test_fp",
        schema_fingerprint="test_sfp",
        profile={},
        schema_profile=[],
        column_profiles={},
    )
    dataset_registry.register(entry)
    yield ds_id, df
    dataset_registry._datasets.pop(ds_id, None)


def test_create_column_missing_expression_triggers_clarification(sample_dataset, monkeypatch):
    ds_id, df = sample_dataset
    engine = TransformEngine()
    router = MasterRouter()

    # Mock ir_planner.plan to return CREATE_COLUMN with expression=None (reproducing underspecified request)
    from transformation.ir_planner import ir_planner
    monkeypatch.setattr(
        ir_planner,
        'plan',
        lambda msg, cols, dtypes, nullable: TransformationIR(
            goal='Add column departure_percent',
            steps=[
                TransformStep(
                    id='step_1',
                    operation='CREATE_COLUMN',
                    params={'column': 'departure_percent'},
                    expression=None,
                    label='Add departure_percent column'
                )
            ]
        )
    )

    ctx = ExecutionContext(
        message='add a column "departure_percent"',
        dataset_id=ds_id,
        chat_id='test_chat_col_1',
        intent='TRANSFORM'
    )
    ctx.llm_operation = {'type': 'IR_REQUIRED'}

    res = engine.handle(ctx)
    assert res.success is True
    assert 'clarification' in res.metadata
    clarif = res.metadata['clarification']

    assert clarif['status'] == 'AWAITING_CLARIFICATION'
    assert clarif['clarification_type'] == 'EXPRESSION_REQUIRED'
    assert clarif['missing_slots'] == ['expression']
    assert 'departure_percent' in clarif['question']

    # Candidate columns should prioritize token overlap ("Departure_Delay") and numeric columns
    assert 'Departure_Delay' in clarif['candidate_columns']

    # Ambiguous options should include Initialize with 0, Initialize with null, and Copy from Departure_Delay
    options = clarif['ambiguous_options']
    labels = [opt['label'] for opt in options]
    assert 'Initialize with 0' in labels
    assert 'Initialize with null' in labels
    assert any('Departure_Delay' in l for l in labels)

    # Test Option 1 (Initialize with 0) via workspace_action
    opt_zero = next(opt for opt in options if opt['action'] == 'INIT_ZERO')
    pending_id = clarif['transformation_id']

    ctx_zero = ExecutionContext(
        message='I meant: Initialize with 0',
        dataset_id=ds_id,
        chat_id='test_chat_col_1',
        workspace_action={
            'type': 'TRANSFORMATION_CLARIFICATION',
            'clarification_type': 'EXPRESSION_REQUIRED',
            'transformation_id': pending_id,
            'action': 'INIT_ZERO',
            'chosen_operation': opt_zero['operation']
        }
    )

    route_res = router._pending_transform_fast_path(ctx_zero)
    assert route_res is not None
    assert route_res['intent'] == 'TRANSFORM'
    assert route_res['operation']['column'] == 'departure_percent'

    # Execute plan generation
    ctx_zero.intent = 'TRANSFORM'
    ctx_zero.llm_operation = route_res['operation']
    plan_res = engine.handle(ctx_zero)
    assert plan_res.success is True
    assert 'transform_plan' in plan_res.metadata
    plan = plan_res.metadata['transform_plan']
    assert 'departure_percent' in plan['affected_columns']
    assert plan['affected_cells_count'] == 3

    # Apply the plan using transformation_executor
    plan_obj = transformation_store.get(plan['plan_id'])
    assert plan_obj is not None
    exec_res = transformation_executor.apply(plan_obj, df, ds_id, None)
    assert exec_res.success is True
    new_entry = dataset_registry.get(ds_id)
    assert 'departure_percent' in new_entry.dataframe.columns
    assert (new_entry.dataframe['departure_percent'] == 0).all()


def test_create_column_text_follow_ups(sample_dataset, monkeypatch):
    ds_id, df = sample_dataset
    engine = TransformEngine()
    router = MasterRouter()

    from transformation.ir_planner import ir_planner
    monkeypatch.setattr(
        ir_planner,
        'plan',
        lambda msg, cols, dtypes, nullable: TransformationIR(
            goal='Add column departure_percent',
            steps=[
                TransformStep(
                    id='step_1',
                    operation='CREATE_COLUMN',
                    params={'column': 'departure_percent'},
                    expression=None,
                    label='Add departure_percent column'
                )
            ]
        )
    )

    # 1. Trigger clarification
    ctx = ExecutionContext(
        message='add a column "departure_percent"',
        dataset_id=ds_id,
        chat_id='test_chat_text_1',
        intent='TRANSFORM'
    )
    ctx.llm_operation = {'type': 'IR_REQUIRED'}
    res = engine.handle(ctx)
    assert res.metadata['clarification']['status'] == 'AWAITING_CLARIFICATION'

    # 2. User replies with text "0"
    ctx_reply = ExecutionContext(
        message='0',
        dataset_id=ds_id,
        chat_id='test_chat_text_1'
    )
    route_res = router._pending_transform_fast_path(ctx_reply)
    assert route_res['intent'] == 'TRANSFORM'
    assert route_res['operation']['expression']['value'] == 0

    # 3. Trigger again and user replies with arithmetic "Departure_Delay / 60"
    res2 = engine.handle(ctx)
    ctx_arith = ExecutionContext(
        message='Departure_Delay / 60',
        dataset_id=ds_id,
        chat_id='test_chat_text_1'
    )
    route_arith = router._pending_transform_fast_path(ctx_arith)
    assert route_arith['intent'] == 'TRANSFORM'
    expr = route_arith['operation']['expression']
    assert expr['type'] == 'ARITHMETIC'
    assert expr['operator'] == 'divide'
    assert expr['left']['name'] == 'Departure_Delay'
    assert expr['right']['value'] == 60
