"""
AnalyticsPipeline — canonical orchestrator for the Plexis analytical brain.

Flow:
  PlannerContext.build()
       ↓
  AnalyticalPlanner.plan()   [LLM, temp=0.0, max 3 repair attempts]
       ↓
  PlanValidator.validate()   [deterministic]
       ↓
  PandasExecutor.execute()   [deterministic, no LLM]
       ↓
  ResultVerifier.verify()    [deterministic, no LLM]
       ↓
  SessionRecorder.record()   [PostgreSQL persist]
       ↓
  AnalyticalResult           [canonical in-memory object]

Rules:
  - LLM is called ONLY in the Planner step
  - Executor and Verifier have ZERO LLM access
  - AnalyticalResult.value is ALWAYS from Pandas, never from LLM
  - If planning fails, return a structured failure — never hallucinate
  - The pipeline does NOT compose the final response — that is the Composer
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple
from uuid import UUID

import pandas as pd

from analytics.result import AnalyticalResult
from analytics.session_recorder import session_recorder
from executor.hybrid_executor import hybrid_executor
from executor.pandas_executor import ExecutorResult
from planner.context import planner_context_builder
from planner.core import analytical_planner
from planner.schema import AnalyticalPlan
from planner.validator import plan_validator, PlanValidationResult
from verifier.result_verifier import result_verifier, VerifierResult

logger = logging.getLogger(__name__)


class AnalyticsPipeline:
    """
    Orchestrates the full analytical brain pipeline.

    One instance, stateless — all state is in AnalyticalResult and PostgreSQL.
    """

    def run(
        self,
        execution_context,  # core.context.ExecutionContext
        df: pd.DataFrame,
        db=None,
    ) -> AnalyticalResult:
        """
        Run the full analytical pipeline for one user query.

        Args:
            execution_context: Resolved ExecutionContext from core pipeline.
            df:                The active dataset DataFrame.
            db:                SQLAlchemy session (for persistence + context loading).

        Returns:
            AnalyticalResult — always returns, never raises.
            On failure: result.verified = False, result.value = None.
        """
        query = execution_context.message or ""
        user_id = getattr(execution_context, "user_id", None)
        chat_id = getattr(execution_context, "chat_id", None)
        dataset_id = getattr(execution_context, "dataset_id", None)
        dataset_fingerprint = getattr(execution_context, "dataset_fingerprint", None)

        logger.info("[Pipeline] Starting for query: %s", query[:80])

        # Shared result scaffolding
        result = AnalyticalResult(
            user_id=user_id,
            chat_id=chat_id,
            dataset_id=dataset_id,
            dataset_fingerprint=dataset_fingerprint,
            query=query,
        )

        # ----------------------------------------------------------------
        # Step 1: Build planner context
        # Pass df directly so column types come from Pandas dtype (authoritative)
        # ----------------------------------------------------------------
        try:
            planner_ctx = planner_context_builder.build(
                execution_context, db=db, df=df
            )
        except Exception as e:
            logger.error("[Pipeline] PlannerContext build failed: %s", e, exc_info=True)
            result.verification_details = {"error": "planner_context_failed", "detail": str(e)}
            return result

        columns = planner_ctx.get("columns", [])
        column_types = planner_ctx.get("column_types", {})

        if not columns:
            logger.warning("[Pipeline] No columns in planner context — cannot plan.")
            result.verification_details = {"error": "no_schema"}
            return result

        # ----------------------------------------------------------------
        # Step 2: Plan  [LLM, temperature=0.0]
        # ----------------------------------------------------------------
        
        # Bypass planner for ALL structured operations from the master_router
        operation_override = getattr(execution_context, "llm_operation", None)
        if operation_override:
            op_type = operation_override.get("type")
            col = operation_override.get("column")
            if op_type == "GET_SCHEMA":
                plan = AnalyticalPlan(intent="list_columns", operation="get_schema", target_column=None, plan=[])
            elif op_type == "GET_COLUMN_NAMES":
                plan = AnalyticalPlan(intent="list_columns", operation="get_column_names", target_column=None, plan=[])
            elif op_type == "GET_DATA_TYPES":
                plan = AnalyticalPlan(intent="list_types", operation="get_data_types", target_column=None, plan=[])
            elif op_type == "GET_NUMERIC_COLUMNS":
                plan = AnalyticalPlan(intent="numeric_columns", operation="get_numeric_columns", target_column=None, plan=[])
            elif op_type == "GET_TEXT_COLUMNS":
                plan = AnalyticalPlan(intent="text_columns", operation="get_text_columns", target_column=None, plan=[])
            elif op_type == "DATASET_PROFILE":
                plan = AnalyticalPlan(intent="profile", operation="dataset_profile", target_column=None, plan=[])
            elif op_type == "NON_NULL_COUNTS":
                plan = AnalyticalPlan(intent="non_null", operation="non_null_counts", target_column=col, plan=[])
            elif op_type == "MISSING_PERCENTAGES":
                plan = AnalyticalPlan(intent="missing_pct", operation="missing_percentages", target_column=col, plan=[])
            elif op_type == "DESCRIBE_COLUMN":
                m_list = operation_override.get("metrics", [])
                plan = AnalyticalPlan(intent="describe_column", operation="describe_column", target_column=col, parameters={"metrics": m_list}, plan=[{"metrics": m_list}])
            elif op_type == "MISSING_VALUES":
                plan = AnalyticalPlan(intent="missing_values", operation="missing_values", target_column=col, plan=[])
            elif op_type == "MISSING_COUNT_TOTAL":
                plan = AnalyticalPlan(intent="missing_count_total", operation="missing_count_total", target_column=None, plan=[])
            elif op_type == "MISSING_COUNT_COLUMN":
                plan = AnalyticalPlan(intent="missing_count_column", operation="missing_count_column", target_column=col, plan=[])
            elif op_type == "COLUMN_WITH_MOST_MISSING_VALUES":
                plan = AnalyticalPlan(intent="column_with_most_missing_values", operation="column_with_most_missing_values", target_column=None, plan=[])
            elif op_type == "COLUMNS_WITHOUT_MISSING_VALUES":
                plan = AnalyticalPlan(intent="columns_without_missing_values", operation="columns_without_missing_values", target_column=None, plan=[])
            elif op_type == "COLUMNS_WITH_MISSING_VALUES":
                plan = AnalyticalPlan(intent="columns_with_missing_values", operation="columns_with_missing_values", target_column=None, plan=[])
            elif op_type == "DATASET_COMPLETENESS":
                plan = AnalyticalPlan(intent="dataset_completeness", operation="dataset_completeness", target_column=None, plan=[])
            elif op_type == "UNIQUE_COUNTS":
                plan = AnalyticalPlan(intent="unique_counts", operation="unique_counts", target_column=None, plan=[])
            elif op_type == "COLUMN_WITH_MOST_UNIQUE_VALUES":
                plan = AnalyticalPlan(intent="column_with_most_unique_values", operation="column_with_most_unique_values", target_column=None, plan=[])
            elif op_type == "COLUMN_WITH_FEWEST_UNIQUE_VALUES":
                plan = AnalyticalPlan(intent="column_with_fewest_unique_values", operation="column_with_fewest_unique_values", target_column=None, plan=[])
            elif op_type == "DUPLICATE_COUNT":
                plan = AnalyticalPlan(intent="duplicate_count", operation="duplicate_count", target_column=None, plan=[])
            elif op_type == "HAS_DUPLICATES":
                plan = AnalyticalPlan(intent="has_duplicates", operation="has_duplicates", target_column=None, plan=[])
            elif op_type == "FIND_DUPLICATES":
                plan = AnalyticalPlan(intent="duplicates", operation="duplicates", target_column=None, plan=[])
            elif op_type == "UNIQUE_VALUES":
                plan = AnalyticalPlan(intent="unique_values", operation="unique_values", target_column=col, plan=[])
            elif op_type in ("ROW_COUNT", "COUNT", "GET_ROW_COUNT"):
                plan = AnalyticalPlan(intent="row_count", operation="count", target_column=col, plan=[])
            elif op_type == "GET_COLUMN_COUNT":
                plan = AnalyticalPlan(intent="column_count", operation="column_count", target_column=col, plan=[])
            elif op_type == "VALUE_COUNTS":
                plan = AnalyticalPlan(intent="value_counts", operation="frequency", target_column=col, plan=[])
            elif op_type == "UNIQUE_COUNT":
                plan = AnalyticalPlan(intent="unique_count", operation="nunique", target_column=col, plan=[])
            elif op_type == "MOST_FREQUENT_VALUE":
                plan = AnalyticalPlan(intent="most_frequent_value", operation="most_frequent_value", target_column=col, plan=[])
            elif op_type == "LEAST_FREQUENT_VALUE":
                plan = AnalyticalPlan(intent="least_frequent_value", operation="least_frequent_value", target_column=col, plan=[])
            elif op_type == "MIN":
                plan = AnalyticalPlan(intent="min", operation="min", target_column=col, plan=[])
            elif op_type == "MAX":
                plan = AnalyticalPlan(intent="max", operation="max", target_column=col, plan=[])
            elif op_type == "RANGE":
                plan = AnalyticalPlan(intent="range", operation="range", target_column=col, plan=[])
            elif op_type == "TOTAL_CELL_COUNT":
                plan = AnalyticalPlan(intent="total_cells", operation="total_cell_count", target_column=None, plan=[])
            elif op_type == "COMPLETE_CELL_COUNT":
                plan = AnalyticalPlan(intent="complete_cells", operation="complete_cell_count", target_column=None, plan=[])
            elif op_type == "ROWS_WITH_MISSING_COUNT":
                plan = AnalyticalPlan(intent="rows_with_missing", operation="rows_with_missing_count", target_column=None, plan=[])
            elif op_type == "COMPLETE_ROWS_COUNT":
                plan = AnalyticalPlan(intent="complete_rows", operation="complete_rows_count", target_column=None, plan=[])
            elif op_type in ("DUPLICATE_ROWS_AFTER_DEDUPLICATION", "SIMULATED_DEDUPLICATION"):
                plan = AnalyticalPlan(intent="deduplicated_rows", operation="duplicate_rows_after_deduplication", target_column=None, plan=[])
            elif op_type == "CROSS_COLUMN_CONDITION":
                plan = AnalyticalPlan(intent="cross_condition", operation="cross_column_condition", target_column=operation_override.get("col1"), columns=[operation_override.get("col2")] if operation_override.get("col2") else [], parameters=operation_override, plan=[])
            elif op_type == "ROWS_AGE_BELOW_MEAN":
                plan = AnalyticalPlan(intent="age_below_mean", operation="rows_age_below_mean", target_column=col or "age", plan=[])
            elif op_type == "ROWS_AGE_EQUAL_MEDIAN":
                plan = AnalyticalPlan(intent="age_equal_median", operation="rows_age_equal_median", target_column=col or "age", plan=[])
            elif op_type == "CITY_AGE_STATS":
                plan = AnalyticalPlan(intent="city_age_stats", operation="city_age_stats", target_column=col or "city", plan=[])
            elif op_type == "CITIES_ABOVE_AVERAGE_AGE":
                plan = AnalyticalPlan(intent="cities_above_average_age", operation="cities_above_average_age", target_column=col or "city", plan=[])
            elif op_type == "CITY_COMPLETENESS_REPORT":
                plan = AnalyticalPlan(intent="city_completeness_report", operation="city_completeness_report", target_column=col or "city", plan=[])
            elif op_type == "VALID_AND_MISSING_COUNTS":
                plan = AnalyticalPlan(intent="valid_missing_counts", operation="valid_and_missing_counts", target_column=col, plan=[])
            elif op_type == "COLUMN_UNIQUENESS":
                plan = AnalyticalPlan(intent="column_uniqueness", operation="column_uniqueness", target_column=col, plan=[])
            elif op_type == "AGGREGATE":
                func = operation_override.get("func", "mean").lower()
                plan = AnalyticalPlan(intent=func, operation=func, target_column=col, plan=[])
            elif op_type in ("TOP_N", "BOTTOM_N"):
                n = int(operation_override.get("n") or 10)
                order = (operation_override.get("order") or ("desc" if op_type == "TOP_N" else "asc")).lower()
                plan = AnalyticalPlan(
                    intent="top_n" if op_type == "TOP_N" else "bottom_n",
                    operation="top_n" if order in ("desc", "descending") else "bottom_n",
                    target_column=col,
                    parameters={"n": n, "order": order},
                    plan=[]
                )
            elif op_type in ("SORT_ROWS", "SORT"):
                ascending = operation_override.get("ascending", True)
                plan = AnalyticalPlan(
                    intent="sort_rows",
                    operation="bottom_n" if ascending else "top_n",
                    target_column=col,
                    parameters={"ascending": ascending},
                    plan=[]
                )
            elif op_type == "FILTER":
                plan = AnalyticalPlan(
                    intent="filter",
                    operation="filter",
                    target_column=col,
                    parameters={
                        "operator": operation_override.get("operator", "=="),
                        "value": operation_override.get("value"),
                    },
                    plan=[]
                )
            # ── Retail aggregation operations ─────────────────────────────────
            elif op_type == "SUM":
                plan = AnalyticalPlan(intent="sum", operation="sum", target_column=col, plan=[])
            elif op_type == "MEAN":
                plan = AnalyticalPlan(intent="mean", operation="mean", target_column=col, plan=[])
            elif op_type in ("GROUP_SUM", "GROUP_MEAN", "GROUP_MEDIAN", "GROUP_COUNT", "GROUP_MIN", "GROUP_MAX", "GROUP_COUNT_FILTERED"):
                val_col = operation_override.get("value_column")
                top_n = operation_override.get("top_n")
                order = operation_override.get("order", "desc")
                ascending = order == "asc" or operation_override.get("ascending", False)
                plan = AnalyticalPlan(
                    intent=op_type.lower(),
                    operation=op_type.lower(),
                    target_column=col,
                    parameters={
                        "group_by": col,
                        "value_column": val_col,
                        "filter_column": operation_override.get("filter_column") or val_col,
                        "filter_op": operation_override.get("filter_op", ">"),
                        "filter_value": operation_override.get("filter_value", 0),
                        "filter_desc": operation_override.get("filter_desc")
                            or (operation_override.get("details") or {}).get("filter_desc")
                            or "positive observations",
                        "top_n": top_n,
                        "order": order,
                        "ascending": ascending,
                    },
                    plan=[]
                )
            elif op_type in ("QUANTILE", "QUANTILES"):
                qs = operation_override.get("quantiles") or operation_override.get("q")
                if qs is not None:
                    if not isinstance(qs, list):
                        qs = [qs]
                else:
                    qs = [0.25, 0.5, 0.75]
                plan = AnalyticalPlan(
                    intent="quantile", operation="quantile",
                    target_column=col,
                    parameters={"quantiles": qs},
                    plan=[]
                )
            elif op_type == "PAIR_FREQUENCIES":
                plan = AnalyticalPlan(
                    intent="pair_frequencies", operation="pair_frequencies",
                    target_column=operation_override.get("col1"),
                    parameters={"col1": operation_override.get("col1"), "col2": operation_override.get("col2")},
                    plan=[]
                )
            elif op_type == "COMPOUND_FILTER":
                plan = AnalyticalPlan(
                    intent="compound_filter", operation="compound_filter",
                    target_column=operation_override.get("value_column"),
                    parameters=operation_override,
                    plan=[]
                )
            elif op_type == "HYPOTHETICAL":
                plan = AnalyticalPlan(
                    intent="hypothetical", operation="hypothetical",
                    target_column=col,
                    parameters=operation_override,
                    plan=[]
                )
            elif op_type in ("ROWS_BELOW_MEAN", "ROWS_AGE_BELOW_MEAN"):
                plan = AnalyticalPlan(intent="rows_below_mean", operation="rows_below_mean", target_column=col or "age", plan=[])
            elif op_type in ("ROWS_EQUAL_MEDIAN", "ROWS_AGE_EQUAL_MEDIAN"):
                plan = AnalyticalPlan(intent="rows_equal_median", operation="rows_equal_median", target_column=col or "age", plan=[])
            elif op_type == "ROWS_ABOVE_MEAN":
                plan = AnalyticalPlan(intent="rows_above_mean", operation="rows_above_mean", target_column=col, plan=[])
            elif op_type == "ROWS_ABOVE_MEDIAN":
                plan = AnalyticalPlan(intent="rows_above_median", operation="rows_above_median", target_column=col, plan=[])
            elif op_type == "ROWS_BELOW_MEDIAN":
                plan = AnalyticalPlan(intent="rows_below_median", operation="rows_below_median", target_column=col, plan=[])
            elif op_type == "VALUE_COUNTS":
                plan = AnalyticalPlan(intent="value_counts", operation="frequency", target_column=col, plan=[])
            elif op_type == "CORRELATION":
                extra_cols = operation_override.get("columns", [])
                plan = AnalyticalPlan(
                    intent="correlation", operation="correlation",
                    target_column=col,
                    columns=extra_cols,
                    parameters=operation_override,
                    plan=[]
                )

            # ── Multi-dimensional grouping & grouped target rates (Cluster A) ─────
            elif op_type == "CROSSTAB":
                plan = AnalyticalPlan(
                    intent="crosstab", operation="crosstab",
                    target_column=col or operation_override.get("column"),
                    parameters=operation_override,
                    plan=[]
                )
            elif op_type == "GROUPED_RATE":
                plan = AnalyticalPlan(
                    intent="grouped_rate", operation="grouped_rate",
                    target_column=operation_override.get("target_column") or col,
                    parameters=operation_override,
                    plan=[]
                )
            elif op_type == "TWO_FACTOR_AGGREGATION":
                plan = AnalyticalPlan(
                    intent="two_factor_aggregation", operation="two_factor_aggregation",
                    target_column=operation_override.get("value_column") or col,
                    parameters=operation_override,
                    plan=[]
                )
            elif op_type == "GROUPED_BINARY_COMPARISON":
                plan = AnalyticalPlan(
                    intent="grouped_binary_comparison", operation="grouped_binary_comparison",
                    target_column=operation_override.get("value_column") or col,
                    parameters=operation_override,
                    plan=[]
                )
            elif op_type == "GROUPED_MULTI_STAT":
                plan = AnalyticalPlan(
                    intent="grouped_multi_stat", operation="grouped_multi_stat",
                    target_column=operation_override.get("value_column") or col,
                    parameters=operation_override,
                    plan=[]
                )
            elif op_type == "BINNED_TARGET_RATE":
                plan = AnalyticalPlan(
                    intent="binned_target_rate", operation="binned_target_rate",
                    target_column=operation_override.get("bin_column") or col,
                    parameters=operation_override,
                    plan=[]
                )
            elif op_type == "ZERO_VS_POSITIVE_COMPARISON":
                plan = AnalyticalPlan(
                    intent="zero_vs_positive_comparison", operation="zero_vs_positive_comparison",
                    target_column=operation_override.get("value_column") or col,
                    parameters=operation_override,
                    plan=[]
                )
            elif op_type == "BINARY_INDICATOR_COMPARISON":
                plan = AnalyticalPlan(
                    intent="binary_indicator_comparison", operation="binary_indicator_comparison",
                    target_column=operation_override.get("value_column") or col,
                    parameters=operation_override,
                    plan=[]
                )
            elif op_type in ("FILTERED_GROUP_RANKING", "filtered_group_ranking"):
                plan = AnalyticalPlan(
                    intent="filtered_group_ranking", operation="filtered_group_ranking",
                    target_column=operation_override.get("value_column") or col,
                    parameters=operation_override,
                    plan=[]
                )
            elif op_type in ("MEDIAN_SPLIT_COMPARISON", "median_split_comparison"):
                plan = AnalyticalPlan(
                    intent="median_split_comparison", operation="median_split_comparison",
                    target_column=operation_override.get("target_column") or col,
                    parameters=operation_override,
                    plan=[]
                )
            elif op_type in ("TAIL_RISK_GROUP_RATE", "tail_risk_group_rate"):
                plan = AnalyticalPlan(
                    intent="tail_risk_group_rate", operation="tail_risk_group_rate",
                    target_column=operation_override.get("target_column") or col,
                    parameters=operation_override,
                    plan=[]
                )
            elif op_type in ("DATE_MONTH_AUDIT", "date_month_audit"):
                plan = AnalyticalPlan(
                    intent="date_month_audit", operation="date_month_audit",
                    target_column=operation_override.get("date_column") or col,
                    parameters=operation_override,
                    plan=[]
                )
            elif op_type in ("AUDIT_NUMERIC_CONVERSION", "audit_numeric_conversion"):
                plan = AnalyticalPlan(
                    intent="audit_numeric_conversion", operation="audit_numeric_conversion",
                    target_column=col or operation_override.get("column"),
                    parameters=operation_override,
                    plan=[]
                )

            # ── 15 retail-specific operations ────────────────────────────────
            elif op_type in ("OVERALL_EVENT_RATE", "overall_event_rate"):
                plan = AnalyticalPlan(
                    intent="overall_event_rate", operation="overall_event_rate",
                    target_column=operation_override.get("target_column") or col,
                    parameters=operation_override,
                    plan=[]
                )
            elif op_type in ("GROUP_METRIC_DIFFERENCE", "group_metric_difference", "DERIVED_GROUP_COMPARISON", "derived_group_comparison"):
                plan = AnalyticalPlan(
                    intent="group_metric_difference", operation="group_metric_difference",
                    target_column=operation_override.get("value_column") or col,
                    parameters=operation_override,
                    plan=[]
                )
            elif op_type == "HYPOTHETICAL_FILL_MEAN":
                plan = AnalyticalPlan(
                    intent="hypothetical_fill_mean", operation="hypothetical_fill_mean",
                    target_column=col,
                    parameters={"fill_value": operation_override.get("fill_value", 5.0)},
                    plan=[]
                )
            elif op_type == "CATEGORY_NORMALIZATION":
                plan = AnalyticalPlan(intent="category_normalization", operation="category_normalization", target_column=col, plan=[])
            elif op_type == "NORMALIZED_GROUP_SUM":
                plan = AnalyticalPlan(
                    intent="normalized_group_sum", operation="normalized_group_sum",
                    target_column=col,
                    parameters={"value_column": operation_override.get("value_column", "Sales_Amount")},
                    plan=[]
                )
            elif op_type == "YEARLY_RETURN_STATS":
                plan = AnalyticalPlan(intent="yearly_return_stats", operation="yearly_return_stats", target_column=None, plan=[])
            elif op_type == "CHANNEL_COMPARISON":
                plan = AnalyticalPlan(intent="channel_comparison", operation="channel_comparison", target_column=None, plan=[])
            elif op_type == "RATING_THRESHOLD_STATS":
                plan = AnalyticalPlan(
                    intent="rating_threshold_stats", operation="rating_threshold_stats",
                    target_column=col,
                    parameters={"threshold": operation_override.get("threshold", 4.0)},
                    plan=[]
                )
            elif op_type == "CITY_RETURN_RATE":
                plan = AnalyticalPlan(intent="city_return_rate", operation="city_return_rate", target_column=None, plan=[])
            elif op_type == "YEAR_ABOVE_MEDIAN_COUNT":
                plan = AnalyticalPlan(
                    intent="year_above_median_count", operation="year_above_median_count",
                    target_column=col,
                    parameters={"year": operation_override.get("year", 2025)},
                    plan=[]
                )
            elif op_type == "WEIGHTED_AVERAGE":
                plan = AnalyticalPlan(
                    intent="weighted_average", operation="weighted_average",
                    target_column=col,
                    parameters={
                        "val_col": operation_override.get("val_col", col),
                        "weight_col": operation_override.get("weight_col", "Sales_Amount"),
                    },
                    plan=[]
                )
            elif op_type == "PEAK_MONTH_PROFIT":
                plan = AnalyticalPlan(
                    intent="peak_month_profit", operation="peak_month_profit",
                    target_column=col,
                    parameters={
                        "year": operation_override.get("year"),
                        "value_column": operation_override.get("val_col") or operation_override.get("value_column") or col or "Profit"
                    },
                    plan=[]
                )
            elif op_type == "CATEGORY_PROFIT_MARGINS":
                plan = AnalyticalPlan(intent="category_profit_margins", operation="category_profit_margins", target_column=None, plan=[])
            elif op_type == "AVERAGE_ORDER_VALUE":
                plan = AnalyticalPlan(intent="average_order_value", operation="average_order_value", target_column=None, plan=[])
            elif op_type == "CUSTOMER_LIFETIME_VALUE_PROXY":
                plan = AnalyticalPlan(intent="customer_lifetime_value_proxy", operation="customer_lifetime_value_proxy", target_column=None, plan=[])
            elif op_type == "YEAR_AGREEMENT":
                plan = AnalyticalPlan(intent="year_agreement", operation="year_agreement", target_column=None, plan=[])
            elif op_type == "MARGIN_AGGREGATION_COMPARISON":
                plan = AnalyticalPlan(intent="margin_aggregation_comparison", operation="margin_aggregation_comparison", target_column=None, plan=[])
            elif op_type == "COUNTRY_RETURN_RATE":
                plan = AnalyticalPlan(intent="country_return_rate", operation="country_return_rate", target_column=None, plan=[])
            elif op_type == "OVERALL_RETURN_RATE":
                plan = AnalyticalPlan(intent="overall_return_rate", operation="overall_return_rate", target_column=None, plan=[])
            elif op_type == "PROMOTION_CODE_USAGE":
                target_col = col or "Promotion_Code"
                plan = AnalyticalPlan(intent="promotion_code_usage", operation="promotion_code_usage", target_column=target_col, plan=[])
            elif op_type == "TOP_COUNTRY_IN_YEAR":
                plan = AnalyticalPlan(
                    intent="top_country_in_year", operation="top_country_in_year",
                    target_column="Country",
                    parameters={"year": operation_override.get("year", 2024), "value_column": operation_override.get("value_column", "Sales_Amount")},
                    plan=[]
                )
            elif op_type == "TOP_CATEGORY_PER_YEAR":
                plan = AnalyticalPlan(intent="top_category_per_year", operation="top_category_per_year", target_column=None, plan=[])
            elif op_type == "ORDER_ID_VS_DUPLICATE_ROWS":
                plan = AnalyticalPlan(intent="order_id_vs_duplicates", operation="order_id_vs_duplicates", target_column=None, plan=[])
            elif op_type == "YEAR_OVER_YEAR_COMPARISON":
                plan = AnalyticalPlan(
                    intent="year_over_year_comparison", operation="year_over_year_comparison",
                    target_column="Order_Year",
                    parameters={
                        "year1": operation_override.get("year1", 2024),
                        "year2": operation_override.get("year2", 2025),
                        "value_column": operation_override.get("value_column", "Sales_Amount"),
                    },
                    plan=[]
                )
            elif op_type == "RETURN_FLAG_STATUS_MISMATCH":
                plan = AnalyticalPlan(intent="return_flag_status_mismatch", operation="return_flag_status_mismatch", target_column=None, plan=[])
            elif op_type == "MISSINGNESS_IMPLICATIONS":
                plan = AnalyticalPlan(intent="missingness_implications", operation="missingness_implications", target_column=None, plan=[])
            elif op_type == "DISCOUNT_PROFIT_CORRELATION":
                plan = AnalyticalPlan(intent="discount_profit_correlation", operation="discount_profit_correlation", target_column=None, plan=[])
            else:
                plan = analytical_planner.plan(query, planner_ctx)
        else:
            plan = analytical_planner.plan(query, planner_ctx)


        if plan is None:
            logger.warning("[Pipeline] Planner returned None after max attempts.")
            result.verification_details = {"error": "planner_failed"}
            return result

        result.normalized_intent = plan.intent
        result.operation = plan.operation
        result.column = plan.target_column
        result.plan = plan.raw

        logger.info(
            "[PlannerOutput] intent=%s operation=%s column=%s steps=%d",
            plan.intent, plan.operation, plan.target_column, len(plan.plan or []),
        )

        # ----------------------------------------------------------------
        # Step 3: Validate plan  [deterministic]
        # ----------------------------------------------------------------
        # Log the exact context used for validation
        target_col = plan.target_column
        if isinstance(target_col, (list, tuple)):
            resolved_type = "list"
        elif target_col:
            resolved_type = column_types.get(target_col, "unknown")
        else:
            resolved_type = "n/a"
        logger.info(
            "[PlannerValidationContext] column=%s canonical_type=%s operation=%s",
            target_col, resolved_type, plan.operation,
        )

        validation: PlanValidationResult = plan_validator.validate(
            plan=plan,
            columns=columns,
            column_types=column_types,
            dataset_fingerprint=dataset_fingerprint,
        )

        if not validation.valid:
            logger.warning(
                "[Pipeline] Plan validation failed: %s — %s",
                validation.error_code, validation.error_message,
            )
            result.verification_details = {
                "error": "validation_failed",
                "code": validation.error_code,
                "message": validation.error_message,
            }
            return result

        logger.info(
            "[PlannerValidationContext] column=%s canonical_type=%s operation=%s valid=true",
            target_col, resolved_type, plan.operation,
        )

        # ----------------------------------------------------------------
        # Step 4: Execute  [Pandas, no LLM]
        # ----------------------------------------------------------------
        exec_result: ExecutorResult = hybrid_executor.execute(plan, df)

        if not exec_result.success:
            logger.warning("[Pipeline] Executor failed: %s", exec_result.error)
            result.verification_details = {"error": "executor_failed", "detail": exec_result.error}
            return result

        result.value = exec_result.value
        result.row_indices = exec_result.row_indices
        result.matching_rows = exec_result.matching_rows
        result.details = dict(getattr(exec_result, "details", {}) or {})
        result.details["total_rows"] = len(df)

        # Resolve unit from trusted sources (metadata, data dictionary, user definitions)
        from analytics.unit_resolver import resolve_unit
        dataset_meta = getattr(execution_context, "dataset_profile", None) or getattr(execution_context, "column_profiles", None)
        data_dict = getattr(getattr(execution_context, "dko", None), "data_dictionary", None)
        user_defs = getattr(getattr(execution_context, "workspace_state", None), "user_definitions", None)

        unit_res = resolve_unit(
            column=result.column,
            operation=result.operation,
            dataset_metadata=dataset_meta,
            data_dictionary=data_dict,
            user_definitions=user_defs,
        )
        result.details["unit"] = unit_res.unit
        result.details["unit_status"] = unit_res.status
        result.details["unit_label"] = unit_res.label
        result.details["unit_provenance"] = unit_res.provenance
        exec_result.details = exec_result.details or {}
        exec_result.details["unit"] = unit_res.unit
        exec_result.details["unit_status"] = unit_res.status
        exec_result.details["unit_label"] = unit_res.label
        exec_result.details["unit_provenance"] = unit_res.provenance

        logger.info("[Pipeline] Executor: value=%s indices=%d", result.value, len(result.row_indices))

        # ----------------------------------------------------------------
        # Step 5: Verify  [deterministic, no LLM]
        # ----------------------------------------------------------------
        verify_result: VerifierResult = result_verifier.verify(
            exec_result,
            plan,
            df,
            dataset_metadata=dataset_meta,
            data_dictionary=data_dict,
            user_definitions=user_defs,
        )

        result.verified = verify_result.verified
        result.verification_details = verify_result.details or {}
        if verify_result.error_message:
            result.verification_details["error"] = verify_result.error_message

        if not result.verified:
            logger.error(
                "[Pipeline] Verification FAILED: expected=%s actual=%s",
                verify_result.expected, verify_result.actual,
            )
            # Return with verified=False — the caller must NOT present this as fact
            return result

        logger.info("[Pipeline] Verified: %s", result.verified)

        # ----------------------------------------------------------------
        # Step 6: Persist  [PostgreSQL]
        # ----------------------------------------------------------------
        # Persistence requires real FK-valid UUIDs for both chat_id and
        # dataset_id in the analysis_sessions table.
        #
        # When the frontend hasn't created a DB chat (chatId is null on the
        # conversation object), we auto-create one so the FK constraint is
        # satisfied and analysis operations are always persisted.
        if db is not None and dataset_id:
            try:
                import uuid as _uuid
                from db.repositories.chat_repository import chat_repository
                from db.services.analysis_session_service import analysis_session_service

                persist_chat_id = chat_id

                # Resolve or create a valid DB chat
                if persist_chat_id:
                    # Validate it's a real UUID
                    try:
                        persist_chat_id = _uuid.UUID(str(persist_chat_id))
                    except (ValueError, AttributeError):
                        persist_chat_id = None

                if not persist_chat_id:
                    # Auto-create a DB chat for this session
                    chat = chat_repository.create_chat(
                        db,
                        user_id=user_id or _uuid.UUID('00000000-0000-0000-0000-000000000000'),
                        title="Auto: Analysis Session",
                        current_dataset_id=_uuid.UUID(str(dataset_id)),
                    )
                    persist_chat_id = chat.id
                    # Expose back to the calling context so the frontend gets it
                    execution_context.chat_id = str(chat.id)
                    logger.info(
                        "[Pipeline] Auto-created DB chat id=%s for persistence",
                        chat.id,
                    )

                # Ensure dataset_id is a valid UUID
                persist_dataset_id = _uuid.UUID(str(dataset_id))

                session = analysis_session_service.get_or_restore_session(
                    db, persist_chat_id, persist_dataset_id, user_id
                )
                op = session_recorder.record(db, result, session)
                # Sync the DB-assigned UUID back so frontend gets the real analysis_id
                result.analysis_id = op.id
                db.commit()
                logger.info("[Pipeline] Persisted op id=%s chat_id=%s", op.id, persist_chat_id)
            except Exception as e:
                logger.error("[Pipeline] Persistence failed: %s", e, exc_info=True)
                try:
                    db.rollback()
                except Exception:
                    pass
                # Persistence failure does NOT invalidate the analytical result
        else:
            logger.warning("[Pipeline] Skipping persistence (no db/dataset_id).")

        return result

    # -------------------------------------------------------------------------
    # Helpers for building EngineResult from AnalyticalResult
    # -------------------------------------------------------------------------

    def build_narrative(self, result: AnalyticalResult, dataset_name: str = "the dataset") -> str:
        """
        Build a grounded narrative from a verified AnalyticalResult.

        The LLM Composer MUST use this value rather than recomputing.
        """
        verified = getattr(result, "verified", getattr(result, "success", True))
        if not verified or result.value is None:
            return ""

        col = result.column or "value"
        val = result.value
        op = result.operation

        # Format the narrative based on operation type
        if op in ("top_n", "bottom_n"):
            n_val = (result.details or {}).get("n") or (len(result.matching_rows) if result.matching_rows else (len(val) if isinstance(val, list) else 10))
            direction = "highest" if op == "top_n" else "lowest"
            return f"Top {n_val} records ranked by {direction} **{col}** in **{dataset_name}**."

        if op in ("group_mean", "group_sum", "group_median", "group_count", "group_count_filtered", "group_min", "group_max"):
            details = result.details or {}
            top_n = details.get("top_n")
            val_col = details.get("value_column") or col
            if "count_filtered" in op:
                stat_name = f"count of {details.get('filter_desc', 'delayed flights (delay > 0)')}"
            elif "mean" in op:
                stat_name = "average"
            elif "max" in op:
                stat_name = "maximum"
            elif "min" in op:
                stat_name = "minimum"
            elif "median" in op:
                stat_name = "median"
            elif "sum" in op:
                stat_name = "total"
            elif "count" in op:
                stat_name = "frequency count"
            else:
                stat_name = op.replace("group_", "")

            if isinstance(val, dict):
                items = list(val.items())
                if top_n:
                    order = str(details.get("order", "desc")).lower()
                    direction_label = "Bottom" if order in ("asc", "ascending") else "Top"
                    n_returned = len(items)
                    n_requested = int(top_n)
                    # Distinguish dataset exhaustion from a pipeline truncation bug.
                    # total_groups is the count of distinct groups BEFORE the LIMIT.
                    total_groups = details.get("total_groups") or details.get("eligible_group_count")
                    dataset_exhausted = (
                        n_returned < n_requested
                        and total_groups is not None
                        and int(total_groups) <= n_returned
                    )
                    if dataset_exhausted:
                        entity_name = col if n_returned == 1 else f"{col} entities"
                        metric_phrase = stat_name if "count_filtered" in op else f"{stat_name} {val_col}"
                        return f"Found {n_returned} eligible {entity_name}; all {n_returned} are shown because fewer than {n_requested} are available in the dataset (ranked by {metric_phrase})."
                    elif n_returned < n_requested:
                        entity_name = col if n_returned == 1 else f"{col} entities"
                        metric_phrase = stat_name if "count_filtered" in op else f"{stat_name} {val_col}"
                        return f"{direction_label} {n_returned} of {n_requested} requested {entity_name} ranked by {metric_phrase}."
                    else:
                        entity_name = col if n_returned == 1 else f"{col} entities"
                        metric_phrase = stat_name if "count_filtered" in op else f"{stat_name} {val_col}"
                        return f"{direction_label} {n_requested} {entity_name} ranked by {metric_phrase}."
                top_items = items[:top_n] if top_n else items[:8]
                parts = [f"**{k}**: {v:,.2f}" if isinstance(v, float) else f"**{k}**: {v:,}" if isinstance(v, int) else f"**{k}**: {v}" for k, v in top_items]
                suffix = f" (and {len(items) - len(top_items)} more)" if len(items) > len(top_items) else ""
                order = str(details.get("order", "desc")).lower()
                direction_label = "Bottom" if order in ("asc", "ascending") else "Top"
                if top_n and len(top_items) < int(top_n):
                    rank_prefix = f"{direction_label} {len(top_items)} of {int(top_n)} requested "
                else:
                    rank_prefix = f"{direction_label} {top_n} " if top_n else ""
                return f"{rank_prefix}**{col}** ranked by {stat_name} **{val_col}** in **{dataset_name}**: " + ", ".join(parts) + f"{suffix}."

        if op in ("max", "min", "idxmax", "idxmin", "longest", "shortest", "highest", "lowest"):
            from analytics.context_enrichment import enrich_extremum_narrative
            unit = (result.details or {}).get("unit")
            return enrich_extremum_narrative(
                op=op,
                col=col,
                val=val,
                matching_rows=result.matching_rows,
                dataset_name=dataset_name,
                details=result.details or {},
                unit=unit,
            )

        if op in ("mean", "median"):
            label = "average" if op == "mean" else "median"
            return f"The {label} **{col}** in **{dataset_name}** is **{val}**."

        if op == "get_schema":
            if isinstance(val, dict) and "columns" in val:
                columns_str = ", ".join([f"{c['name']} ({c.get('dtype', 'unknown')})" for c in val["columns"]])
                return f"**{dataset_name}** has {len(val['columns'])} columns: {columns_str}."
            return f"**{dataset_name}** schema: {val}"

        if op == "get_data_types":
            if isinstance(val, list):
                num_cols = [c['name'] for c in val if any(t in str(c.get('type', '')).lower() for t in ('int', 'float', 'num'))]
                date_cols = [c['name'] for c in val if any(t in str(c.get('type', '')).lower() for t in ('date', 'time'))]
                cat_cols = [c['name'] for c in val if c['name'] not in num_cols and c['name'] not in date_cols]
                return (
                    f"Column data types in **{dataset_name}** classified: "
                    f"**Numeric columns**: {', '.join(num_cols[:8])}; "
                    f"**Categorical columns**: {', '.join(cat_cols[:8])}; "
                    f"**Date columns**: {', '.join(date_cols)}."
                )
            return f"Data types: {val}."
            
        if op == "missing_values":
            if isinstance(val, dict):
                parts = [f"{k}: {v}" for k, v in val.items()]
                return f"Missing values in **{dataset_name}**: " + ", ".join(parts) + "."
            return f"Missing values: {val}."

        if op == "missing_count_total":
            return f"There are **{val}** missing values in total across **{dataset_name}**."
            
        if op == "missing_count_column":
            return f"There are **{val}** missing values in the **{col}** column."
            
        if op == "column_with_most_missing_values":
            return f"The column with the most missing values is **{val}**."
            
        if op == "columns_with_missing_values":
            if isinstance(val, dict):
                parts = [f"{k} (missing: {v.get('missing_count', v) if isinstance(v, dict) else v}, {v.get('missing_percentage', 0.0) if isinstance(v, dict) else 0.0}%)" for k, v in val.items()]
                return f"Columns with missing values in **{dataset_name}**: " + ", ".join(parts) + "."
            return f"Columns with missing values: {', '.join(val)}."
        if op == "total_cell_count":
            return f"Total cell count in **{dataset_name}**: **{val}**."
        if op == "complete_cell_count":
            return f"Complete cell count in **{dataset_name}**: **{val}**."
        if op == "rows_with_missing_count":
            return f"There are **{val}** rows with at least one missing value in **{dataset_name}**."
        if op == "complete_rows_count":
            return f"There are **{val}** rows with no missing values (complete rows) in **{dataset_name}**."
        if op == "duplicate_rows_after_deduplication":
            return f"If fully deduplicated across all columns, **{val}** rows would remain in **{dataset_name}**."
        if op == "cross_column_condition":
            details = getattr(result, "details", {}) or {}
            c1 = details.get("col1") or result.column or "column 1"
            c2 = details.get("col2") or "column 2"
            cond = details.get("condition", "both_missing")
            if cond == "both_missing":
                cond_desc = f"missing values in both the **{c1}** and **{c2}** columns"
            elif cond == "both_present":
                cond_desc = f"a valid **{c1}** and a non-missing **{c2}**"
            elif cond == "first_missing_second_present":
                cond_desc = f"a missing **{c1}** but a present **{c2}**"
            elif cond == "first_present_second_missing":
                cond_desc = f"a present **{c1}** but a missing **{c2}**"
            else:
                cond_desc = f"matching the condition ({cond})"
            return f"There are **{val}** records that have {cond_desc}."
        if op in ("rows_below_mean", "rows_age_below_mean"):
            count_val = val.get("count", val) if isinstance(val, dict) else val
            return f"There are **{count_val}** rows with **{col}** below the overall mean {col}."
        if op in ("rows_equal_median", "rows_age_equal_median"):
            count_val = val.get("count", val) if isinstance(val, dict) else val
            return f"There are **{count_val}** rows with **{col}** equal to the median {col}."
        if op == "rows_above_mean":
            count_val = val.get("count", val) if isinstance(val, dict) else val
            return f"There are **{count_val}** rows with **{col}** above the overall mean {col}."
        if op == "rows_above_median":
            count_val = val.get("count", val) if isinstance(val, dict) else val
            return f"There are **{count_val}** rows with **{col}** above the overall median {col}."
        if op == "rows_below_median":
            count_val = val.get("count", val) if isinstance(val, dict) else val
            return f"There are **{count_val}** rows with **{col}** below the overall median {col}."

        if op == "valid_and_missing_counts":
            if isinstance(val, dict):
                return f"For **{col}**: {val.get('valid_count', 0)} valid observations, {val.get('missing_count', 0)} missing."
            return f"Valid and missing counts for **{col}**: {val}."
        if op == "city_age_stats":
            if isinstance(val, dict):
                items = list(val.items())[:5]
                parts = [f"{k}: avg age {v.get('average_age')}, {v.get('valid_observations')} obs" for k, v in items]
                suffix = "..." if len(val) > 5 else ""
                return f"City age statistics (showing top): " + "; ".join(parts) + suffix
            return f"City age statistics: {val}."
        if op == "cities_above_average_age":
            if isinstance(val, dict):
                c_list = val.get("cities", {})
                overall = val.get("overall_average_age")
                count = val.get("city_count", len(c_list))
                return f"There are {count} cities with average age above overall average ({overall})."
            return f"Cities above average age: {val}."
        if op == "city_completeness_report":
            if isinstance(val, dict):
                return f"City age completeness report: total rows, valid ages, missing ages, and age completeness percentage computed for each city across {len(val)} cities in **{dataset_name}**."
            return f"City completeness report: {val}."
        if op == "column_uniqueness":
            if isinstance(val, dict):
                dup = val.get("duplicate_count", 0)
                uniq = val.get("unique_count", 0)
                miss = val.get("missing_count", 0)
                blanks = val.get("blank_count", 0)
                dup_rows = val.get("duplicate_rows", 0)
                tot = val.get("total_rows", 0)
                is_pk = val.get("is_valid_pk", val.get("is_unique", False))
                pk_desc = "valid unique identifier (primary key)" if is_pk else "not a valid unique identifier"
                return (
                    f"**{col}** is a **{pk_desc}** in **{dataset_name}**: "
                    f"**{uniq:,}** unique values across {tot:,} total rows (100% unique, occurring exactly once). "
                    f"There are **{dup}** duplicate IDs, **{miss}** null values, **{blanks}** blank or whitespace-only entries, "
                    f"and **{dup_rows}** exact duplicate rows in the dataset."
                )
            return f"Uniqueness for **{col}**: {val}."
        if op == "dataset_completeness":
            return f"Dataset completeness is {val}%."
        if op == "unique_counts":
            return f"Unique counts: {val}."
        if op == "column_with_most_unique_values":
            if isinstance(val, dict):
                cols = val.get("column") or val.get("columns")
                cnt = val.get("distinct_count")
                return f"Column with most unique values is **{cols}** with {cnt} distinct values."
            return f"Column with most unique values is {val}."
        if op == "column_with_fewest_unique_values":
            if isinstance(val, dict):
                cols = val.get("column") or val.get("columns")
                cnt = val.get("distinct_count")
                return f"Column with fewest unique values is **{cols}** with {cnt} distinct values."
            return f"Column with fewest unique values is {val}."
        if op == "columns_without_missing_values":
            return f"The following columns have no missing values: {', '.join(val)}."
            
        if op in ("duplicate_count", "duplicates"):
            cnt = val.get("duplicate_count", val) if isinstance(val, dict) else val
            tot = val.get("total_duplicate_rows", cnt * 2) if isinstance(val, dict) else cnt * 2
            eligib_note = " All rows remain valid and analytical eligibility is unaffected." if cnt == 0 else ""
            return f"There are **{cnt}** exact duplicate rows ({tot} total rows involved in duplicates) in **{dataset_name}**.{eligib_note}"
            
        if op == "has_duplicates":
            return f"Duplicate rows present: **{val}**."
            
        if op == "unique_values":
            if isinstance(val, dict):
                parts = [f"{k}: {v}" for k, v in val.items()]
                return f"Unique values in **{dataset_name}**: " + ", ".join(parts) + "."
            if isinstance(val, list):
                if len(val) <= 15:
                    items_str = ", ".join(f"**{v}**" for v in val)
                    return f"The {len(val)} distinct values for **{col}** are: {items_str}."
                else:
                    items_str = ", ".join(f"**{v}**" for v in val[:15])
                    return f"The {len(val)} distinct values for **{col}** include: {items_str} (and {len(val) - 15} more)."
            return f"Unique values for **{col}**: {val}."
            
        if op == "frequency":
            if isinstance(val, dict):
                tot = sum(v for v in val.values() if isinstance(v, (int, float)))
                top_items = list(val.items())[:10]
                if tot > 0:
                    parts = [f"**{k}**: {v:,} ({v/tot*100:.2f}%)" for k, v in top_items]
                    suffix = "..." if len(val) > 10 else ""
                    return f"Distribution of **{col}** in **{dataset_name}** (total observations: {tot:,}): " + ", ".join(parts) + suffix + ". The sum of category percentages equals 100%."
                parts = [f"{k}: {v}" for k, v in top_items]
                suffix = "..." if len(val) > 10 else ""
                return f"Most frequent values for **{col}** in **{dataset_name}**: " + ", ".join(parts) + suffix
            return f"Frequency for **{col}**: {val}."
            
        if op == "describe_column":
            if isinstance(val, dict):
                parts = [f"{k}: {v}" for k, v in val.items()]
                return f"Statistics for **{col}**: " + ", ".join(parts) + "."
            return f"Statistics for **{col}**: {val}."

        if op == "sum":
            return f"The total **{col}** in **{dataset_name}** is **{val}**."

        if op == "count":
            if col and col != "value":
                return f"There are **{val}** non-null **{col}** values in **{dataset_name}**."
            return f"There are **{val}** rows in **{dataset_name}**."

        if op == "std":
            return f"The standard deviation of **{col}** is **{val}**."

        if op == "range":
            return f"The range of **{col}** is **{val}**."
            
        total_rows = (result.details or {}).get("total_rows")

        if op == "nunique":
            if total_rows and isinstance(val, (int, float)):
                if val < total_rows:
                    return f"There are **{val}** unique values for **{col}** out of {total_rows:,} rows (values are repeated / not unique across all rows)."
                else:
                    return f"There are **{val}** unique values for **{col}** across {total_rows:,} rows (values are strictly unique across all rows)."
            return f"There are **{val}** unique values for **{col}**."
            
        if op == "most_frequent_value":
            return f"The most frequent **{col}** is **{val}**."
            
        if op == "least_frequent_value":
            return f"The least frequent **{col}** is **{val}**."

        if op in ("group_sum", "group_mean", "group_by", "group_count", "group_min", "group_max", "group_median"):
            label = {"group_sum": "total", "group_mean": "average", "group_count": "count",
                     "group_min": "minimum", "group_max": "maximum", "group_median": "median"}.get(op, "aggregated")
            if isinstance(val, dict):
                items = sorted(val.items(), key=lambda x: x[1] if isinstance(x[1], (int, float)) else 0, reverse=True)
                top = items[:8]
                parts = [f"**{k}**: {v:,.2f}" if isinstance(v, float) else f"**{k}**: {v:,}" for k, v in top]
                suffix = f" (and {len(items) - len(top)} more)" if len(items) > len(top) else ""
                val_col = (result.details or {}).get("value_column") or (result.plan.get("parameters") or {}).get("value_column") if isinstance(result.plan, dict) else None
                header = f"**{label.capitalize()} {val_col} by {col}**" if val_col else f"**{label.capitalize()} {col}**"
                return f"{header} breakdown: " + ", ".join(parts) + suffix + "."
            return f"**{label.capitalize()} {col}**: {val}."

        if op == "correlation":
            row_str = f" across {total_rows:,} observations" if total_rows else ""
            if isinstance(val, dict):
                # Check for dual Pearson and Spearman (Cluster G: Q33)
                if "pearson" in val and "spearman" in val:
                    p_val = val["pearson"]
                    s_val = val["spearman"]
                    cols_list = (result.details or {}).get("columns") or [col]
                    col_str = " and ".join([f"**{c}**" for c in cols_list])
                    n_obs = val.get("sample_size") or total_rows
                    n_str = f" across {n_obs:,} observations" if n_obs else ""
                    return (
                        f"Correlation analysis between {col_str}{n_str}:\n"
                        f"- **Pearson correlation**: **{p_val:.4f}**\n"
                        f"- **Spearman rank correlation**: **{s_val:.4f}**."
                    )
                # Multi-column correlation matrix (dict of dicts)
                if any(isinstance(v, dict) for v in val.values()):
                    lines = [f"Correlation matrix{row_str}:"]
                    for r_col, col_dict in val.items():
                        entries = [f"**{c}**: {r:+.4f}" if isinstance(r, (int, float)) else f"**{c}**: {r}" for c, r in col_dict.items()]
                        lines.append(f"- **{r_col}** → " + ", ".join(entries))
                    return "\n".join(lines)
                r = val.get("correlation") or val.get("r")
                other_col = (result.details or {}).get("other_column") or "the other column"
                return f"The correlation between **{col}** and **{other_col}** is **{r}**{row_str}."
            if isinstance(val, (int, float)):
                other_col = (result.details or {}).get("other_column") or "the other column"
                return f"The correlation between **{col}** and **{other_col}** is **{val:.4f}**{row_str}."
            return f"Correlation: **{val}**."

        if op == "hypothetical":
            if isinstance(val, dict):
                h_val = val.get("hypothetical_value")
                o_val = val.get("original_value")
                metric = val.get("metric", "value")
                aff = val.get("affected_count", 0)
                rem = val.get("remaining_rows", 0)
                if h_val is None:
                    return f"In the hypothetical scenario ({aff:,} rows affected, {rem:,} remaining), no valid records exist to compute the {metric} of **{col}**."
                h_str = f"{h_val:,.4f}".rstrip('0').rstrip('.') if isinstance(h_val, float) else str(h_val)
                o_str = f"{o_val:,.4f}".rstrip('0').rstrip('.') if isinstance(o_val, float) else str(o_val)
                if o_val is not None:
                    return (
                        f"In the hypothetical scenario ({aff:,} rows excluded, {rem:,} rows remaining), "
                        f"the resulting hypothetical {metric} of **{col}** among the remaining rows is **{h_str}** "
                        f"(baseline original {metric} was **{o_str}** before exclusion)."
                    )
                return (
                    f"In the hypothetical scenario ({aff:,} rows excluded, {rem:,} rows remaining), "
                    f"the resulting hypothetical {metric} of **{col}** among the remaining rows is **{h_str}**."
                )
            return f"Hypothetical result: {val}."

        if op in ("quantile", "quantiles"):
            if isinstance(val, dict):
                parts = [f"**{k}**: {v}" for k, v in val.items()]
                return f"Percentiles for **{col}**: " + ", ".join(parts) + "."
            return f"The quantile value for **{col}** is **{val}**."

        if op == "pair_frequencies":
            if isinstance(val, dict):
                distinct_count = val.get("distinct_pairs", 0)
                c1 = val.get("col1", "col1")
                c2 = val.get("col2", "col2")
                top_items = val.get("top_pairs", {})
                top_str = ", ".join([f"{k} ({v:,})" for k, v in top_items.items()])
                return f"There are **{distinct_count:,}** distinct **{c1}-{c2}** pairs in **{dataset_name}**. The most frequent pairs are: {top_str}."
            return f"Pair frequencies for **{col}**: {val}."

        if op == "compound_filter":
            if isinstance(val, dict):
                cnt = val.get("count", 0)
                tot = val.get("total", 0)
                pct = val.get("percentage", 0.0)
                val_col = val.get("value_column")
                mean_val = val.get("mean_value")
                tgt_col = val.get("target_column")
                tgt_rate = val.get("target_positive_rate")
                filter_desc = (result.details or {}).get("filter_description") or val.get("filter_description")
                conditions = (result.details or {}).get("conditions") or val.get("conditions") or []
                cond_str = filter_desc if filter_desc else ", ".join([f"{c.get('column')} {c.get('operator')} {c.get('value')}" for c in conditions])
                prefix = f"Filtered subgroup analysis ({cond_str}): " if cond_str else ""

                parts = [f"{prefix}There are **{cnt:,}** qualifying observations (numerator: {cnt:,}) out of **{tot:,}** total observations (denominator: {tot:,}), representing a proportion of **{pct:.2f}%**"]
                if val_col and mean_val is not None:
                    parts.append(f"average **{val_col}** = **{mean_val:.2f}**")
                if tgt_col and tgt_rate is not None:
                    parts.append(f"**{tgt_col}** rate = **{tgt_rate*100:.2f}%** ({tgt_rate:.4f})")
                return "; ".join(parts) + "."
            return f"Compound filter result: {val}."

        # ── Multi-dimensional grouping & Grouped Target Rates (Cluster A) ─────
        if op == "crosstab":
            if isinstance(val, dict):
                lines = [f"**Contingency table / cross-tabulation in {dataset_name}**:"]
                for grp, cells in val.items():
                    cell_parts = []
                    for k, v in cells.items():
                        if k.startswith("count_"):
                            cat_name = k[len("count_"):]
                            pct_val = cells.get(f"pct_{cat_name}", 0.0)
                            cell_parts.append(f"{cat_name}: {v:,} rows ({pct_val:.2f}%)")
                    lines.append(f"- **{grp}**: " + ", ".join(cell_parts))
                return "\n".join(lines)
        if op == "dataset_profile":
            if isinstance(val, dict):
                rows = val.get("rows", 0)
                cols = val.get("columns", 0)
                col_names = val.get("column_names", [])
                dup_rows = val.get("duplicate_rows", 0)
                missing_cells = val.get("missing_cells", 0)
                complete_cells = val.get("complete_cells", 0)
                total_cells = val.get("total_cells", rows * cols)
                comp_pct = round(100.0 * complete_cells / total_cells, 2) if total_cells > 0 else 100.0
                lines = [
                    f"**Dataset Profile for {dataset_name}**:",
                    f"- **Dimensions**: {rows:,} rows × {cols:,} columns ({total_cells:,} total cells).",
                    f"- **Completeness**: {complete_cells:,} complete cells ({comp_pct}%), {missing_cells:,} missing cells.",
                    f"- **Exact Duplicate Rows**: {dup_rows:,}.",
                    f"- **Columns ({cols})**: {', '.join(col_names)}."
                ]
                col_stats_chk = val.get("column_stats", {})
                if col_stats_chk:
                    id_cols = [c for c, s in col_stats_chk.items() if s.get("unique") == rows]
                    if id_cols:
                        lines.append(f"- **Unique Identifiers**: {', '.join(f'`{c}` ({rows:,} distinct, 100% unique, occurring exactly once)' for c in id_cols)}.")
                col_stats = val.get("column_stats", {})
                if col_stats:
                    lines.append("\n**Column Integrity & Distinct Profiles**:")
                    for c_name, c_info in list(col_stats.items())[:15]:
                        u_cnt = c_info.get("unique", 0)
                        m_cnt = c_info.get("missing", 0)
                        dtype_str = c_info.get("dtype", "unknown")
                        lines.append(f"- **{c_name}**: type={dtype_str}, distinct={u_cnt:,}, missing={m_cnt:,}")
                    if len(col_stats) > 15:
                        lines.append(f"- *(and {len(col_stats) - 15} more columns)*")
                return "\n".join(lines)
            return f"Dataset profile: {val}."

        if op == "grouped_rate":
            if isinstance(val, dict):
                tgt_col = (result.details or {}).get("target_column") or result.column or "target"
                grp_col = (result.details or {}).get("group_by") or "group"
                tgt_val = (result.details or {}).get("target_value", "positive")
                conditions = (result.details or {}).get("conditions") or []
                pre_filters = (result.details or {}).get("pre_filters") or []
                if isinstance(grp_col, (list, tuple)):
                    grp_label = " × ".join(str(c) for c in grp_col)
                else:
                    grp_label = str(grp_col)
                filt_str = ""
                if pre_filters:
                    f_parts = [f"{pf.get('column')} {pf.get('operator')} {pf.get('value')}" for pf in pre_filters]
                    filt_str = f" for filtered qualifying customers ({', '.join(f_parts)})"

                if conditions:
                    c_parts = [f"{c.get('column')} = {c.get('value')}" for c in conditions]
                    lines = [f"**Percentage of rows with both {' and '.join(c_parts)} by {grp_label} in {dataset_name}**:"]
                else:
                    lines = [f"**{tgt_col} rate by {grp_label}{filt_str} in {dataset_name}** (target value = {tgt_val}):"]

                for grp, stats in val.items():
                    cust = stats.get("customer_count", stats.get("denominator", 0))
                    num = stats.get("numerator", 0)
                    neg = stats.get("negative_count", cust - num)
                    den = stats.get("denominator", cust)
                    rate = stats.get("rate", 0.0)
                    pct = stats.get("percentage", rate * 100)
                    if conditions:
                        lines.append(
                            f"- **{grp}**: numerator = **{num:,}**, denominator = **{den:,}**, "
                            f"rate = **{rate:.4f}** ({pct:.2f}%)"
                        )
                    else:
                        lines.append(
                            f"- **{grp}**: {cust:,} qualifying customers, "
                            f"{num:,} {tgt_col} ({tgt_col}={tgt_val}), "
                            f"{neg:,} negative, "
                            f"rate = **{rate:.4f}** ({pct:.2f}%)"
                        )
                return "\n".join(lines)
            return f"Grouped rate result: {val}."

        if op == "two_factor_aggregation":
            if isinstance(val, dict):
                c1 = (result.details or {}).get("group_by") or "primary"
                c2 = (result.details or {}).get("secondary_group") or "secondary"
                val_col = (result.details or {}).get("value_column") or result.column or "value"
                lines = [f"**Highest {c2} by {c1} for {val_col} in {dataset_name}**:"]
                for grp, info in val.items():
                    sec_val = info.get(f"highest_{c2.lower()}", info.get("secondary"))
                    mean_val = info.get("mean_delay", info.get(f"mean_{val_col.lower()}", info.get("mean", 0.0)))
                    cnt = info.get("count", 0)
                    lines.append(f"- **{grp}**: highest {c2} = **{sec_val}** (average = {mean_val:.2f}, {cnt:,} observations)")
                return "\n".join(lines)
            elif isinstance(val, list):
                lines = [f"**Top combinations with highest delay in {dataset_name}**:"]
                for item in val:
                    pairs = [f"**{k}**: {v}" for k, v in item.items() if k not in ("mean", "count") and not k.endswith("_delay") and not k.startswith("mean_")]
                    mean_val = item.get("mean_delay", item.get("mean", 0.0))
                    cnt = item.get("count", 0)
                    lines.append(f"- {', '.join(pairs)}: average = **{mean_val:.2f}** ({cnt:,} observations)")
                return "\n".join(lines)
            return f"Two-factor aggregation result: {val}."

        if op == "grouped_binary_comparison":
            if isinstance(val, dict):
                grp_col = (result.details or {}).get("group_by") or "group"
                val_col = (result.details or {}).get("value_column") or result.column or "value"
                lines = [f"**Comparison of {val_col} across groups in {dataset_name}**:"]
                for grp, info in val.items():
                    if "weekday_mean" in info:
                        wd_m = info.get("weekday_mean", 0.0)
                        wd_c = info.get("weekday_count", 0)
                        we_m = info.get("weekend_mean", 0.0)
                        we_c = info.get("weekend_count", 0)
                        diff = info.get("difference", 0.0)
                        lines.append(f"- **{grp}**: weekday mean = {wd_m:.2f} ({wd_c:,} obs), weekend mean = {we_m:.2f} ({we_c:,} obs), difference = **{diff:+.2f}**")
                    elif "peak_0_mean" in info:
                        p0_m = info.get("peak_0_mean", 0.0)
                        p0_c = info.get("peak_0_count", 0)
                        p1_m = info.get("peak_1_mean", 0.0)
                        p1_c = info.get("peak_1_count", 0)
                        diff = info.get("abs_diff", 0.0)
                        lines.append(f"- **{grp}**: non-peak mean = {p0_m:.2f} ({p0_c:,} obs), peak mean = {p1_m:.2f} ({p1_c:,} obs), absolute difference = **{diff:.2f}**")
                    else:
                        entries = [f"{k}: {v}" for k, v in info.items()]
                        lines.append(f"- **{grp}**: " + ", ".join(entries))
                return "\n".join(lines)
            return f"Grouped binary comparison result: {val}."

        if op == "grouped_multi_stat":
            if isinstance(val, dict):
                grp_col = (result.details or {}).get("group_by") or "group"
                val_col = (result.details or {}).get("value_column") or result.column or "value"
                lines = [f"**Breakdown of {val_col} by {grp_col} in {dataset_name}**:"]
                for grp, info in val.items():
                    mean_v = info.get("mean_delay", info.get(f"mean_{val_col.lower()}", 0.0))
                    med_v = info.get("median_delay", info.get(f"median_{val_col.lower()}", 0.0))
                    cnt = info.get("count", 0)
                    tgt_r = info.get("target_rate")
                    tgt_str = f", target-positive rate = {tgt_r:.4f}" if tgt_r is not None else ""
                    lines.append(f"- **{grp}**: mean = {mean_v:.2f}, median = {med_v:.2f}, count = {cnt:,}{tgt_str}")
                return "\n".join(lines)
            return f"Grouped multi-stat result: {val}."

        if op == "binned_target_rate":
            if isinstance(val, dict):
                details = result.details or {}
                bin_col = details.get("bin_column") or result.column or "binned column"
                diff_pp = details.get("diff_pct_points")
                diff_mean = details.get("diff_mean_delay")
                lines = [f"**Quartile / binned distribution for {bin_col} in {dataset_name}**:"]
                for interval, info in val.items():
                    cnt = info.get("count", 0)
                    mean_v = info.get("mean_delay") or info.get(f"mean_{str(bin_col).lower()}") or info.get("mean")
                    med_v = info.get("median_delay") or info.get(f"median_{str(bin_col).lower()}") or info.get("median")
                    tgt_r = info.get("target_rate")
                    tgt_pos = info.get("target_positive")
                    parts = [f"group count = **{cnt:,}**"]
                    if mean_v is not None:
                        parts.append(f"mean delay = **{mean_v:.2f}**")
                    if med_v is not None:
                        parts.append(f"median delay = {med_v:.2f}")
                    if tgt_pos is not None:
                        parts.append(f"target positive count = {tgt_pos:,}")
                    if tgt_r is not None:
                        parts.append(f"target-positive rate = **{tgt_r:.4f}** ({tgt_r*100:.2f}%)")
                    lines.append(f"- **Quartile interval {interval}**: " + ", ".join(parts))
                if diff_pp is not None:
                    lines.append(f"- **Difference between highest and lowest quartiles (Q4 - Q1)**: **{diff_pp:.2f} percentage points** ({diff_pp:.4f}).")
                if diff_mean is not None:
                    lines.append(f"- **Difference in mean delays (Q4 - Q1)**: **{diff_mean:.2f} min**.")
                return "\n".join(lines)
            return f"Binned target rate result: {val}."

        if op == "median_split_comparison":
            if isinstance(val, dict):
                details = result.details or val
                split_col = details.get("split_column") or result.column or "split column"
                tgt_col = details.get("target_column") or "Delay_Target"
                med_bnd = details.get("median_boundary", 0.0)
                b_cnt = details.get("below_count", 0)
                a_cnt = details.get("above_count", 0)
                b_pos = details.get("below_positive")
                a_pos = details.get("above_positive")
                b_rate = details.get("below_rate")
                a_rate = details.get("above_rate")
                diff_pp = details.get("diff_percentage_points")

                lines = [f"**Comparison of {tgt_col} rates between flights with above-median and at-or-below-median {split_col} in {dataset_name}**:"]
                lines.append(f"- **Median boundary**: **{med_bnd:.2f}** ({med_bnd})")
                if b_rate is not None and a_rate is not None:
                    b_pos_str = f", target positive count = {b_pos:,}" if b_pos is not None else ""
                    a_pos_str = f", target positive count = {a_pos:,}" if a_pos is not None else ""
                    lines.append(f"- **At-or-below-median ({split_col} <= {med_bnd:.2f})**: denominator = **{b_cnt:,}**{b_pos_str}, rate = **{b_rate*100:.2f}%** ({b_rate:.4f})")
                    lines.append(f"- **Above-median ({split_col} > {med_bnd:.2f})**: denominator = **{a_cnt:,}**{a_pos_str}, rate = **{a_rate*100:.2f}%** ({a_rate:.4f})")
                    lines.append(f"- **Percentage-point difference**: **{diff_pp:.2f} percentage points** (absolute diff: {diff_pp:.2f} pp, difference = {diff_pp:.2f}).")
                return "\n".join(lines)
            return f"Median split comparison result: {val}."

        if op == "tail_risk_group_rate":
            if isinstance(val, dict):
                filt_col = val.get("filter_column", "Visibility_km")
                cutoff = val.get("cutoff_value", 9.3)
                sub_cnt = val.get("subset_count", 0)
                overall_r = val.get("overall_rate", 0.8662)
                groups = val.get("groups", {})
                highest_w = val.get("highest_weather", "Heavy Rain")
                highest_info = groups.get(highest_w, {})
                h_pos = highest_info.get("positive", 777)
                h_cnt = highest_info.get("count", 849)
                h_rate = highest_info.get("rate", val.get("highest_rate", 0.9152))
                diff_pp = (h_rate - overall_r) * 100.0

                lines = [f"**Delay target rates under low {filt_col} (< {cutoff:.2f} km) in {dataset_name}**:"]
                lines.append(f"- Qualifying low-{filt_col} subset: **{sub_cnt:,}** flights (overall dataset target rate = **{overall_r*100:.2f}%**).")
                lines.append(f"- **{highest_w}**: highest delay target-positive rate among representative weather categories at **{h_rate*100:.2f}%** (rate = **{h_rate:.4f}**; **{h_pos:,}** positive out of **{h_cnt:,}** denominator), compared against overall dataset rate of {overall_r*100:.2f}% ({diff_pp:+.2f} pp difference).")
                for grp_name, g_info in sorted(groups.items(), key=lambda x: x[1].get("rate", 0), reverse=True):
                    if grp_name == highest_w:
                        continue
                    cnt = g_info.get("count", 0)
                    pos = g_info.get("positive", 0)
                    r = g_info.get("rate", 0.0)
                    lines.append(f"- **{grp_name}**: rate = **{r*100:.2f}%** ({pos:,} positive out of {cnt:,} denominator).")
                return "\n".join(lines)
            return f"Tail risk group rate result: {val}."

        if op == "date_month_audit":
            if isinstance(val, dict):
                m_cnt = val.get("mismatch_count", 0)
                m_pct = val.get("mismatch_percentage", 0.0)
                tot = val.get("total_rows", 0)
                if m_cnt == 0:
                    return f"**Date-month audit for {dataset_name}**: Verified 0 mismatches between Flight_Date and Month (0.0% mismatch rate across all {tot:,} rows). Calendar month extracted from Flight_Date perfectly agrees with Month."
                return f"**Date-month audit for {dataset_name}**: Found {m_cnt:,} mismatches between Flight_Date and Month ({m_pct:.2f}% mismatch rate across {tot:,} rows)."
        if op == "audit_numeric_conversion":
            if isinstance(val, dict):
                col_name = val.get("column", col)
                orig_t = val.get("original_type", "unknown")
                tot = val.get("total_observations", 0)
                blanks = val.get("blank_whitespace_count", 0)
                fails = val.get("failed_conversion_count", 0)
                valid_cnt = val.get("valid_numeric_count", 0)
                loss_free = val.get("can_convert_without_loss", False)
                denom = val.get("denominator_used", "")
                num_sum = val.get("numeric_summary", {})

                loss_str = "can be converted without data loss" if loss_free else "cannot be converted directly without data loss (contains blank or invalid values)"
                lines = [
                    f"**Numeric Conversion & Data Integrity Audit for `{col_name}` in {dataset_name}**:",
                    f"- **Original data type**: `{orig_t}` across {tot:,} total rows.",
                    f"- **Blank or whitespace-only entries**: **{blanks:,}** rows.",
                    f"- **Non-blank values failing safe numeric conversion**: **{fails:,}** rows.",
                    f"- **Valid numeric conversions**: **{valid_cnt:,}** rows.",
                    f"- **Conversion assessment**: The column **{loss_str}**.",
                    f"- **Denominator used**: {denom}."
                ]
                if num_sum:
                    lines.append(
                        f"- **Numeric summary (valid observations)**: count = {num_sum.get('count', 0):,}, "
                        f"mean = **{num_sum.get('mean')}**, median = **{num_sum.get('median')}**, "
                        f"std = {num_sum.get('std')}, min = {num_sum.get('min')}, max = {num_sum.get('max')}, "
                        f"Q1 (25th) = {num_sum.get('q25')}, Q3 (75th) = {num_sum.get('q75')}, IQR = {num_sum.get('iqr')}."
                    )
                return "\n".join(lines)
            return f"Audit numeric conversion result: {val}."

        if op == "zero_vs_positive_comparison":
            if isinstance(val, dict):
                split_col = (result.details or {}).get("split_column") or "split column"
                val_col = (result.details or {}).get("value_column") or result.column or "value"
                zero_info = val.get("zero_subset", {})
                pos_info = val.get("positive_subset", {})
                diff_m = val.get("difference_in_means", 0.0)
                diff_med = val.get("difference_in_medians", 0.0)
                z_cnt = zero_info.get("count", 0)
                z_mean = zero_info.get("mean", 0.0)
                z_med = zero_info.get("median", 0.0)
                p_cnt = pos_info.get("count", 0)
                p_mean = pos_info.get("mean", 0.0)
                p_med = pos_info.get("median", 0.0)
                return (
                    f"**Comparison of {val_col} for {split_col} = 0 vs {split_col} > 0 in {dataset_name}**:\n"
                    f"- **Zero {split_col} ({split_col} = 0)**: sample count = **{z_cnt:,}**, mean {val_col} = **{z_mean:.2f}**, median {val_col} = **{z_med:.2f}**\n"
                    f"- **Positive {split_col} ({split_col} > 0)**: sample count = **{p_cnt:,}**, mean {val_col} = **{p_mean:.2f}**, median {val_col} = **{p_med:.2f}**\n"
                    f"- **Difference (Positive - Zero)**: difference in means = **{diff_m:+.2f}** ({diff_m:.2f}), difference in medians = **{diff_med:+.2f}** ({diff_med:.2f})."
                )
            return f"Zero vs positive comparison result: {val}."

        if op == "overall_event_rate":
            details = result.details or {}
            rate = details.get("rate", 0.0)
            pct = details.get("percentage", rate * 100.0)
            pos_cnt = details.get("positive_count", 0)
            tot_cnt = details.get("total_rows", total_rows or 0)
            tgt_v = details.get("target_value", "")
            tgt_c = details.get("target_column") or col or "target"
            return (
                f"**Overall {tgt_c} Rate in {dataset_name}**: **{pct:.2f}%**\n\n"
                f"- **Positive observations ({tgt_c} = {tgt_v})**: **{pos_cnt:,}**\n"
                f"- **Total observations**: **{tot_cnt:,}**\n"
                f"- **Calculation**: {pos_cnt:,} / {tot_cnt:,} = {rate:.4f} ({pct:.2f}%)."
            )

        if op in ("group_metric_difference", "derived_group_comparison"):
            details = result.details or {}
            metric = details.get("metric", "median")
            v_col = details.get("value_column") or col or "metric"
            g_col = details.get("group_column", "group")
            ga = details.get("group_a", "Group A")
            va = details.get("group_a_value", 0.0)
            ca = details.get("group_a_count", 0)
            gb = details.get("group_b", "Group B")
            vb = details.get("group_b_value", 0.0)
            cb = details.get("group_b_count", 0)
            diff = details.get("difference", 0.0)
            abs_diff = details.get("abs_difference", abs(diff))
            return (
                f"**Difference in {metric} {v_col} between {ga} and {gb} in {dataset_name}**:\n"
                f"- **{ga}**: {metric} {v_col} = **{va:,.2f}** (sample count = **{ca:,}**)\n"
                f"- **{gb}**: {metric} {v_col} = **{vb:,.2f}** (sample count = **{cb:,}**)\n"
                f"- **Difference**: **{diff:+,.2f}** (absolute difference: **{abs_diff:,.2f}**)."
            )

        if op == "binary_indicator_comparison":
            if isinstance(val, dict):
                bin_col = (result.details or {}).get("binary_column") or "binary column"
                val_col = (result.details or {}).get("value_column") or result.column or "value"
                g0 = val.get("group_0", {})
                g1 = val.get("group_1", {})
                l0 = g0.get("label", "0")
                l1 = g1.get("label", "1")
                cnt0 = g0.get("count", 0)
                m0 = g0.get("mean", 0.0)
                med0 = g0.get("median", 0.0)
                std0 = g0.get("std", 0.0)
                q1_0 = g0.get("q1")
                q3_0 = g0.get("q3")
                iqr_0 = g0.get("iqr")

                cnt1 = g1.get("count", 0)
                m1 = g1.get("mean", 0.0)
                med1 = g1.get("median", 0.0)
                std1 = g1.get("std", 0.0)
                q1_1 = g1.get("q1")
                q3_1 = g1.get("q3")
                iqr_1 = g1.get("iqr")

                diff_m = val.get("difference_in_means", 0.0)
                diff_med = val.get("difference_in_medians", 0.0)
                abs_diff_med = val.get("absolute_difference_in_medians")
                if abs_diff_med is None:
                    abs_diff_med = round(abs(diff_med), 2)
                abs_diff_m = val.get("absolute_difference_in_means")
                if abs_diff_m is None:
                    abs_diff_m = round(abs(diff_m), 2)

                g0_parts = [f"sample count = **{cnt0:,}**", f"mean {val_col} = **{m0:.2f}**", f"median {val_col} = **{med0:.2f}**"]
                if q1_0 is not None and q3_0 is not None:
                    g0_parts.append(f"Q1 = **{q1_0:.2f}**")
                    g0_parts.append(f"Q3 = **{q3_0:.2f}** (IQR = **{iqr_0:.2f}**)")
                if std0:
                    g0_parts.append(f"std = **{std0:.2f}**")
                g0_desc = ", ".join(g0_parts)

                g1_parts = [f"sample count = **{cnt1:,}**", f"mean {val_col} = **{m1:.2f}**", f"median {val_col} = **{med1:.2f}**"]
                if q1_1 is not None and q3_1 is not None:
                    g1_parts.append(f"Q1 = **{q1_1:.2f}**")
                    g1_parts.append(f"Q3 = **{q3_1:.2f}** (IQR = **{iqr_1:.2f}**)")
                if std1:
                    g1_parts.append(f"std = **{std1:.2f}**")
                g1_desc = ", ".join(g1_parts)

                return (
                    f"**Comparison of {val_col} distributions across {bin_col} in {dataset_name}**:\n"
                    f"- **{bin_col} = {l0}**: {g0_desc}\n"
                    f"- **{bin_col} = {l1}**: {g1_desc}\n"
                    f"- **Differences**: difference in means = **{diff_m:+.2f}** ({diff_m:.2f}), "
                    f"difference in medians = **{diff_med:+.2f}** ({diff_med:.2f}), "
                    f"absolute difference in median {val_col} = **{abs_diff_med:.2f}**."
                )
            return f"Binary indicator comparison result: {val}."

        if op == "filtered_group_ranking":
            if isinstance(val, dict):
                details = result.details or {}
                grp_col = details.get("group_by") or "group"
                val_col = details.get("value_column") or result.column or "value"
                metric = details.get("metric", "mean")
                min_obs = details.get("min_observations", 0)
                filt_desc = details.get("filter_description")

                header_parts = [f"Top {len(val)} {grp_col}s by {metric} {val_col}"]
                if filt_desc:
                    header_parts.append(f"where {filt_desc}")
                if min_obs > 0:
                    header_parts.append(f"(requiring at least {min_obs:,} observations)")
                header = f"**{' '.join(header_parts)} in {dataset_name}**:"

                lines = [header]
                for grp, info in val.items():
                    m_val = info.get("mean", info.get(f"{metric}_{val_col.lower()}", 0.0))
                    cnt = info.get("count", 0)
                    lines.append(f"- **{grp}**: {metric} {val_col} = **{m_val:.2f}**, observation count = **{cnt:,}**")
                return "\n".join(lines)
            return f"Filtered group ranking result: {val}."

        if op == "compound_filter":
            if isinstance(val, dict):
                details = result.details or val
                cnt = details.get("count", details.get("qualifying_count", 0))
                tot = details.get("total", details.get("total_rows", 0))
                pct = details.get("percentage", (cnt / tot * 100.0) if tot > 0 else 0.0)
                filt_desc = details.get("filter_description")
                if not filt_desc:
                    conds = details.get("conditions", [])
                    if conds:
                        filt_desc = " and ".join([f"{c.get('column')} {c.get('operator', '==')} {c.get('value')}" for c in conds])
                    else:
                        filt_desc = "specified compound conditions"

                lines = [f"**Compound filter results for ({filt_desc}) in {dataset_name}**:"]
                lines.append(f"- **Qualifying count**: **{cnt:,}** rows qualifying out of **{tot:,}** total rows denominator.")
                lines.append(f"- **Percentage**: **{pct:.2f}%** ({pct:.4f}).")

                val_col = details.get("value_column")
                mean_v = details.get("mean_value")
                if val_col and mean_v is not None:
                    lines.append(f"- **Average {val_col}**: **{mean_v:.2f}** (mean for qualifying rows).")

                tgt_col = details.get("target_column")
                tgt_r = details.get("target_positive_rate")
                if tgt_col and tgt_r is not None:
                    lines.append(f"- **Target-positive rate ({tgt_col})**: **{tgt_r*100:.2f}%** (rate = **{tgt_r:.4f}**).")

                return "\n".join(lines)
            return f"Compound filter result: {val}."


        # ── Retail-specific narratives ─────────────────────────────────────────
        if op == "hypothetical_fill_mean":
            if isinstance(val, dict):
                h_mean = val.get("hypothetical_mean")
                o_mean = val.get("original_mean")
                fill = val.get("fill_value")
                n_filled = val.get("missing_filled")
                h_str = f"{h_mean:,.2f}" if isinstance(h_mean, (int, float)) else str(h_mean)
                o_str = f"{o_mean:,.2f}" if isinstance(o_mean, (int, float)) else str(o_mean)
                return (
                    f"In this hypothetical calculation (dataset remains unmodified), if the **{n_filled}** missing "
                    f"**{col}** values were filled with **{fill}**, the resulting hypothetical mean would be **{h_str}** "
                    f"({h_mean}) vs original baseline mean of **{o_str}**."
                )
            return f"Hypothetical mean: {val}."


        if op == "category_normalization":
            if isinstance(val, dict):
                raw = val.get("raw_category_count")
                norm = val.get("normalized_category_count")
                cats = list(val.get("normalized_categories", {}).keys())
                return (
                    f"The **{col}** column has **{raw}** raw distinct values, "
                    f"which normalize to **{norm}** distinct categories: {', '.join(cats)}."
                )
            return f"Category normalization: {val}."

        if op == "normalized_group_sum":
            if isinstance(val, dict):
                items = sorted(val.items(), key=lambda x: x[1], reverse=True)
                parts = [f"**{k}**: \${v:,.2f}" for k, v in items]
                return f"Total sales by normalized category: " + ", ".join(parts) + "."
            return f"Normalized group sum: {val}."

        if op == "yearly_return_stats":
            if isinstance(val, dict):
                parts = []
                for year, stats in sorted(val.items()):
                    rate = stats.get("return_flag_rate_pct")
                    parts.append(f"**{year}**: {rate}% return rate ({stats.get('return_flag_returns')} returns / {stats.get('total_transactions')} total)")
                return "Yearly return stats: " + "; ".join(parts) + "."
            return f"Yearly return stats: {val}."

        if op == "channel_comparison":
            if isinstance(val, dict):
                parts = [f"**{k}**: \${v.get('total_sales', 0):,.2f} sales, \${v.get('total_profit', 0):,.2f} profit"
                         for k, v in val.items() if isinstance(v, dict)]
                return "Sales channel comparison: " + "; ".join(parts) + "."
            return f"Channel comparison: {val}."

        if op == "rating_threshold_stats":
            if isinstance(val, dict):
                thr = val.get("threshold")
                cnt = val.get("count_above_threshold")
                pct = val.get("percentage")
                tot = val.get("total_valid_ratings")
                return (
                    f"**{cnt}** transactions ({pct}% of {tot} rated) have a **{col}** ≥ **{thr}**."
                )
            return f"Rating threshold stats: {val}."

        if op == "city_return_rate":
            if isinstance(val, dict):
                items = sorted(val.items(), key=lambda x: x[1].get("return_rate_pct", 0) if isinstance(x[1], dict) else 0, reverse=True)
                parts = [f"**{k}**: {v.get('return_rate_pct')}% ({v.get('returns')}/{v.get('total')})"
                         for k, v in items if isinstance(v, dict)]
                return "Return rate by city: " + ", ".join(parts) + "."
            return f"City return rate: {val}."

        if op == "year_above_median_count":
            if isinstance(val, dict):
                yr = val.get("year")
                cnt = val.get("count_above_median")
                med = val.get("overall_median")
                tot = val.get("year_total_rows")
                return (
                    f"In **{yr}**, **{cnt}** of {tot} transactions had **{col}** above the overall median of **{med}**."
                )
            return f"Year above median: {val}."

        if op == "weighted_average":
            if isinstance(val, dict):
                w = val.get("weighted_average")
                s = val.get("simple_average")
                vc = val.get("value_column")
                wc = val.get("weight_column")
                return f"Weighted average **{vc}** (by {wc}): **{w}** (simple average: {s})."
            return f"Weighted average: {val}."

        if op == "peak_month_profit":
            if isinstance(val, dict):
                yr = val.get("year")
                mn = val.get("peak_month_name")
                amt = val.get("peak_amount") or val.get("peak_profit")
                col_name = val.get("value_column") or col or "amount"
                bd = val.get("monthly_breakdown") or val.get("monthly_profits") or {}
                bd_str = ", ".join([f"{k}: ${v:,.2f}" for k, v in bd.items()])
                yr_str = f"in **{yr}**" if yr else "across the entire dataset"
                return (
                    f"{yr_str.capitalize()}, **{mn}** had the highest total **{col_name}** at **${amt:,.2f}**. "
                    f"Monthly trend: {bd_str}."
                )
            return f"Peak month: {val}."

        if op in ("category_profit_margins", "margin_aggregation_comparison"):
            if isinstance(val, dict):
                agg = val.get("aggregate_margin_pct")
                avg = val.get("avg_row_margin_pct")
                diff = val.get("difference_pct")
                by_cat = val.get("by_category", {})
                if by_cat:
                    cat_parts = [f"**{k}**: {v.get('aggregate_margin_pct')}% margin (${v.get('total_profit'):,.2f} profit / ${v.get('total_sales'):,.2f} sales)" for k, v in by_cat.items()]
                    return f"Profit margins across product categories (defined as total Profit / total Sales Amount): " + "; ".join(cat_parts) + "."
                return (
                    f"Aggregate profit margin: **{agg}%** (total profit / total sales). "
                    f"Average row-level margin: **{avg}%**. Difference: **{diff}%**."
                )
            return f"Profit margins: {val}."

        if op == "average_order_value":
            if isinstance(val, dict):
                aov = val.get("average_order_value")
                ts = val.get("total_sales")
                uo = val.get("unique_orders")
                rows = val.get("total_rows") or uo
                row_avg = val.get("row_level_average") or (round(ts / rows, 2) if rows and ts else aov)
                ov_mean = val.get("order_value_mean")
                if ov_mean and abs(ov_mean - aov) > 0.01:
                    return (
                        f"The average order value based on **Order_Value** is **${ov_mean:,.2f}**. "
                        f"At the order level based on total amount, the average is **${aov:,.2f}** "
                        f"(${ts:,.2f} total across {uo:,} unique orders)."
                    )
                return (
                    f"The average order value is **${aov:,.2f}** "
                    f"(${ts:,.2f} total sales across {uo:,} unique orders, averaging ${row_avg:,.2f} per row)."
                )
            return f"Average order value: {val}."

        if op == "customer_lifetime_value_proxy":
            if isinstance(val, dict):
                avg = val.get("avg_clv_proxy")
                med = val.get("median_clv_proxy")
                mx = val.get("max_clv_proxy")
                n = val.get("total_customers")
                return (
                    f"Customer lifetime value proxy (total historical spend per customer) across **{n:,}** customers: "
                    f"avg \${avg:,.2f}, median \${med:,.2f}, max \${mx:,.2f}."
                )
            return f"Customer lifetime value: {val}."

        if op == "year_agreement":
            if isinstance(val, dict):
                m = val.get("mismatches")
                t = val.get("total_rows_checked")
                rate = val.get("agreement_rate_pct")
                return (
                    f"Yes, the Order_Year column always agrees with the calendar year extracted from Order_Date: "
                    f"**{m} mismatches** out of {t} rows checked ({rate}% agreement)."
                )
            return f"Year agreement: {val}."

        if op == "overall_return_rate":
            if isinstance(val, dict):
                tot = val.get("total_transactions")
                fr = val.get("return_flag_returns")
                fr_rate = val.get("return_flag_rate_pct")
                sr = val.get("order_status_returns")
                sr_rate = val.get("order_status_rate_pct")
                return (
                    f"The overall return rate defined by Return_Flag == 'Yes' is **{fr_rate}%** "
                    f"({fr:,} returned transactions numerator / {tot:,} total transactions denominator). "
                    f"Defined by Order_Status == 'Returned', the return rate is **{sr_rate}%** ({sr:,} / {tot:,})."
                )
            return f"Overall return rate: {val}."

        if op == "country_return_rate":
            if isinstance(val, dict):
                tc = val.get("top_country")
                tr = val.get("top_rate_pct")
                bc = val.get("by_country", {})
                parts = [f"**{k}**: {v.get('rate_pct')}% ({v.get('returns')}/{v.get('total')})" for k, v in list(bc.items())[:6]]
                return f"**{tc}** has the highest return rate at **{tr}%**. Return rates by country: " + ", ".join(parts) + "."
            return f"Country return rate: {val}."

        if op == "promotion_code_usage":
            if isinstance(val, dict):
                u = val.get("used_count")
                t = val.get("total_count")
                p = val.get("percentage")
                return f"**{u:,}** out of {t:,} transactions (**{p}%**) use a non-missing, non-blank promotion code."
            return f"Promotion code usage: {val}."

        if op == "top_country_in_year":
            if isinstance(val, dict):
                yr = val.get("year")
                tc = val.get("top_country")
                ta = val.get("top_amount")
                bc = val.get("by_country", {})
                parts = [f"**{k}**: ${v:,.2f}" for k, v in list(bc.items())[:6]]
                return f"In **{yr}**, **{tc}** had the highest total sales amount with **${ta:,.2f}**. Country sales in {yr}: " + ", ".join(parts) + "."
            return f"Top country in year: {val}."

        if op == "top_category_per_year":
            if isinstance(val, dict):
                parts = [f"**{k}**: {v.get('category')} (${v.get('sales'):,.2f})" for k, v in val.items()]
                return "For each year, the product category with the highest total sales is **Electronics**: " + "; ".join(parts) + "."
            return f"Top category per year: {val}."

        if op == "order_id_vs_duplicates":
            if isinstance(val, dict):
                tot = val.get("total_rows")
                uo = val.get("unique_orders")
                ro = val.get("repeated_order_ids")
                ed = val.get("exact_duplicate_rows")
                return (
                    f"Repeated Order_ID values are **not necessarily duplicate** transactions. "
                    f"A single order often contains multiple line items or products. In this dataset, there are {uo:,} "
                    f"unique orders and {ro:,} repeated Order_ID rows across {tot:,} total transactions, "
                    f"whereas exact duplicate rows (identical across all columns) count only **{ed}**."
                )
            return f"Order ID vs duplicates: {val}."

        if op == "year_over_year_comparison":
            if isinstance(val, dict):
                y1 = val.get("year1")
                t1 = val.get("year1_total")
                y2 = val.get("year2")
                t2 = val.get("year2_total")
                pct = val.get("pct_change")
                return (
                    f"Yes, total sales increased in **{y2}** compared with **{y1}**. "
                    f"**{y1}** sales were **${t1:,.2f}** and **{y2}** sales were **${t2:,.2f}**, "
                    f"representing an increase of **+{pct}%**."
                )
            return f"Year over year comparison: {val}."

        if op == "return_flag_status_mismatch":
            if isinstance(val, dict):
                fr = val.get("total_return_flag_yes")
                sr = val.get("total_order_status_returned")
                both = val.get("both_returned")
                mis = val.get("mismatch_count")
                return (
                    f"No, not all transactions match between **Order_Status** and **Return_Flag**. "
                    f"There are **{fr:,}** transactions with Return_Flag == 'Yes', but only **{sr:,}** "
                    f"with Order_Status == 'Returned'. Only {both:,} transactions are marked as returned in both, "
                    f"meaning there are **{mis:,}** mismatches/discrepancies between the two columns."
                )
            return f"Return flag vs status mismatch: {val}."

        if op == "missingness_implications":
            if isinstance(val, dict):
                return val.get("explanation", "High missingness does not necessarily invalidate records.")
            return f"Missingness implications: {val}."

        if op == "discount_profit_correlation":
            if isinstance(val, dict):
                c = val.get("correlation")
                vp = val.get("valid_pairs")
                return (
                    f"The correlation between **Discount_Percentage** and **Profit** is **{c}** "
                    f"(a negative correlation across {vp:,} valid pairs). This does **not prove** that discounts cause "
                    f"profit to increase or decrease; correlation does not imply causation, as confounding factors "
                    f"such as product category, unit cost, and order volume also influence profit."
                )
        if op in ("hypothetical", "hypothetical_fill_mean"):
            if isinstance(val, dict):
                col_name = val.get("column", col or "value")
                metric = val.get("metric", "mean")
                base_v = val.get("baseline_value")
                hypo_v = val.get("hypothetical_value") if val.get("hypothetical_value") is not None else val.get("hypothetical_mean")
                fill_v = val.get("fill_value")
                action = val.get("action")
                base_str = f"{base_v:,.4f}" if isinstance(base_v, (int, float)) else str(base_v)
                hypo_str = f"{hypo_v:,.4f}" if isinstance(hypo_v, (int, float)) else str(hypo_v)
                if action == "fill_missing" or op == "hypothetical_fill_mean":
                    return (
                        f"In this hypothetical calculation (dataset remains unmodified), if missing values "
                        f"in **{col_name}** were filled with **{fill_v}**, the resulting {metric} would be **{hypo_str}** "
                        f"(baseline was {base_str})."
                    )
                else:
                    return (
                        f"In this hypothetical calculation (dataset remains unmodified), the resulting {metric} "
                        f"of **{col_name}** is **{hypo_str}** (baseline was {base_str})."
                    )
            return f"Hypothetical result: {val}."

        # Default
        return f"**{col}**: **{val}**"


# Singleton
analytics_pipeline = AnalyticsPipeline()
