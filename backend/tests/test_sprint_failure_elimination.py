"""
Automated Regression Tests for Plexis Failure Elimination Sprint.

Covers:
  1. Multi-Column Domain & Value Integrity Audit (Telco Q08 generalization)
  2. Dual-Metric Group Rank Concordance & Comparison (Flight Q26 generalization)
  3. Categorical / Route Label Normalization & Group Cardinality (Flight Q50 generalization)
  4. Hypothetical Value Clipping / Capping & Mean Impact Analysis (Flight Q57 generalization)
  5. Predictive Target Leakage & Feature Risk Analysis (Flight Q60 generalization)
"""
import pytest
import pandas as pd
import numpy as np

import api.chat
from router.master_router import MasterRouter
from core.context import ExecutionContext
from datasets.registry import dataset_registry, DatasetEntry


@pytest.fixture
def synthetic_df():
    """Create a synthetic multi-type DataFrame for generalization testing."""
    return pd.DataFrame({
        "customer_id": [f"CUST_{i:04d}" for i in range(100)],
        "is_vip": [1 if i % 4 == 0 else 0 for i in range(100)],
        "tenure_months": [i % 60 for i in range(100)],
        "charge_amt": [20.0 + (i * 1.5) for i in range(100)],
        "status_str": ["Active" if i % 3 != 0 else "Churned" for i in range(100)],
        "origin_city": ["DEL" if i % 2 == 0 else "BOM" for i in range(100)],
        "dest_city": ["BLR" if i % 3 == 0 else "MAA" for i in range(100)],
        "risk_score": [10.0 + (i * 0.5) for i in range(100)],  # min 10.0, max 59.5
        "month_num": [(i % 12) + 1 for i in range(100)],
        "delay_minutes": [float(i % 45) for i in range(100)],
        "is_delayed": [1 if (i % 45) > 15 else 0 for i in range(100)],
    })


@pytest.fixture
def router_and_context(synthetic_df):
    entry = DatasetEntry(
        dataset_id="synthetic_test",
        filename="synthetic.csv",
        file_path="",
        upload_timestamp="",
        row_count=len(synthetic_df),
        column_count=len(synthetic_df.columns),
        file_fingerprint="",
        schema_fingerprint="",
        dataframe=synthetic_df,
        profile={},
        schema_profile=[],
        column_profiles={},
    )
    dataset_registry.register(entry)
    router = MasterRouter()
    return router


def test_multi_column_domain_audit_generalization(router_and_context):
    router = router_and_context
    msg = "Audit the domains and values of is_vip, tenure_months, charge_amt, and status_str. Identify invalid or suspicious values."
    ctx = ExecutionContext(message=msg, dataset_id="synthetic_test")
    res = router.route(ctx)

    assert res.success is True
    ans = res.answer.lower()
    assert "is_vip" in ans
    assert "tenure_months" in ans
    assert "charge_amt" in ans
    assert "status_str" in ans
    assert "0" in ans and "1" in ans  # binary domain verified
    assert "active" in ans or "churned" in ans  # categories verified


def test_dual_metric_rank_concordance_generalization(router_and_context):
    router = router_and_context
    msg = "For each month_num, report the mean delay_minutes and the proportion of is_delayed = 1. Determine whether the monthly rankings for these two metrics are identical."
    ctx = ExecutionContext(message=msg, dataset_id="synthetic_test")
    res = router.route(ctx)

    assert res.success is True
    ans = res.answer.lower()
    assert "month_num" in ans
    assert "delay_minutes" in ans
    assert "is_delayed" in ans
    assert "rank" in ans
    assert "identical" in ans or "not identical" in ans or "different" in ans


def test_route_normalization_generalization(router_and_context):
    router = router_and_context
    msg = "Compare average delay_minutes by origin-destination route after normalizing route labels. Explain the normalization and show whether it changes the number of distinct route groups. Do not mutate the source dataset."
    ctx = ExecutionContext(message=msg, dataset_id="synthetic_test")
    res = router.route(ctx)

    assert res.success is True
    ans = res.answer.lower()
    assert "route" in ans
    assert "distinct" in ans
    assert "normaliz" in ans
    assert "does not change" in ans or "no change" in ans or "unchanged" in ans


def test_hypothetical_clipping_generalization(router_and_context):
    router = router_and_context
    # risk_score is between 10.0 and 59.5. Clipping to [0, 100] should change 0 values
    msg = "If risk_score values were hypothetically clipped to the interval [0, 100], how many values would change and what would the resulting mean be? First inspect the actual range. Do not apply the transformation to the source dataset."
    ctx = ExecutionContext(message=msg, dataset_id="synthetic_test")
    res = router.route(ctx)

    assert res.success is True
    ans = res.answer.lower()
    assert "risk_score" in ans
    assert "0" in ans or "none" in ans or "within" in ans
    assert "mean" in ans
    assert "range" in ans or "interval" in ans

    # Test with normalized_query where punctuation/brackets were stripped by pipeline
    ctx2 = ExecutionContext(message=msg, dataset_id="synthetic_test")
    ctx2.normalized_query = "if risk_score values were hypothetically clipped to interval 0 100 how many values would change and what would resulting mean be first inspect actual range do not apply transformation to source dataset"
    res2 = router.route(ctx2)
    assert res2.success is True
    ans2 = res2.answer.lower()
    assert "0 values" in ans2 or "none" in ans2
    assert "[0.00, 100.00]" in ans2

    # Test with normalized_query where punctuation/brackets were stripped by pipeline
    ctx2 = ExecutionContext(message=msg, dataset_id="synthetic_test")
    ctx2.normalized_query = "if risk_score values were hypothetically clipped to interval 0 100 how many values would change and what would resulting mean be first inspect actual range do not apply transformation to source dataset"
    res2 = router.route(ctx2)
    assert res2.success is True
    ans2 = res2.answer.lower()
    assert "0 values" in ans2 or "none" in ans2
    assert "[0.00, 100.00]" in ans2


def test_target_leakage_generalization(router_and_context):
    router = router_and_context
    msg = "If the goal is to predict is_delayed, which available columns could cause target leakage? Identify delay_minutes as a potential leakage risk, explain why, and distinguish observed dataset facts from assumptions. Do not train a model or change the dataset."
    ctx = ExecutionContext(message=msg, dataset_id="synthetic_test")
    res = router.route(ctx)

    assert res.success is True
    ans = res.answer.lower()
    assert "delay_minutes" in ans
    assert "leak" in ans
    assert "is_delayed" in ans
    assert "observed" in ans or "post-decision" in ans
