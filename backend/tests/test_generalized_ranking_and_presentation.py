import pytest
import pandas as pd
from core.context import ExecutionContext
from router.master_router import master_router
from analytics.pipeline import analytics_pipeline
from executor.duckdb_executor import duckdb_executor
from executor.pandas_executor import pandas_executor
from planner.schema import AnalyticalPlan
from datasets.registry import dataset_registry, DatasetEntry


@pytest.fixture
def flight_dataset():
    df = pd.DataFrame({
        "Airline": [
            "Delta", "United", "American", "Southwest", "JetBlue",
            "Delta", "Spirit", "Frontier", "Alaska", "Allegiant",
            "Delta", "United", "Southwest", "Hawaiian", "Sun Country"
        ],
        "Departure_Delay": [
            119, 108, 108, 106, 106,
            105, 103, 102, 102, 102,
            95, 90, 85, 80, 75
        ],
        "Flight_Number": [
            "DL101", "UA202", "AA303", "WN404", "B6505",
            "DL102", "NK606", "F9707", "AS808", "G4909",
            "DL103", "UA203", "WN405", "HA010", "SY011"
        ],
        "Origin": [
            "JFK", "ORD", "DFW", "ATL", "BOS",
            "LGA", "FLL", "DEN", "SEA", "LAS",
            "ATL", "SFO", "MDW", "HNL", "MSP"
        ],
        "Destination": [
            "LAX", "SFO", "MIA", "MCO", "SAN",
            "ORD", "EWR", "LAS", "PDX", "PHX",
            "LAX", "ORD", "BWI", "LAX", "DFW"
        ],
    })
    entry = DatasetEntry(
        dataset_id="test_flight_ds",
        filename="flights.csv",
        file_path="",
        upload_timestamp="",
        row_count=len(df),
        column_count=len(df.columns),
        file_fingerprint="fp_flight",
        schema_fingerprint="sfp_flight",
        dataframe=df,
        profile={},
        schema_profile=[
            {"name": "Airline", "type": "string"},
            {"name": "Departure_Delay", "type": "numeric"},
            {"name": "Flight_Number", "type": "string"},
            {"name": "Origin", "type": "string"},
            {"name": "Destination", "type": "string"},
        ],
        column_profiles={},
    )
    dataset_registry.register(entry)
    return df, entry


def test_ranking_routing_distinctions(flight_dataset):
    df, entry = flight_dataset

    # Ambiguous ranking without explicit aggregation returns structured clarification
    ctx1 = ExecutionContext(
        session_id="test_s1",
        message="give me top 10 most delayed airlines",
        dataset_id="test_flight_ds",
        dataset_filename="flights.csv",
    )
    ctx1.schema_profile = entry.schema_profile
    res1 = master_router.route(ctx1)
    assert res1.metadata and "clarification" in res1.metadata
    clarif = res1.metadata["clarification"]
    assert clarif["clarification_type"] == "RANKING_AMBIGUITY"
    assert clarif["entity"] == "Airline"
    assert clarif["metric"] == "Departure_Delay"
    assert len(clarif["ambiguous_options"]) >= 3

    # Explicit Grouped ranking executes directly: "top 10 airlines by average departure delay"
    ctx1_exp = ExecutionContext(
        session_id="test_s1_exp",
        message="top 10 airlines by average departure delay",
        dataset_id="test_flight_ds",
        dataset_filename="flights.csv",
    )
    ctx1_exp.schema_profile = entry.schema_profile
    master_router.route(ctx1_exp)
    op1 = getattr(ctx1_exp, "llm_operation", {})
    assert op1["type"] == "GROUP_MEAN"
    assert op1["column"] == "Airline"
    assert op1["value_column"] == "Departure_Delay"
    assert op1.get("top_n") == 10
    assert op1.get("order") == "desc"

    # 2. Row ranking: "give me top 10 most delayed flights"
    ctx2 = ExecutionContext(
        session_id="test_s2",
        message="give me top 10 most delayed flights",
        dataset_id="test_flight_ds",
        dataset_filename="flights.csv",
    )
    ctx2.schema_profile = entry.schema_profile
    master_router.route(ctx2)
    op2 = getattr(ctx2, "llm_operation", {})
    assert op2["type"] == "TOP_N"
    assert op2["column"] == "Departure_Delay"
    assert op2.get("n") == 10
    assert op2.get("order") == "desc"

    # 3. Scalar extremum: "what is the highest departure delay"
    ctx3 = ExecutionContext(
        session_id="test_s3",
        message="what is the highest departure delay",
        dataset_id="test_flight_ds",
        dataset_filename="flights.csv",
    )
    ctx3.schema_profile = entry.schema_profile
    master_router.route(ctx3)
    op3 = getattr(ctx3, "llm_operation", {})
    assert op3["type"] == "MAX"
    assert op3["column"] == "Departure_Delay"


def test_qualitative_entity_ranking_generalizes_to_unfamiliar_schema():
    df = pd.DataFrame({
        "operator_code": ["North", "North", "South", "South", "West"],
        "delay_minutes": [2, 8, 7, 9, 5],
        "Service Ref": ["n1", "n2", "s1", "s2", "w1"],
    })
    ctx = ExecutionContext(session_id="synthetic_rank", message="show top 2 most delayed operators")
    res = master_router._analysis_fast_path(ctx, ctx.message, df_override=df)
    assert res is not None
    assert "clarification" in res
    assert res["clarification"]["clarification_type"] == "RANKING_AMBIGUITY"
    assert res["clarification"]["entity"] == "operator_code"
    assert res["clarification"]["metric"] == "delay_minutes"

    expected = df.loc[df["delay_minutes"] > 0].groupby("operator_code").size().sort_values(ascending=False, kind="stable").head(2)
    plan = AnalyticalPlan(
        operation="group_count_filtered",
        target_column="operator_code",
        parameters={"group_by": "operator_code", "filter_column": "delay_minutes", "filter_op": ">", "filter_value": 0, "top_n": 2, "order": "desc"},
    )
    result = duckdb_executor.execute(plan, df)
    assert result.success
    assert list(result.value) == list(expected.index)
    assert list(result.value.values()) == pytest.approx(list(expected.values))


def test_grouped_ranking_execution_limit_and_order(flight_dataset):
    df, _ = flight_dataset
    plan = AnalyticalPlan(
        intent="group_mean",
        operation="group_mean",
        target_column="Airline",
        parameters={
            "group_by": "Airline",
            "value_column": "Departure_Delay",
            "top_n": 5,
            "order": "desc",
        },
        plan=[],
    )

    # Test via DuckDB
    res_duck = duckdb_executor.execute(plan, df)
    assert res_duck.success
    assert len(res_duck.value) == 5
    # Verify values are descending
    vals = list(res_duck.value.values())
    assert all(vals[i] >= vals[i + 1] for i in range(len(vals) - 1))

    # Test via Pandas
    res_pan = pandas_executor.execute(plan, df)
    assert res_pan.success
    assert len(res_pan.value) == 5
    vals_p = list(res_pan.value.values())
    assert all(vals_p[i] >= vals_p[i + 1] for i in range(len(vals_p) - 1))


def test_top_n_row_ranking_execution_and_provenance(flight_dataset):
    df, _ = flight_dataset
    plan = AnalyticalPlan(
        intent="top_n",
        operation="top_n",
        target_column="Departure_Delay",
        parameters={"n": 10, "order": "desc"},
        plan=[],
    )

    # Test DuckDB execution
    res_duck = duckdb_executor.execute(plan, df)
    assert res_duck.success
    assert len(res_duck.value) == 10
    assert len(res_duck.matching_rows) == 10
    assert res_duck.matching_rows[0]["result_rank"] == 1
    assert res_duck.matching_rows[0]["Departure_Delay"] == 119
    assert res_duck.matching_rows[0]["source_row_number"] == 1
    assert res_duck.matching_rows[9]["result_rank"] == 10

    # Test Pandas execution
    res_pan = pandas_executor.execute(plan, df)
    assert res_pan.success
    assert len(res_pan.value) == 10
    assert len(res_pan.matching_rows) == 10
    assert res_pan.matching_rows[0]["result_rank"] == 1
    assert res_pan.matching_rows[0]["Departure_Delay"] == 119
    assert res_pan.matching_rows[0]["source_row_number"] == 1


def test_top_n_exceeds_legacy_limit_and_has_stable_tie_order():
    df = pd.DataFrame({"metric_value": [8, 10, 10, 4]}, index=[40, 10, 30, 20])
    plan = AnalyticalPlan(operation="top_n", target_column="metric_value", parameters={"n": 1500})
    for executor in (duckdb_executor, pandas_executor):
        result = executor.execute(plan, df)
        assert result.success
        assert result.value == [10, 10, 8, 4]
        assert [r["dataframe_index"] for r in result.matching_rows] == [10, 30, 40, 20]
        assert [r["result_rank"] for r in result.matching_rows] == [1, 2, 3, 4]


def test_pipeline_narrative_no_operation_leakage(flight_dataset):
    df, _ = flight_dataset
    ctx = ExecutionContext(
        session_id="test_s_pipe",
        message="give me top 10 most delayed airlines",
        dataset_id="test_flight_ds",
        dataset_filename="flights.csv",
    )
    ctx.llm_operation = {
        "type": "GROUP_MEAN",
        "column": "Airline",
        "value_column": "Departure_Delay",
        "top_n": 10,
        "order": "desc",
    }
    result = analytics_pipeline.run(ctx, df)
    narrative = analytics_pipeline.build_narrative(result, "flights.csv")

    assert "(operation:" not in narrative
    assert "Top 10" in narrative
    assert "Airline" in narrative
    assert "Departure_Delay" in narrative

    # Also test row ranking narrative
    ctx_row = ExecutionContext(
        session_id="test_s_pipe2",
        message="give me top 10 most delayed flights",
        dataset_id="test_flight_ds",
        dataset_filename="flights.csv",
    )
    ctx_row.llm_operation = {
        "type": "TOP_N",
        "column": "Departure_Delay",
        "n": 10,
        "order": "desc",
    }
    result_row = analytics_pipeline.run(ctx_row, df)
    narrative_row = analytics_pipeline.build_narrative(result_row, "flights.csv")

    assert "(operation:" not in narrative_row
    assert "Top 10 records ranked by highest" in narrative_row
    assert "Departure_Delay" in narrative_row


def test_tie_handling_distinction(flight_dataset):
    df, _ = flight_dataset
    from api.chat import _build_result_block

    # For TOP_N: is_tied MUST be False, even when multiple rows are returned
    ctx_top = ExecutionContext(
        session_id="test_tie1",
        message="top 10 departure delays",
        dataset_id="test_flight_ds",
        dataset_filename="flights.csv",
    )
    ctx_top.llm_operation = {"type": "TOP_N", "column": "Departure_Delay", "n": 10}
    res_top = analytics_pipeline.run(ctx_top, df)
    ctx_top.analytical_result = res_top

    block_top = _build_result_block(ctx_top)
    assert block_top["is_tied"] is False
    assert block_top["tie_count"] == 0
    assert len(block_top["rows"]) == 10

    ctx_top_12 = ExecutionContext(
        session_id="test_tie12",
        message="top 12 departure delays",
        dataset_id="test_flight_ds",
        dataset_filename="flights.csv",
    )
    ctx_top_12.llm_operation = {"type": "TOP_N", "column": "Departure_Delay", "n": 12}
    ctx_top_12.analytical_result = analytics_pipeline.run(ctx_top_12, df)
    block_top_12 = _build_result_block(ctx_top_12)
    assert len(block_top_12["rows"]) == 12
    assert [row["result_rank"] for row in block_top_12["rows"]] == list(range(1, 13))


def test_grouped_result_block_carries_ordered_rank_entries():
    from collections import OrderedDict
    from types import SimpleNamespace
    from api.chat import _build_result_block

    ctx = ExecutionContext(session_id="ordered_groups", message="top 3 delayed airlines")
    ctx.analytical_result = SimpleNamespace(
        # Deliberately alphabetical metric map: only executor provenance is the
        # authoritative ranking order.
        value={"Carrier A": 9, "Carrier M": 7, "Carrier Z": 12},
        operation="group_count_filtered",
        column="Carrier",
        row_indices=[],
        matching_rows=[],
        verified=True,
        details={
            "top_n": 3, "group_by": "Carrier", "value_column": "Delay_Minutes", "order": "desc",
            "ordered_entries": [
                {"entity": "Carrier Z", "value": 12, "rank": 1},
                {"entity": "Carrier A", "value": 9, "rank": 2},
                {"entity": "Carrier M", "value": 7, "rank": 3},
            ],
        },
    )
    result_block = _build_result_block(ctx)
    assert result_block["details"]["ordered_entries"] == [
        {"entity": "Carrier Z", "value": 12, "rank": 1},
        {"entity": "Carrier A", "value": 9, "rank": 2},
        {"entity": "Carrier M", "value": 7, "rank": 3},
    ]
    assert result_block["ranking"]["items"] == result_block["details"]["ordered_entries"]
    assert result_block["ranking"]["computed_count"] == 3
    assert result_block["ranking"]["serialized_count"] == 3
    import json
    round_tripped = json.loads(json.dumps(result_block, sort_keys=True))
    assert [item["entity"] for item in round_tripped["ranking"]["items"]] == ["Carrier Z", "Carrier A", "Carrier M"]


def test_grouped_rank_narrative_reports_fewer_available_groups_than_requested():
    from collections import OrderedDict
    from types import SimpleNamespace

    result = SimpleNamespace(
        value=OrderedDict([("Carrier A", 12), ("Carrier B", 9), ("Carrier C", 7)]),
        operation="group_count_filtered",
        column="Carrier",
        details={"top_n": 5, "value_column": "Delay_Minutes", "filter_desc": "Delay Minutes > 0", "order": "desc"},
        matching_rows=[],
        verified=True,
    )
    narrative = analytics_pipeline.build_narrative(result, "synthetic dataset")
    assert "Top 3 of 5 requested" in narrative
    assert "count of Delay Minutes > 0" in narrative
    assert "Carrier A" not in narrative  # values come from the table, not a second prose ranking


@pytest.mark.parametrize("executor", [duckdb_executor, pandas_executor])
def test_group_ranking_completeness_and_deterministic_order(executor):
    # Unfamiliar schema, 12 eligible entities, a tie at the 10th boundary,
    # and one null entity. Independently calculated expected results below.
    df = pd.DataFrame({
        "operator": [f"Entity {i:02d}" for i in range(12)] + ["Entity 12", None],
        "score": [12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 3, 1, None, 100],
    })
    expected_top = [(f"Entity {i:02d}", value) for i, value in enumerate([12, 11, 10, 9, 8, 7, 6, 5, 4], start=0)]
    expected_top += [("Entity 09", 3)]  # tied entity labels break ties ascending
    plan = AnalyticalPlan(
        operation="group_max", target_column="operator",
        parameters={"group_by": "operator", "value_column": "score", "top_n": 10, "order": "desc"},
    )
    result = executor.execute(plan, df)
    assert result.success
    assert len(result.value) == 10
    assert list(result.value.items()) == expected_top
    assert result.details["requested_n"] == 10
    assert result.details["eligible_group_count"] == 12
    assert result.details["returned_group_count"] == 10
    assert result.details["tie_policy"] == "metric_value_then_entity_ascending"

    bottom_plan = AnalyticalPlan(
        operation="group_min", target_column="operator",
        parameters={"group_by": "operator", "value_column": "score", "top_n": 10, "order": "asc"},
    )
    bottom = executor.execute(bottom_plan, df)
    assert bottom.success
    assert len(bottom.value) == 10
    assert list(bottom.value.values()) == [1, 3, 3, 4, 5, 6, 7, 8, 9, 10]
    assert list(bottom.value)[:3] == ["Entity 11", "Entity 09", "Entity 10"]


def test_filtered_group_ranking_returns_actual_eligible_entity_count():
    df = pd.DataFrame({"category": ["a", "a", "b", "b", "c", None], "amount": [4, 0, 3, 0, 0, 8]})
    plan = AnalyticalPlan(
        operation="group_count_filtered", target_column="category",
        parameters={"group_by": "category", "filter_column": "amount", "filter_op": ">", "filter_value": 0, "top_n": 10, "order": "desc"},
    )
    for executor in (duckdb_executor, pandas_executor):
        result = executor.execute(plan, df)
        assert result.success
        assert result.value == {"a": 1, "b": 1}
        assert result.details["eligible_group_count"] == 2
        assert result.details["returned_group_count"] == 2

def test_least_delay_ranking_clarification_and_followups(flight_dataset):
    df, entry = flight_dataset

    # 1. User asks 'give me top 10 airlines who have least delay'
    ctx = ExecutionContext(
        session_id="test_least_delay_1",
        message="give me top 10 airlines who have least delay",
        dataset_id="test_flight_ds",
        dataset_filename="flights.csv",
    )
    ctx.schema_profile = entry.schema_profile
    res = master_router.route(ctx)
    assert res.metadata and "clarification" in res.metadata
    clarif = res.metadata["clarification"]
    assert clarif["clarification_type"] == "RANKING_AMBIGUITY"
    assert clarif["entity"] == "Airline"
    assert clarif["metric"] == "Departure_Delay"
    assert clarif["order"] == "asc"
    assert "transformation_id" in clarif
    assert len(clarif["ambiguous_options"]) >= 3
    assert "lowest average" in clarif["ambiguous_options"][0]["label"].lower()

    # 2. Follow-up via workspace action (clicking card button)
    ctx_click = ExecutionContext(
        session_id="test_least_delay_1",
        message="I meant: Lowest average departure delay",
        dataset_id="test_flight_ds",
        dataset_filename="flights.csv",
        workspace_action={
            "type": "RANKING_CLARIFICATION",
            "clarification_type": "RANKING_AMBIGUITY",
            "transformation_id": clarif["transformation_id"],
            "value": "RANKING_AVERAGE",
            "chosen_operation": clarif["ambiguous_options"][0]["operation"],
        },
    )
    ctx_click.schema_profile = entry.schema_profile
    res_click = master_router.route(ctx_click)
    op_click = getattr(ctx_click, "llm_operation", {})
    assert op_click["type"] == "GROUP_MEAN"
    assert op_click["column"] == "Airline"
    assert op_click["value_column"] == "Departure_Delay"
    assert op_click.get("order") == "asc"

    # 3. Follow-up via free-text (e.g. 'by lowest average') on another session
    ctx_init2 = ExecutionContext(
        session_id="test_least_delay_text",
        message="give me top 10 airlines who have least delay",
        dataset_id="test_flight_ds",
        dataset_filename="flights.csv",
    )
    ctx_init2.schema_profile = entry.schema_profile
    res2 = master_router.route(ctx_init2)
    assert res2.metadata and "clarification" in res2.metadata

    ctx_text = ExecutionContext(
        session_id="test_least_delay_text",
        message="by lowest average",
        dataset_id="test_flight_ds",
        dataset_filename="flights.csv",
    )
    ctx_text.schema_profile = entry.schema_profile
    res_text = master_router.route(ctx_text)
    op_text = getattr(ctx_text, "llm_operation", {})
    assert op_text["type"] == "GROUP_MEAN"
    assert op_text["column"] == "Airline"
    assert op_text["value_column"] == "Departure_Delay"
    assert op_text.get("order") == "asc"
