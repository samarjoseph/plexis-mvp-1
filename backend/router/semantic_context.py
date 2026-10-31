"""
SemanticContextBuilder — the 20% deterministic supporting signal layer.

PURPOSE:
  Builds a structured context package that the LLM interpreter uses to
  make semantic decisions. This module provides facts, not decisions.

WHAT IT DOES (deterministic):
  - Extracts column names + types from the loaded dataset
  - Identifies which columns appear to be referenced in the query
  - Determines which analytical capabilities are schema-compatible
    (based on column type, not query keywords)
  - Packages workspace context (recent operations, active selection)
  - Produces a compact schema summary

WHAT IT DOES NOT DO:
  - Does not decide intent
  - Does not score queries against keywords
  - Does not classify analytical vs conversational
  - Does not match synonyms

The LLM receives this context and makes the final semantic decision.
"""
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Supported capabilities and their schema requirements ─────────────────────
# This is structural/schema-level information, NOT semantic matching.
# The LLM resolves which capability fits the user's query.
_CAPABILITY_SCHEMA = [
    {
        "type": "MIN",
        "description": "Find the row(s) with the minimum value in a numeric column",
        "requires_numeric": True,
        "multi_column": False,
    },
    {
        "type": "MAX",
        "description": "Find the row(s) with the maximum value in a numeric column",
        "requires_numeric": True,
        "multi_column": False,
    },
    {
        "type": "MEAN",
        "description": "Calculate the average (mean) of a numeric column",
        "requires_numeric": True,
        "multi_column": False,
    },
    {
        "type": "MEDIAN",
        "description": "Calculate the median of a numeric column",
        "requires_numeric": True,
        "multi_column": False,
    },
    {
        "type": "STD",
        "description": "Calculate the standard deviation of a numeric column",
        "requires_numeric": True,
        "multi_column": False,
    },
    {
        "type": "SUM",
        "description": "Calculate the sum of a numeric column",
        "requires_numeric": True,
        "multi_column": False,
    },
    {
        "type": "COUNT",
        "description": "Count rows, optionally with a filter condition",
        "requires_numeric": False,
        "multi_column": False,
    },
    {
        "type": "TOP_N",
        "description": "Find the top N rows by a column value (highest to lowest)",
        "requires_numeric": False,
        "multi_column": False,
    },
    {
        "type": "BOTTOM_N",
        "description": "Find the bottom N rows by a column value (lowest to highest)",
        "requires_numeric": False,
        "multi_column": False,
    },
    {
        "type": "FILTER",
        "description": "Filter rows by a condition on a column value",
        "requires_numeric": False,
        "multi_column": False,
    },
    {
        "type": "FIND_DUPLICATES",
        "description": "Find rows with duplicate values in one or more columns",
        "requires_numeric": False,
        "multi_column": True,
    },
    {
        "type": "FIND_MISSING",
        "description": "Find rows with missing/null/empty values in columns",
        "requires_numeric": False,
        "multi_column": True,
    },
    {
        "type": "FIND_OUTLIERS",
        "description": "Find statistical outliers in a numeric column using IQR or z-score",
        "requires_numeric": True,
        "multi_column": False,
    },
    {
        "type": "CORRELATION",
        "description": "Calculate the correlation between two numeric columns",
        "requires_numeric": True,
        "multi_column": True,
    },
]

# ── Operation type mappings for normalize bridge ──────────────────────────────
# The LLM may output MIN/MAX/MEAN/MEDIAN/STD/SUM — map these to AGGREGATE
# since the backend operates on TOP_N/BOTTOM_N/AGGREGATE for the operate endpoint.
# MIN → BOTTOM_N(n=1) or AGGREGATE(func=min); MAX → TOP_N(n=1) or AGGREGATE(func=max)
OPERATION_BRIDGE = {
    "MIN": "min_value",       # capability_id
    "MAX": "max_value",       # capability_id
    "MEAN": "AGGREGATE",
    "MEDIAN": "AGGREGATE",
    "STD": "AGGREGATE",
    "SUM": "AGGREGATE",
    "COUNT": "filter_count",
}


class SemanticContextBuilder:
    """
    Builds structured supporting context for the LLM interpreter.

    Called once per request, before the LLM interpretation call.
    Returns a dict that is serialized into the LLM prompt.
    """

    def build(self, context: Any) -> Dict:
        """
        Build the supporting semantic context.

        Returns a structured dict with:
          - dataset_loaded, dataset_filename
          - columns (list of {name, type, sample_values})
          - referenced_columns (columns appearing in the query by name)
          - compatible_capabilities (schema-compatible capabilities)
          - workspace_context (recent ops, selection, evidence)
        """
        result = {
            "dataset_loaded": False,
            "dataset_id": "",
            "dataset_fingerprint": "",
            "dataset_filename": None,
            "columns": [],
            "referenced_columns": [],
            "compatible_capabilities": [],
            "workspace_context": {},
        }

        # ── 1. Dataset schema ─────────────────────────────────────────────────
        if getattr(context, 'dataset_id', None):
            result["dataset_loaded"] = True
            result["dataset_id"] = str(context.dataset_id)
            result["dataset_filename"] = getattr(context, 'dataset_filename', None) or ""
            try:
                from datasets.registry import dataset_registry
                entry = dataset_registry.get_by_id(context.dataset_id)
                if entry:
                    result["dataset_fingerprint"] = getattr(entry, 'file_fingerprint', '') or ""
            except Exception:
                pass

            columns = self._extract_columns(context)
            result["columns"] = columns

            # Referenced columns: any column name that appears in the query
            query = (getattr(context, 'normalized_query', '') or
                     getattr(context, 'message', '')).lower()
            result["referenced_columns"] = self._find_referenced_columns(query, columns)

            # Compatible capabilities: schema-level compatibility only
            result["compatible_capabilities"] = self._compatible_capabilities(columns)

        # ── 2. Workspace context ──────────────────────────────────────────────
        result["workspace_context"] = self._build_workspace_context(context)

        # ── 3. Conversation context (recent turns for multi-turn follow-ups) ──
        history = getattr(context, 'conversation_history', None) or []
        if history:
            result["recent_conversation"] = [
                {"role": turn.get("role", "user"), "content": str(turn.get("content", ""))[:200]}
                for turn in history[-4:]
            ]

        return result

    # ── Column extraction ─────────────────────────────────────────────────────

    def _extract_columns(self, context: Any) -> List[Dict]:
        """Extract column list with type information from context."""
        columns = []

        try:
            # Priority 1: live dataframe (most accurate types)
            if getattr(context, 'dataset_id', None):
                from datasets.registry import dataset_registry
                entry = dataset_registry.get_by_id(context.dataset_id)
                if entry and entry.dataframe is not None:
                    df = entry.dataframe
                    for col in df.columns[:50]:  # Cap at 50 columns
                        dtype = str(df[col].dtype)
                        col_type = self._classify_dtype(dtype)
                        col_info = {
                            "name": col,
                            "type": col_type,
                            "dtype": dtype,
                        }
                        # Add a few sample values for the LLM (non-null, first 3)
                        try:
                            samples = df[col].dropna().head(3).tolist()
                            col_info["samples"] = [str(s) for s in samples]
                        except Exception:
                            col_info["samples"] = []
                        columns.append(col_info)
                    return columns
        except Exception as e:
            logger.debug(f"Column extraction from dataframe failed: {e}")

        # Priority 2: schema_profile already on context
        try:
            if getattr(context, 'schema_profile', None):
                for col in context.schema_profile[:50]:
                    if isinstance(col, dict):
                        columns.append({
                            "name": col.get('name', ''),
                            "type": col.get('type', 'unknown'),
                            "dtype": col.get('dtype', ''),
                            "samples": col.get('sample_values', [])[:3],
                        })
                return columns
        except Exception as e:
            logger.debug(f"Column extraction from schema_profile failed: {e}")

        # Priority 3: dataset_profile
        try:
            if getattr(context, 'dataset_profile', None):
                cols = context.dataset_profile.get('columns', [])
                for col in cols[:50]:
                    if isinstance(col, dict):
                        columns.append({
                            "name": col.get('name', ''),
                            "type": col.get('type', 'unknown'),
                            "dtype": '',
                            "samples": [],
                        })
                    elif isinstance(col, str):
                        columns.append({"name": col, "type": "unknown", "dtype": '', "samples": []})
        except Exception as e:
            logger.debug(f"Column extraction from dataset_profile failed: {e}")

        return columns

    def _classify_dtype(self, dtype: str) -> str:
        """Map pandas dtype string to semantic type."""
        dtype_lower = dtype.lower()
        if any(t in dtype_lower for t in ('int', 'float', 'numeric', 'double', 'decimal')):
            return 'numeric'
        if any(t in dtype_lower for t in ('datetime', 'timestamp', 'date', 'time')):
            return 'datetime'
        if 'bool' in dtype_lower:
            return 'boolean'
        return 'text'

    def _find_referenced_columns(self, query: str, columns: List[Dict]) -> List[str]:
        """
        Find column names that appear in the query (case-insensitive, word boundary).
        Deterministic name matching — no semantic inference.
        """
        referenced = []
        for col in columns:
            name = col.get('name', '')
            if not name:
                continue
            # Match whole word or underscore-joined words
            name_lower = name.lower()
            name_spaced = name_lower.replace('_', ' ')
            if (name_lower in query or
                    name_spaced in query or
                    re.search(r'\b' + re.escape(name_lower) + r'\b', query)):
                referenced.append(name)
        return referenced

    # ── Capability schema compatibility ───────────────────────────────────────

    def _compatible_capabilities(self, columns: List[Dict]) -> List[Dict]:
        """
        Return capabilities that are schema-compatible with the loaded dataset.
        This is purely structural (does the dataset have numeric columns? etc.)
        The LLM chooses which capability fits the user's intent.
        """
        has_numeric = any(c.get('type') == 'numeric' for c in columns)
        has_multiple_numeric = sum(1 for c in columns if c.get('type') == 'numeric') >= 2
        has_any = len(columns) > 0

        compatible = []
        for cap in _CAPABILITY_SCHEMA:
            if cap['requires_numeric'] and not has_numeric:
                continue
            if cap['multi_column'] and cap.get('type') == 'CORRELATION' and not has_multiple_numeric:
                continue
            if not has_any:
                continue
            compatible.append({
                "type": cap["type"],
                "description": cap["description"],
            })
        return compatible

    # ── Workspace context ─────────────────────────────────────────────────────

    def _build_workspace_context(self, context: Any) -> Dict:
        """
        Extract workspace context (recent operations, active selection, evidence).
        Used to resolve references like 'that result', 'these rows', 'the same operation'.
        """
        ws = {}

        workspace_state = getattr(context, 'workspace_state', None) or {}
        workspace_summary = getattr(context, 'workspace_summary', '') or ''
        workspace_action = getattr(context, 'workspace_action', None) or {}

        if workspace_summary:
            ws['summary'] = workspace_summary[:400]

        # Recent operation history
        history = workspace_state.get('operationHistory', {})
        recent_ops = history.get('recent', [])
        if recent_ops:
            ws['recent_operations'] = [
                {"type": op.get('type'), "column": op.get('column')}
                for op in recent_ops[:3]
            ]

        # Active selection
        selection = workspace_state.get('selection', {})
        selected_rows = selection.get('selectedRows', [])
        if selected_rows:
            ws['selected_rows'] = selected_rows[:20]
            ws['selected_row_count'] = len(selected_rows)

        # Active workspace action (e.g. explain_evidence, explain_row)
        if workspace_action and workspace_action.get('type'):
            ws['active_action'] = {
                "type": workspace_action.get('type'),
                "description": workspace_action.get('description', ''),
            }

        return ws


semantic_context_builder = SemanticContextBuilder()
