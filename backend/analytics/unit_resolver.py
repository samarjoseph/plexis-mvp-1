"""
Unit Resolver — Reusable, trustworthy unit resolution mechanism for Plexis.

Rules:
  1. Never infer units from column name, value magnitude, or common industry convention.
  2. Query trusted sources in strict order:
     a) Explicit dataset or column metadata.
     b) A documented data dictionary or verified schema definition.
     c) An explicitly supplied user definition.
  3. Distinguish:
     - known: a verified unit is established.
     - unknown: the unit could not be established.
     - not_applicable: metric has no physical measurement unit (counts, ratios, etc.).
  4. Retain provenance for audit and verification.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class UnitResolution:
    status: str                  # "known" | "unknown" | "not_applicable"
    unit: Optional[str] = None   # e.g. "min", "USD", "°C", or None
    label: str = ""              # e.g. "min" or "Unit not specified in dataset metadata"
    provenance: Optional[str] = None  # "column_metadata" | "data_dictionary" | "user_definition" | None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "unit": self.unit,
            "label": self.label,
            "provenance": self.provenance,
        }


# Operations and metrics that inherently have no physical measurement unit
_DIMENSIONLESS_OPERATIONS = frozenset({
    "count", "nunique", "row_count", "total_cell_count",
    "complete_cell_count", "duplicate_count", "missing_count_total",
    "missing_count_column", "rows_with_missing_count",
    "complete_rows_count", "has_duplicates",
    "frequency", "quantile", "correlation", "covariance",
    "group_count",
})


def is_dimensionless_metric(col: Optional[str], op: Optional[str]) -> bool:
    """True if the operation or metric is count-based, ratio, index, or dimensionless."""
    op_norm = (op or "").lower()
    if op_norm in _DIMENSIONLESS_OPERATIONS:
        return True

    col_norm = (col or "").lower()
    if any(col_norm.endswith(sfx) for sfx in ("_count", "_pct", "_percentage", "_ratio", "_rate", "_idx", "_index", "_rank")):
        return True

    return False


def resolve_unit(
    column: Optional[str],
    operation: Optional[str] = None,
    dataset_metadata: Optional[Dict[str, Any]] = None,
    data_dictionary: Optional[Dict[str, Any]] = None,
    user_definitions: Optional[Dict[str, Any]] = None,
) -> UnitResolution:
    """
    Resolve unit using trusted sources only:
      1. Explicit column / dataset metadata.
      2. Documented data dictionary / schema definition.
      3. Explicit user definition.

    Never guesses units from column names alone (e.g. Departure_Delay has no unit metadata -> unknown).
    """
    col = (column or "").strip()
    op = (operation or "").strip().lower()

    # Check if metric is dimensionless
    if is_dimensionless_metric(col, op):
        return UnitResolution(
            status="not_applicable",
            unit=None,
            label="",
            provenance="metric_type_dimensionless",
        )

    # 1. Check explicit dataset or column metadata
    if dataset_metadata and isinstance(dataset_metadata, dict):
        col_meta = None
        columns_val = dataset_metadata.get("columns")
        if isinstance(columns_val, dict):
            col_meta = columns_val.get(col)
        elif isinstance(columns_val, list):
            for item in columns_val:
                if isinstance(item, dict) and item.get("name") == col:
                    col_meta = item
                    break
        if not col_meta and col:
            col_meta = dataset_metadata.get(col)

        # Also inspect 'column_types' (dataset_profiler output format)
        if not col_meta and col:
            col_types = dataset_metadata.get("column_types")
            if isinstance(col_types, list):
                for item in col_types:
                    if isinstance(item, dict) and item.get("name") == col:
                        col_meta = item
                        break

        if isinstance(col_meta, dict) and col_meta.get("unit"):
            unit_val = str(col_meta["unit"]).strip()
            return UnitResolution(
                status="known",
                unit=unit_val,
                label=unit_val,
                provenance="column_metadata",
            )
        # Dataset-level units map
        units_map = dataset_metadata.get("units")
        if isinstance(units_map, dict) and col and units_map.get(col):
            unit_val = str(units_map[col]).strip()
            return UnitResolution(
                status="known",
                unit=unit_val,
                label=unit_val,
                provenance="dataset_metadata",
            )

    # 2. Check documented data dictionary or verified schema definition
    if data_dictionary and isinstance(data_dictionary, dict) and col:
        entry = data_dictionary.get(col) or data_dictionary.get(col.lower())
        if isinstance(entry, dict) and entry.get("unit"):
            unit_val = str(entry["unit"]).strip()
            return UnitResolution(
                status="known",
                unit=unit_val,
                label=unit_val,
                provenance="data_dictionary",
            )

    # 3. Check explicitly supplied user definitions
    if user_definitions and isinstance(user_definitions, dict) and col:
        user_unit = user_definitions.get(col) or user_definitions.get(col.lower())
        if user_unit:
            unit_val = str(user_unit).strip()
            return UnitResolution(
                status="known",
                unit=unit_val,
                label=unit_val,
                provenance="user_definition",
            )

    # If no trusted unit definition is found, return explicit unknown status
    return UnitResolution(
        status="unknown",
        unit=None,
        label="Unit not specified in dataset metadata",
        provenance=None,
    )
