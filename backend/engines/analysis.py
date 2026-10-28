"""
AnalysisEngine — the analytical execution engine for Plexis.

Handles DATA_OPERATION intents from the MasterRouter.

CORRECTED ARCHITECTURE (Phase 4):
  The AnalysisEngine now delegates ALL analytical computation to the pipeline:
    AnalyticsPipeline → Planner → Validator → Executor → Verifier → PostgreSQL

  The Capability registry is kept as a FAST PATH for operations that have
  a direct, unambiguous mapping and don't need LLM planning.
  e.g., if context.operation == "MAX" and column is already resolved.

  But the PRIMARY path for all natural-language queries is now the pipeline.

Separation of concerns:
  - Router: WHAT kind of request (DATA_OPERATION)
  - Pipeline/Planner: WHAT the operation means (max(age))
  - Executor: COMPUTE the answer (65)
  - Verifier: CONFIRM the answer
  - Composer: EXPLAIN the answer (never change it)
"""
from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

import pandas as pd

from engines.base import BaseEngine, EngineResult
from analytics.result import AnalyticalResult

if TYPE_CHECKING:
    from core.context import ExecutionContext

logger = logging.getLogger(__name__)


class AnalysisEngine(BaseEngine):
    """
    Handles DATA_OPERATION intents.

    Primary path: AnalyticsPipeline (planner → executor → verifier)
    """

    @property
    def engine_name(self) -> str:
        return "analysis"

    def can_handle(self, context: "ExecutionContext") -> bool:
        return getattr(context, "intent", None) in ("analysis", "DATA_OPERATION", "data_operation")

    def handle(self, context: "ExecutionContext") -> EngineResult:
        logger.info("[AnalysisEngine] Handling DATA_OPERATION: %s", context.message[:80])

        # ── Step 1: Resolve DataFrame ─────────────────────────────────────────────
        df = self._resolve_dataframe(context)
        if df is None:
            return EngineResult(
                answer="I don't have a dataset to analyze. Please upload one first.",
                source="analysis",
                success=False,
                should_compose=False,
            )

        #  Step 2: Run the analytical pipeline for each operation
        ops = getattr(context, "llm_operations", [getattr(context, "llm_operation", None)])
        if not ops:
            ops = [None]
            
        results = []
        for op in ops:
            # Isolate operation
            context.llm_operation = op
            try:
                db = self._get_db()
                from analytics.pipeline import analytics_pipeline
                res = analytics_pipeline.run(context, df, db=db)
                results.append(res)
            except Exception as e:
                logger.error("[AnalysisEngine] Pipeline error: %s", e, exc_info=True)
                return EngineResult(
                    answer="An error occurred during analysis.",
                    source="analysis",
                    success=False,
                    should_compose=False,
                )

        #  Step 3: Handle pipeline failure 
        all_verified = all(r.verified for r in results)
        if not all_verified:
            failed_res = next((r for r in results if not r.verified), None)
            v_details = failed_res.verification_details if failed_res else {}
            err = v_details.get("error", "unknown")
            code = v_details.get("code", "")
            msg = v_details.get("message", "")
            logger.warning("[AnalysisEngine] Pipeline could not compute result: %s (code=%s)", err, code)

            # Handle nonexistent column references gracefully with authoritative schema refusal
            if code == "COLUMN_NOT_FOUND" or "not found in dataset" in str(msg).lower():
                import re
                target_col = v_details.get("column") or getattr(failed_res, "column", None)
                if not target_col:
                    m = re.search(r"Column '([^']+)' not found", str(msg))
                    if m:
                        target_col = m.group(1)
                col_name = target_col or "requested"
                return EngineResult(
                    answer=f"The dataset does not contain a **{col_name}** column. The available columns in this dataset are: {', '.join(df.columns)}.",
                    source="analysis",
                    provider="system",
                    success=True,
                    should_compose=False,
                )

            # Try capability fallback for simple cases (only if single operation)
            if len(results) == 1:
                engine_result = self._capability_fallback(context, df)
                if engine_result:
                    return engine_result

            # If planning failed completely, tell the user honestly
            if err == "planner_failed":
                return EngineResult(
                    answer=(
                        "I couldn't reliably interpret that analytical request. "
                        "Could you rephrase it? For example: 'what is the highest age?'"
                    ),
                    source="analysis",
                    success=False,
                    should_compose=False,
                )
            return EngineResult(
                answer="",
                source="analysis",
                success=True,
                should_compose=True,  # Let LLM try for non-analytical failures
                facts={"query": context.message},
            )


        #  Step 4: Build EngineResult from verified AnalyticalResult 
        dataset_name = getattr(context, "dataset_filename", "the dataset") or "the dataset"
        from analytics.pipeline import analytics_pipeline
        narratives = [analytics_pipeline.build_narrative(r, dataset_name) for r in results]
        
        # Memoized dataset dimensions to avoid recomputing 600ms full-table scans on every request
        df_cache = getattr(df, "_plexis_dim_cache", None)
        if df_cache is None or df_cache.get("shape") != df.shape:
            total_missing = int(df.isna().sum().sum())
            total_cells = len(df) * len(df.columns)
            rows_with_missing = int(df.isna().any(axis=1).sum())
            complete_rows = len(df) - rows_with_missing
            dup_rows = int(df.duplicated().sum())
            df_cache = {
                "shape": df.shape,
                "total_missing": total_missing,
                "total_cells": total_cells,
                "rows_with_missing": rows_with_missing,
                "complete_rows": complete_rows,
                "duplicate_rows": dup_rows,
            }
            try:
                df._plexis_dim_cache = df_cache
            except Exception:
                pass
        else:
            total_missing = df_cache["total_missing"]
            total_cells = df_cache["total_cells"]
            rows_with_missing = df_cache["rows_with_missing"]
            complete_rows = df_cache["complete_rows"]
            dup_rows = df_cache["duplicate_rows"]

        facts = {
            "computed_answer": "\n\n".join(narratives),
            "dataset_dimensions": {
                "row_count": len(df),
                "column_count": len(df.columns),
                "columns": list(df.columns),
                "total_cells": total_cells,
                "total_missing_cells": total_missing,
                "complete_cells": total_cells - total_missing,
                "rows_with_missing": rows_with_missing,
                "complete_rows": complete_rows,
                "duplicate_rows": dup_rows,
            },
            "operations": [
                {
                    "value": r.value,
                    "operation": r.operation,
                    "column": r.column,
                    "details": getattr(r, "details", {}),
                    "summary": narratives[i] if i < len(narratives) else "",
                    "matching_rows": r.matching_rows[:50],
                    "row_indices": r.row_indices[:50]
                }
                for i, r in enumerate(results)
            ]
        }
        if results:
            context.analytical_result = results[0]
            context.analysis_id = str(results[0].analysis_id)
        evidence = [r.to_response_dict() for r in results]

        # Execution architecture: Class A vs. Hybrid vs. Unverified fallback
        is_hybrid = getattr(context, "execution_class", "") == "Hybrid"
        has_content = bool(narratives and all(n and n.strip() for n in narratives))
        can_bypass_llm = all_verified and has_content and not is_hybrid

        engine_result = EngineResult(
            answer="\n\n".join(narratives),
            source="analysis",
            provider="system",
            success=True,
            should_compose=not can_bypass_llm,
            facts=facts,
            evidence=evidence,
        )

        return engine_result

    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    def _resolve_dataframe(self, context: "ExecutionContext") -> Optional[pd.DataFrame]:
        """L1 (in-memory registry) → L2 (PostgreSQL restore)."""
        dataset_id = getattr(context, "dataset_id", None)

        if dataset_id:
            try:
                from datasets.registry import dataset_registry
                entry = dataset_registry.get(str(dataset_id))
                if entry and hasattr(entry, "dataframe") and entry.dataframe is not None:
                    logger.debug("[AnalysisEngine] L1 hit: dataset_id=%s", dataset_id)
                    return entry.dataframe
            except Exception as e:
                logger.warning("[AnalysisEngine] DatasetRegistry error: %s", e)

        if dataset_id:
            try:
                db = self._get_db()
                from db.services.dataset_service import dataset_service
                import uuid
                user_id = getattr(context, "user_id", None)
                ds_uuid = uuid.UUID(str(dataset_id)) if not isinstance(dataset_id, uuid.UUID) else dataset_id
                _dataset, df = dataset_service.restore_dataset_df(db, ds_uuid, user_id)
                if df is not None:
                    logger.info("[AnalysisEngine] L2 restore: dataset_id=%s", dataset_id)
                    return df
            except Exception as e:
                logger.error("[AnalysisEngine] L2 restore failed: %s", e, exc_info=True)

        return None

    def _get_db(self):
        """Get the current SQLAlchemy session."""
        try:
            from db.session import get_db
            return get_db()
        except Exception as e:
            logger.warning("[AnalysisEngine] _get_db failed: %s", e)
            return None

    def _capability_fallback(
        self,
        context: "ExecutionContext",
        df: pd.DataFrame,
    ) -> Optional[EngineResult]:
        """
        Fallback to the Capability registry for simple unambiguous operations.
        Used when the planner fails but we have high-confidence context signals.
        """
        try:
            from capabilities.registry import capability_registry
            from capabilities.base import CapabilityResult

            capability = capability_registry.resolve(context)
            if capability is None:
                return None

            logger.info(
                "[AnalysisEngine] Capability fallback: %s", capability.capability_id
            )
            cap_result: CapabilityResult = capability.execute(context, df)

            if not cap_result.success:
                return None

            has_hint = bool(cap_result.narrative_hint)
            return EngineResult(
                answer=cap_result.narrative_hint if has_hint else "",
                source="analysis",
                provider="system",
                success=True,
                facts=cap_result.facts,
                evidence=cap_result.evidence,
                should_compose=not has_hint,
                metadata={"capability_fallback": capability.capability_id},
            )
        except Exception as e:
            logger.warning("[AnalysisEngine] Capability fallback error: %s", e)
            return None


analysis_engine = AnalysisEngine()
