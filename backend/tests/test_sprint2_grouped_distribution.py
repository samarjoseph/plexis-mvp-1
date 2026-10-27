"""
Sprint 2 Generalization and Regression Test Suite:
Grouped Continuous Distribution Comparison across Binary Indicators
"""

import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import unittest
import numpy as np
import pandas as pd

from executor.pandas_executor import PandasExecutor
from executor.duckdb_executor import DuckDBExecutor
from planner.schema import AnalyticalPlan
from analytics.pipeline import analytics_pipeline
from router.master_router import master_router
from core.context import ExecutionContext
from core.context import ExecutionContext


class TestGroupedContinuousDistributionSprint2(unittest.TestCase):
    """
    Validates generalized binary indicator comparison:
    - Synthetic data with unfamiliar column names
    - Parity between Pandas and DuckDB executors
    - Negative tests (missing columns, non-binary columns)
    - Router natural language classification
    - Narrative rendering with quartiles and absolute difference
    """

    def setUp(self):
        np.random.seed(42)
        # Synthetic DataFrame 1: String binary labels ("Left" vs "Stayed")
        self.df_hr = pd.DataFrame({
            "employee_id": [f"EMP_{i:04d}" for i in range(100)],
            "emp_compensation": np.concatenate([
                np.random.normal(55000, 12000, 40),  # "Left"
                np.random.normal(82000, 15000, 60),  # "Stayed"
            ]),
            "attrition_status": ["Left"] * 40 + ["Stayed"] * 60,
        })

        # Synthetic DataFrame 2: Numeric binary indicators (0 vs 1)
        self.df_metrics = pd.DataFrame({
            "synthetic_metric": [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0],
            "synthetic_flag": [0, 0, 0, 0, 0, 1, 1, 1, 1, 1],
        })

        # Synthetic DataFrame 3: Boolean indicators (True vs False) with some NaNs
        self.df_latency = pd.DataFrame({
            "response_time_ms": [120.0, 150.0, None, 210.0, 300.0, 420.0, 510.0, 600.0],
            "is_experimental_tier": [False, False, False, False, True, True, True, None],
        })

        self.pandas_exec = PandasExecutor()
        self.duckdb_exec = DuckDBExecutor()
        self.router = master_router

    def test_pandas_and_duckdb_parity_hr(self):
        """Parity test on synthetic HR data with unfamiliar column names."""
        plan = AnalyticalPlan(
            intent="binary_indicator_comparison",
            operation="binary_indicator_comparison",
            target_column="emp_compensation",
            parameters={
                "binary_column": "attrition_status",
                "value_column": "emp_compensation",
            },
            plan=[],
        )

        res_pandas = self.pandas_exec.execute(plan, self.df_hr)
        self.assertTrue(res_pandas.success, f"Pandas failed: {res_pandas.error}")

        self.assertTrue(self.duckdb_exec.can_handle(plan, self.df_hr))
        res_duckdb = self.duckdb_exec.execute(plan, self.df_hr)
        self.assertTrue(res_duckdb.success, f"DuckDB failed: {res_duckdb.error}")

        # Check group counts
        self.assertEqual(res_pandas.value["group_0"]["count"], res_duckdb.value["group_0"]["count"])
        self.assertEqual(res_pandas.value["group_1"]["count"], res_duckdb.value["group_1"]["count"])

        # Check means and medians
        self.assertAlmostEqual(res_pandas.value["group_0"]["mean"], res_duckdb.value["group_0"]["mean"], places=1)
        self.assertAlmostEqual(res_pandas.value["group_0"]["median"], res_duckdb.value["group_0"]["median"], places=1)
        self.assertAlmostEqual(res_pandas.value["group_1"]["mean"], res_duckdb.value["group_1"]["mean"], places=1)
        self.assertAlmostEqual(res_pandas.value["group_1"]["median"], res_duckdb.value["group_1"]["median"], places=1)

        # Check quartiles and IQR
        self.assertAlmostEqual(res_pandas.value["group_0"]["q1"], res_duckdb.value["group_0"]["q1"], places=1)
        self.assertAlmostEqual(res_pandas.value["group_0"]["q3"], res_duckdb.value["group_0"]["q3"], places=1)
        self.assertAlmostEqual(res_pandas.value["group_1"]["q1"], res_duckdb.value["group_1"]["q1"], places=1)
        self.assertAlmostEqual(res_pandas.value["group_1"]["q3"], res_duckdb.value["group_1"]["q3"], places=1)

        # Check differences and absolute differences
        self.assertAlmostEqual(res_pandas.value["difference_in_means"], res_duckdb.value["difference_in_means"], places=1)
        self.assertAlmostEqual(res_pandas.value["difference_in_medians"], res_duckdb.value["difference_in_medians"], places=1)
        self.assertAlmostEqual(res_pandas.value["absolute_difference_in_medians"], res_duckdb.value["absolute_difference_in_medians"], places=1)

    def test_pandas_and_duckdb_parity_numeric_flags(self):
        """Parity test on 0/1 numeric flags with known exact quantiles."""
        plan = AnalyticalPlan(
            intent="binary_indicator_comparison",
            operation="binary_indicator_comparison",
            target_column="synthetic_metric",
            parameters={
                "binary_column": "synthetic_flag",
                "value_column": "synthetic_metric",
            },
            plan=[],
        )

        res_pandas = self.pandas_exec.execute(plan, self.df_metrics)
        res_duckdb = self.duckdb_exec.execute(plan, self.df_metrics)

        self.assertTrue(res_pandas.success)
        self.assertTrue(res_duckdb.success)

        # Group 0: [10, 20, 30, 40, 50] -> med=30, q1=20, q3=40
        self.assertEqual(res_pandas.value["group_0"]["count"], 5)
        self.assertEqual(res_pandas.value["group_0"]["median"], 30.0)
        self.assertEqual(res_pandas.value["group_0"]["q1"], 20.0)
        self.assertEqual(res_pandas.value["group_0"]["q3"], 40.0)
        self.assertEqual(res_pandas.value["group_0"]["iqr"], 20.0)

        # Group 1: [60, 70, 80, 90, 100] -> med=80, q1=70, q3=90
        self.assertEqual(res_pandas.value["group_1"]["count"], 5)
        self.assertEqual(res_pandas.value["group_1"]["median"], 80.0)
        self.assertEqual(res_pandas.value["group_1"]["q1"], 70.0)
        self.assertEqual(res_pandas.value["group_1"]["q3"], 90.0)
        self.assertEqual(res_pandas.value["group_1"]["iqr"], 20.0)

        # Differences
        self.assertEqual(res_pandas.value["difference_in_medians"], 50.0)
        self.assertEqual(res_pandas.value["absolute_difference_in_medians"], 50.0)

        # Exact parity with DuckDB
        self.assertEqual(res_duckdb.value["group_0"]["median"], 30.0)
        self.assertEqual(res_duckdb.value["group_0"]["q1"], 20.0)
        self.assertEqual(res_duckdb.value["group_0"]["q3"], 40.0)
        self.assertEqual(res_duckdb.value["group_1"]["median"], 80.0)
        self.assertEqual(res_duckdb.value["group_1"]["q1"], 70.0)
        self.assertEqual(res_duckdb.value["group_1"]["q3"], 90.0)
        self.assertEqual(res_duckdb.value["absolute_difference_in_medians"], 50.0)

    def test_negative_rejection_missing_columns(self):
        """Negative test: non-existent columns should fail gracefully."""
        plan_bad_val = AnalyticalPlan(
            intent="binary_indicator_comparison",
            operation="binary_indicator_comparison",
            target_column="non_existent_column",
            parameters={"binary_column": "synthetic_flag", "value_column": "non_existent_column"},
            plan=[],
        )
        res_p = self.pandas_exec.execute(plan_bad_val, self.df_metrics)
        self.assertFalse(res_p.success)
        self.assertIn("not found", res_p.error)

        self.assertFalse(self.duckdb_exec.can_handle(plan_bad_val, self.df_metrics))

        plan_bad_bin = AnalyticalPlan(
            intent="binary_indicator_comparison",
            operation="binary_indicator_comparison",
            target_column="synthetic_metric",
            parameters={"binary_column": "invalid_binary_flag", "value_column": "synthetic_metric"},
            plan=[],
        )
        res_p2 = self.pandas_exec.execute(plan_bad_bin, self.df_metrics)
        self.assertFalse(res_p2.success)
        self.assertIn("not found", res_p2.error)

        self.assertFalse(self.duckdb_exec.can_handle(plan_bad_bin, self.df_metrics))

    def test_router_natural_language_routing(self):
        """Test router classification for natural language distribution comparison queries."""
        # Query 1: Synthetic HR
        q1 = "Compare emp_compensation distributions for attrition_status (Left vs Stayed). Report medians and difference."
        ctx1 = ExecutionContext(request_id="test-1", message=q1)
        route1 = self.router._analysis_fast_path(ctx1, q1, df_override=self.df_hr)
        self.assertIsNotNone(route1)
        self.assertEqual(route1.get("intent"), "DATA_OPERATION")
        self.assertEqual(route1.get("operations")[0]["type"], "BINARY_INDICATOR_COMPARISON")
        self.assertEqual(route1.get("operations")[0]["value_column"], "emp_compensation")
        self.assertEqual(route1.get("operations")[0]["binary_column"], "attrition_status")

        # Query 2: Telco Q38 phrasing with synthetic columns
        df_churn_synth = pd.DataFrame({
            "tenure": [1, 10, 20, 30],
            "Churn": ["Yes", "No", "Yes", "No"],
        })
        q2 = "Compare tenure distributions for churned and non-churned customers. Report counts, means, medians, quartiles, and the absolute difference in median tenure."
        ctx2 = ExecutionContext(request_id="test-2", message=q2)
        route2 = self.router._analysis_fast_path(ctx2, q2, df_override=df_churn_synth)
        self.assertIsNotNone(route2)
        self.assertEqual(route2.get("intent"), "DATA_OPERATION")
        self.assertEqual(route2.get("operations")[0]["type"], "BINARY_INDICATOR_COMPARISON")
        self.assertEqual(route2.get("operations")[0]["value_column"], "tenure")
        self.assertEqual(route2.get("operations")[0]["binary_column"], "Churn")

    def test_pipeline_narrative_rendering(self):
        """Test narrative generation includes all required facts and statistics."""
        plan = AnalyticalPlan(
            intent="binary_indicator_comparison",
            operation="binary_indicator_comparison",
            target_column="synthetic_metric",
            parameters={"binary_column": "synthetic_flag", "value_column": "synthetic_metric"},
            plan=[],
        )
        res = self.duckdb_exec.execute(plan, self.df_metrics)
        narrative = analytics_pipeline.build_narrative(res, "synthetic_data.csv")

        self.assertIn("sample count", narrative)
        self.assertIn("mean", narrative)
        self.assertIn("median", narrative)
        self.assertIn("Q1", narrative)
        self.assertIn("Q3", narrative)
        self.assertIn("IQR", narrative)
        self.assertIn("absolute difference in median", narrative)


if __name__ == "__main__":
    unittest.main()
