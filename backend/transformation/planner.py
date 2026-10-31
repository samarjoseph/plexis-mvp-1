"""
TransformationPlanner — deterministic plan calculator.

Computes the exact cells that WOULD be affected by a transformation operation,
without actually mutating the DataFrame. Returns a TransformationPlan.

Safety: No eval/exec. All operations are explicit pandas method dispatches.
"""
from __future__ import annotations
import hashlib
import io
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import pandas as pd

from transformation.contracts import TransformationPlan, AffectedCell

logger = logging.getLogger(__name__)


class TransformationClarificationRequired(ValueError):
    """A valid transformation is missing a required, user-selectable slot."""

    def __init__(self, message: str, *, missing_slots: list[str], candidate_columns=None,
                 clarification_type: str = 'COLUMN_SCOPE', operation_payload: dict | None = None,
                 ambiguous_options: list | None = None):
        super().__init__(message)
        self.missing_slots = list(missing_slots)
        self.candidate_columns = list(candidate_columns or [])
        self.clarification_type = clarification_type
        self.operation_payload = operation_payload
        self.ambiguous_options = list(ambiguous_options or [])


class ValueTypeMismatchConfirmationRequired(ValueError):
    """A valid transformation requested a typed value that does not exist, but a strong textual alternative exists."""

    def __init__(self, message: str, *, requested_value_info: dict, candidate_value_info: dict,
                 candidate_columns: list[str], candidate_count: int, operation_payload: dict):
        super().__init__(message)
        self.requested_value_info = requested_value_info
        self.candidate_value_info = candidate_value_info
        self.candidate_columns = list(candidate_columns)
        self.candidate_count = candidate_count
        self.operation_payload = operation_payload
        self.clarification_type = 'VALUE_TYPE_MISMATCH'


def _column_matches_semantic_scope(
    series: pd.Series, semantic_type: str | None, semantic_profile=None,
) -> bool:
    """Resolve semantic column scope from pandas dtype and cached scalar shape."""
    from pandas.api import types as ptypes

    kind = (semantic_type or '').strip().upper()
    dtype = series.dtype
    profile_attribute = {
        'NUMBER': 'numeric_stats', 'NUMERIC': 'numeric_stats',
        'INTEGER': 'numeric_stats', 'FLOAT': 'numeric_stats',
        'BOOLEAN': 'categorical_stats', 'BOOL': 'categorical_stats',
        'TEXT': 'categorical_stats', 'STRING': 'categorical_stats',
        'DATE': 'datetime_stats', 'DATETIME': 'datetime_stats',
        'CATEGORICAL': 'categorical_stats',
    }.get(kind)
    if semantic_profile is not None and profile_attribute:
        if getattr(semantic_profile, profile_attribute, None) is not None:
            return True
    if kind in {'NUMBER', 'NUMERIC', 'INTEGER', 'FLOAT'}:
        return ptypes.is_numeric_dtype(dtype) and not ptypes.is_bool_dtype(dtype)
    if kind in {'BOOLEAN', 'BOOL'}:
        return ptypes.is_bool_dtype(dtype)
    if kind in {'DATE', 'DATETIME'}:
        return ptypes.is_datetime64_any_dtype(dtype)
    if kind in {'TEXT', 'STRING'}:
        if ptypes.is_string_dtype(dtype) and not ptypes.is_object_dtype(dtype):
            return True
        if ptypes.is_object_dtype(dtype):
            types = series.map(lambda value: type(value).__name__).value_counts()
            return types.get('str', 0) > len(series) / 2
    if kind == 'CATEGORICAL':
        if ptypes.is_categorical_dtype(dtype):
            return True
        if len(series) == 0:
            return False
        return series.nunique(dropna=True) / max(1, len(series)) <= 0.2
    return False

# Allowlisted operation types
ALLOWED_OPERATIONS = frozenset({
    'FILL_MISSING',
    'DROP_DUPLICATES',
    'RENAME_COLUMN',
    'DROP_COLUMN',
    'ADD_COLUMN',
    'FILTER_ROWS',
    'CAST_DTYPE',
    'REPLACE_VALUES',
    'SORT_ROWS',
    'CLIP',
    'UPDATE_EXTREMUM',
    'EXTREME_VALUE_AMBIGUITY',
})


def _compute_fingerprint(df: pd.DataFrame) -> str:
    """Compute a lightweight, ultra-fast fingerprint of a DataFrame for staleness detection (< 2ms)."""
    if df is None or not isinstance(df, pd.DataFrame):
        return ""
    n = len(df)
    cols = list(df.columns)
    dtypes = [str(d) for d in df.dtypes]
    if n <= 30:
        sample_vals = [tuple(df.iloc[i].values) for i in range(n)]
    else:
        sample_indices = list(range(10)) + list(range(n // 2 - 5, n // 2 + 5)) + list(range(n - 10, n))
        sample_vals = [tuple(df.iloc[i].values) for i in sample_indices]
    sig = f"{n}:{cols}:{dtypes}:{sample_vals}"
    return hashlib.sha256(sig.encode('utf-8', errors='replace')).hexdigest()[:32]


class TransformationPlanner:

    def _resolve_column_name(self, df: pd.DataFrame, col_name: str) -> Optional[str]:
        if not col_name:
            return None
        if col_name in df.columns:
            return col_name
        col_lower = {c.lower(): c for c in df.columns}
        if col_name.lower() in col_lower:
            return col_lower[col_name.lower()]
        norm = lambda s: re.sub(r'[\s_-]+', '', str(s).lower())
        target_norm = norm(col_name)
        for c in df.columns:
            if norm(c) == target_norm:
                return c
        return None

    def plan(
        self,
        df: pd.DataFrame,
        operation: Dict[str, Any],
        dataset_id: str,
    ) -> TransformationPlan:
        """
        Compute a TransformationPlan without mutating the DataFrame.

        Args:
            df: The current DataFrame (read-only)
            operation: LLM-structured operation dict {type, ...params}
            dataset_id: The dataset this plan applies to

        Returns:
            TransformationPlan with full preview

        Raises:
            ValueError: If operation type is not allowlisted
        """
        op_type = (operation.get('type') or '').upper()
        if op_type not in ALLOWED_OPERATIONS:
            raise ValueError(
                f"Operation type '{op_type}' is not allowlisted. Allowed: {ALLOWED_OPERATIONS}"
            )

        fingerprint = _compute_fingerprint(df)
        plan_id = str(uuid.uuid4())

        if op_type == 'FILL_MISSING':
            return self._plan_fill_missing(df, operation, plan_id, dataset_id, fingerprint)
        elif op_type == 'DROP_DUPLICATES':
            return self._plan_drop_duplicates(df, operation, plan_id, dataset_id, fingerprint)
        elif op_type == 'RENAME_COLUMN':
            return self._plan_rename_column(df, operation, plan_id, dataset_id, fingerprint)
        elif op_type == 'DROP_COLUMN':
            return self._plan_drop_column(df, operation, plan_id, dataset_id, fingerprint)
        elif op_type == 'ADD_COLUMN':
            return self._plan_add_column(df, operation, plan_id, dataset_id, fingerprint)
        elif op_type == 'FILTER_ROWS':
            return self._plan_filter_rows(df, operation, plan_id, dataset_id, fingerprint)
        elif op_type == 'CAST_DTYPE':
            return self._plan_cast_dtype(df, operation, plan_id, dataset_id, fingerprint)
        elif op_type == 'REPLACE_VALUES':
            return self._plan_replace_values(df, operation, plan_id, dataset_id, fingerprint)
        elif op_type == 'SORT_ROWS':
            return self._plan_sort_rows(df, operation, plan_id, dataset_id, fingerprint)
        elif op_type == 'CLIP':
            return self._plan_clip(df, operation, plan_id, dataset_id, fingerprint)
        elif op_type == 'UPDATE_EXTREMUM':
            return self._plan_update_extremum(df, operation, plan_id, dataset_id, fingerprint)
        elif op_type == 'EXTREME_VALUE_AMBIGUITY':
            return self._plan_extreme_ambiguity(df, operation, plan_id, dataset_id, fingerprint)
        else:
            raise ValueError(f"Unhandled operation: {op_type}")

    def _make_sample(self, rows_df: pd.DataFrame, column: str, after_val) -> list:
        """Build before_sample for up to 20 affected rows."""
        sample = []
        for idx, row in rows_df.head(20).iterrows():
            sample.append(AffectedCell(
                row_index=int(idx),
                source_row_number=int(idx) + 1,
                column=column,
                before=row[column] if column in row.index else None,
                after=after_val,
            ))
        return sample

    def _plan_fill_missing(self, df, op, plan_id, dataset_id, fingerprint):
        fill_value = op.get('value', 0)
        columns = op.get('columns') or df.columns.tolist()
        if isinstance(columns, str):
            columns = [columns]
        columns = [c for c in columns if c in df.columns]

        sample = []
        total_cells = 0
        affected_rows_set = set()
        affected_cols = []
        warnings = []
        resolved_fill_values = {}

        for col in columns:
            missing_mask = df[col].isna()
            missing_rows = df[missing_mask]
            count = len(missing_rows)
            if count == 0:
                continue
            
            affected_cols.append(col)
            total_cells += count
            affected_rows_set.update(missing_rows.index.tolist())
            
            if fill_value == "mean":
                val = df[col].mean()
                if hasattr(val, 'item'): val = val.item()
                resolved_fill_values[col] = val
                sample.extend(self._make_sample(missing_rows, col, val))
            elif fill_value == "median":
                val = df[col].median()
                if hasattr(val, 'item'): val = val.item()
                resolved_fill_values[col] = val
                sample.extend(self._make_sample(missing_rows, col, val))
            elif fill_value == "mode":
                mode_series = df[col].mode()
                val = mode_series.iloc[0] if not mode_series.empty else None
                if hasattr(val, 'item'): val = val.item()
                resolved_fill_values[col] = val
                sample.extend(self._make_sample(missing_rows, col, val))
            elif fill_value in ("ffill", "bfill"):
                filled = df[col].ffill() if fill_value == "ffill" else df[col].bfill()
                resolved_fill_values[col] = {}
                for idx, row in missing_rows.iterrows():
                    val = filled.loc[idx]
                    if hasattr(val, 'item'): val = val.item()
                    resolved_fill_values[col][int(idx)] = val
                
                still_missing = int(filled[missing_mask].isna().sum())
                if still_missing > 0:
                    warnings.append(f"Column '{col}' still has {still_missing} missing values after {fill_value}.")
                
                for idx, row in missing_rows.head(20).iterrows():
                    sample.append(AffectedCell(
                        row_index=int(idx),
                        source_row_number=int(idx) + 1,
                        column=col,
                        before=row[col] if col in row.index else None,
                        after=filled.loc[idx],
                    ))
            else:
                sample.extend(self._make_sample(missing_rows, col, fill_value))

        label = f"Fill missing values with {fill_value!r}"
        if len(columns) == 1:
            if fill_value in ("mean", "median", "mode") and columns[0] in resolved_fill_values:
                label = f"Fill missing in '{columns[0]}' with {fill_value} ({resolved_fill_values[columns[0]]})"
            elif fill_value in ("ffill", "bfill"):
                label = f"Fill missing in '{columns[0]}' with {fill_value}"
            else:
                label = f"Fill missing in '{columns[0]}' with {fill_value!r}"

        col_list = ', '.join(f"'{c}'" for c in columns) if len(columns) <= 3 else f'{len(columns)} columns'
        
        if fill_value in ("mean", "median", "mode", "ffill", "bfill"):
            code = f"df[{col_list}] = df[{col_list}].fillna(df[{col_list}].{fill_value}())"
        else:
            code = f"df[{col_list}].fillna({fill_value!r})"
            
        if not affected_cols:
            code = f"# No missing values found in specified columns"
            warnings.append("No missing values found in the specified columns.")

        payload = dict(op)
        if resolved_fill_values:
            payload['resolved_fill_values'] = resolved_fill_values

        return TransformationPlan(
            plan_id=plan_id,
            dataset_id=dataset_id,
            operation_type='FILL_MISSING',
            operation_label=label,
            operation_code=code,
            operation_payload=payload,
            affected_rows_count=len(affected_rows_set),
            affected_cells_count=total_cells,
            affected_columns=affected_cols,
            before_sample=sample[:20],
            dataset_fingerprint=fingerprint,
            warnings=warnings,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def _plan_drop_duplicates(self, df, op, plan_id, dataset_id, fingerprint):
        columns = op.get('columns') or df.columns.tolist()
        if isinstance(columns, str):
            columns = [columns]
        columns = [c for c in columns if c in df.columns]

        dup_mask = df.duplicated(subset=columns, keep='first')
        dup_rows = df[dup_mask]
        n_rows = len(dup_rows)
        n_cells = n_rows * len(df.columns)  # entire rows removed

        sample = []
        for idx, row in dup_rows.head(20).iterrows():
            # Show first column as the representative "before"
            col = columns[0] if columns else df.columns[0]
            sample.append(AffectedCell(
                row_index=int(idx),
                source_row_number=int(idx) + 1,
                column=col,
                before=str(row[col]),
                after='[row removed]',
            ))

        col_str = ', '.join(f"'{c}'" for c in columns[:3])
        label = f"Drop duplicate rows (by {col_str})"
        code = f"df.drop_duplicates(subset={columns!r}, keep='first')"
        return TransformationPlan(
            plan_id=plan_id, dataset_id=dataset_id,
            operation_type='DROP_DUPLICATES', operation_label=label,
            operation_code=code, operation_payload=op,
            affected_rows_count=n_rows, affected_cells_count=n_cells,
            affected_columns=columns, before_sample=sample[:20],
            dataset_fingerprint=fingerprint,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def _plan_rename_column(self, df, op, plan_id, dataset_id, fingerprint):
        renames = op.get('renames')
        if renames:
            invalid = [old for old in renames if old not in df.columns]
            if invalid:
                raise ValueError(f"Column(s) {', '.join(map(str, invalid))} not found in dataset.")
            if any(not str(new).strip() for new in renames.values()):
                raise ValueError("Every renamed column needs a new name.")
            if 'impact' not in op:
                op['impact'] = {'type': 'schema', 'columns_changed': len(renames), 'cells_changed': 0}
            sample = [AffectedCell(
                row_index=-1, source_row_number=None, column=old,
                before=f"Column header: '{old}'", after=f"Column header: '{new}'",
            ) for old, new in renames.items()]
            label = 'Rename columns: ' + ', '.join(f"'{old}' to '{new}'" for old, new in renames.items())
            return TransformationPlan(
                plan_id=plan_id, dataset_id=dataset_id,
                operation_type='RENAME_COLUMN', operation_label=label,
                operation_code=f"# rename columns: {renames!r}", operation_payload=op,
                affected_rows_count=len(df), affected_cells_count=0,
                affected_columns=list(renames), before_sample=sample[:20],
                dataset_fingerprint=fingerprint,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        old_name = op.get('column') or op.get('old_name', '')
        new_name = op.get('new_name', '')
        if old_name not in df.columns:
            raise ValueError(f"Column '{old_name}' not found in dataset.")
        if not new_name:
            raise ValueError("new_name is required for RENAME_COLUMN")

        if old_name == new_name:
            raise ValueError(f"The column is already named '{new_name}', so there is nothing to rename.")
        if new_name in df.columns:
            raise ValueError(f"Destination column '{new_name}' already exists.")
            
        if 'impact' not in op:
            op['impact'] = {'type': 'schema', 'columns_changed': 1, 'cells_changed': 0}
        n_rows = len(df)
        sample = [AffectedCell(
            row_index=-1, source_row_number=None,
            column=old_name, before=f"Column header: '{old_name}'", after=f"Column header: '{new_name}'"
        )]
        label = f"Rename column '{old_name}' to '{new_name}'"
        code = f"df.rename(columns={{'{old_name}': '{new_name}'}})"
        return TransformationPlan(
            plan_id=plan_id, dataset_id=dataset_id,
            operation_type='RENAME_COLUMN', operation_label=label,
            operation_code=code, operation_payload=op,
            affected_rows_count=n_rows, affected_cells_count=0,
            affected_columns=[old_name], before_sample=sample,
            dataset_fingerprint=fingerprint,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def _plan_drop_column(self, df, op, plan_id, dataset_id, fingerprint):
        column = op.get('column', '')
        if column not in df.columns:
            raise ValueError(f"Column '{column}' not found in dataset.")
            
        if 'impact' not in op:
            op['impact'] = {'type': 'schema', 'columns_changed': 1, 'cells_changed': 0}
            
        n_rows = len(df)
        sample = [AffectedCell(
            row_index=-1, source_row_number=None,
            column=column, before=f"Column header: '{column}'", after='Column dropped'
        )]
        label = f"Drop column '{column}'"
        code = f"df.drop(columns=['{column}'])"
        return TransformationPlan(
            plan_id=plan_id, dataset_id=dataset_id,
            operation_type='DROP_COLUMN', operation_label=label,
            operation_code=code, operation_payload=op,
            affected_rows_count=n_rows, affected_cells_count=0,
            affected_columns=[column], before_sample=sample,
            dataset_fingerprint=fingerprint,
            warnings=[f"Warning: Dropping column '{column}' cannot be undone without a version snapshot."],
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def _plan_add_column(self, df, op, plan_id, dataset_id, fingerprint):
        column = op.get('column') or op.get('name') or op.get('new_column', '')
        if not column:
            raise ValueError("Column name is required for ADD_COLUMN")
        if column in df.columns:
            raise ValueError(f"Column '{column}' already exists in dataset.")
            
        value = op.get('value', None)
        default_val_repr = repr(value) if value is not None else "None"
        
        n_rows = len(df)
        sample = [AffectedCell(
            row_index=-1, source_row_number=None,
            column=column, before="[None]", after=f"New column: '{column}' (initial value: {default_val_repr})"
        )]
        label = f"Add column '{column}'"
        code = f"df['{column}'] = {default_val_repr}"
        if 'impact' not in op:
            op['impact'] = {'type': 'schema', 'columns_changed': 1, 'cells_changed': n_rows}
            
        return TransformationPlan(
            plan_id=plan_id, dataset_id=dataset_id,
            operation_type='ADD_COLUMN', operation_label=label,
            operation_code=code, operation_payload=op,
            affected_rows_count=n_rows, affected_cells_count=n_rows,
            affected_columns=[column], before_sample=sample,
            dataset_fingerprint=fingerprint,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def _plan_filter_rows(self, df, op, plan_id, dataset_id, fingerprint):
        column = op.get('column', '')
        operator = op.get('operator', '==')
        value = op.get('value')
        action = op.get('action', 'KEEP').upper()
        if column not in df.columns:
            raise ValueError(f"Column '{column}' not found in dataset.")

        # Apply filter to find matching rows
        col_series = (
            pd.to_numeric(df[column], errors='coerce')
            if operator in ('>', '<', '>=', '<=')
            else df[column]
        )
        op_map = {
            '==': lambda s, v: s == v,
            '!=': lambda s, v: s != v,
            '>':  lambda s, v: s > v,
            '<':  lambda s, v: s < v,
            '>=': lambda s, v: s >= v,
            '<=': lambda s, v: s <= v,
        }
        match_mask = op_map.get(operator, lambda s, v: s == v)(col_series, value)
        
        if action == 'KEEP':
            removed_rows = df[~match_mask]
            label = f"Keep rows where {column} {operator} {value!r} (removes {len(removed_rows)} rows)"
            code = f"df[df['{column}'] {operator} {value!r}]"
        else:
            removed_rows = df[match_mask]
            label = f"Remove rows where {column} {operator} {value!r} (removes {len(removed_rows)} rows)"
            code = f"df[~(df['{column}'] {operator} {value!r})]"

        n_removed = len(removed_rows)

        sample = []
        for idx, row in removed_rows.head(20).iterrows():
            sample.append(AffectedCell(
                row_index=int(idx), source_row_number=int(idx) + 1,
                column=column, before=str(row[column]), after='[row removed]'
            ))

        return TransformationPlan(
            plan_id=plan_id, dataset_id=dataset_id,
            operation_type='FILTER_ROWS', operation_label=label,
            operation_code=code, operation_payload=op,
            affected_rows_count=n_removed,
            affected_cells_count=n_removed * len(df.columns),
            affected_columns=[column], before_sample=sample,
            dataset_fingerprint=fingerprint,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def _plan_cast_dtype(self, df, op, plan_id, dataset_id, fingerprint):
        column = op.get('column', '')
        dtype = op.get('dtype', 'float')
        if column not in df.columns:
            raise ValueError(f"Column '{column}' not found.")
        # Preview what would fail conversion
        try:
            if dtype in ('int', 'float', 'int64', 'float64'):
                converted = pd.to_numeric(df[column], errors='coerce')
            else:
                converted = df[column].astype(dtype, errors='ignore')
            failed_mask = converted.isna() & df[column].notna()
        except Exception:
            failed_mask = pd.Series([False] * len(df))

        n_rows = len(df)
        n_failed = int(failed_mask.sum())
        warnings = [f"{n_failed} values cannot be converted and will become NaN."] if n_failed else []
        label = f"Convert '{column}' to {dtype}"
        if dtype in ('int', 'float'):
            code = f"df['{column}'] = pd.to_numeric(df['{column}'], errors='coerce')"
        else:
            code = f"df['{column}'] = df['{column}'].astype('{dtype}')"
        sample = [AffectedCell(
            row_index=0, source_row_number=1, column=column,
            before=f"dtype: {df[column].dtype}", after=f"dtype: {dtype}"
        )]
        return TransformationPlan(
            plan_id=plan_id, dataset_id=dataset_id,
            operation_type='CAST_DTYPE', operation_label=label,
            operation_code=code, operation_payload=op,
            affected_rows_count=n_rows, affected_cells_count=n_rows,
            affected_columns=[column], before_sample=sample,
            dataset_fingerprint=fingerprint, warnings=warnings,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def _plan_replace_values(self, df, op, plan_id, dataset_id, fingerprint):
        column = op.get('column', '')
        requested_columns = op.get('columns') or op.get('selected_columns') or []
        scope = op.get('scope') or {}
        old_val = op.get('old_value')
        new_val = op.get('new_value')
        from transformation.validator import AGGREGATION_OPERATOR_WORDS
        old_str = str(old_val).strip().lower() if old_val is not None else ''
        was_quoted = op.get('_old_was_quoted', False)
        if old_str in AGGREGATION_OPERATOR_WORDS and not was_quoted:
            raise ValueError(
                f"Cannot use aggregation/comparative term '{old_val}' as a literal value to replace. "
                f"To modify extreme values, specify whether to cap the column or update only the maximal/minimal record."
            )

        # Reconstruct TypedLiteral from _old_typed metadata if available (set by router)
        from datasets.semantics.typed_literal import TypedLiteral, build_mask, parse_literal

        typed_meta = op.get('_old_typed')
        if typed_meta:
            old_lit = TypedLiteral.from_dict(typed_meta)
        else:
            # Infer type from the Python value (already typed by parse_literal in router)
            if old_val is None:
                old_lit = parse_literal('null', was_quoted=False)
            elif isinstance(old_val, bool):
                old_lit = parse_literal(str(old_val).lower(), was_quoted=False)
            elif isinstance(old_val, int):
                old_lit = TypedLiteral(old_val, 'integer', str(old_val), False, False)
            elif isinstance(old_val, float):
                old_lit = TypedLiteral(old_val, 'float', str(old_val), False, False)
            else:
                old_lit = TypedLiteral(str(old_val), 'string', str(old_val), False, False)

        new_typed_meta = op.get('_new_typed')
        if new_typed_meta:
            new_lit = TypedLiteral.from_dict(new_typed_meta)
            new_val = new_lit.python_value
        else:
            if new_val is None:
                new_lit = parse_literal('null', was_quoted=False)
            elif isinstance(new_val, bool):
                new_lit = parse_literal(str(new_val).lower(), was_quoted=False)
            elif isinstance(new_val, int):
                new_lit = TypedLiteral(new_val, 'integer', str(new_val), False, False)
            elif isinstance(new_val, float):
                new_lit = TypedLiteral(new_val, 'float', str(new_val), False, False)
            else:
                new_lit = TypedLiteral(str(new_val), 'string', str(new_val), False, False)
            new_val = new_lit.python_value
        
        if column:
            resolved_col = self._resolve_column_name(df, column)
            if resolved_col and pd.api.types.is_numeric_dtype(df[resolved_col].dtype):
                if old_lit.value_type == 'string' and not was_quoted:
                    raise ValueError(
                        f"Cannot replace string value '{old_val}' in numeric column '{resolved_col}'. "
                        f"If you intended an extreme-value operation, please specify capping or extremum update."
                    )

        op_old_raw = op.get('_old_raw')
        op_old_was_quoted = op.get('_old_was_quoted', False)
        
        if op_old_raw is not None and not op.get('_correction_replanned'):
            from datasets.semantics.value_resolver import resolve_natural_value, ValueAmbiguityClarificationRequired
            
            # Use natural language resolver to decide old_lit
            try:
                old_lit, ambiguous_options = resolve_natural_value(op_old_raw, op_old_was_quoted, df, list(df.columns))
                if ambiguous_options:
                    # Ambiguity found (e.g. both NULL and "Missing" string exist)
                    raise ValueAmbiguityClarificationRequired(
                        f"I found multiple possible meanings for \"{op_old_raw}\". Which one did you mean?",
                        operation_payload=op,
                        ambiguous_options=ambiguous_options
                    )
            except ValueAmbiguityClarificationRequired:
                raise
            except Exception as e:
                # Fallback to existing
                old_lit = TypedLiteral.from_dict(op.get('_old_typed'))
        elif op.get('_old_typed'):
            old_lit = TypedLiteral.from_dict(op.get('_old_typed'))
            
        old_val = old_lit.python_value
        
        op = dict(op)
        op['old_value'] = old_val
        op['_old_typed'] = old_lit.to_dict()
        op['new_value'] = new_val
        op['_new_typed'] = new_lit.to_dict()
        new_typed_meta = op['_new_typed']

        def _type_label(v):
            if v is None: return 'null'
            if isinstance(v, bool): return f'boolean({str(v).lower()})'
            if isinstance(v, int): return f'integer({v})'
            if isinstance(v, float): return f'float({v})'
            return f'string("{v}")'

        def _literal_label(value, metadata):
            if metadata:
                return TypedLiteral.from_dict(metadata).display_label()
            return _type_label(value)

        masks_by_column = {}
        if requested_columns:
            invalid = [c for c in requested_columns if c not in df.columns]
            if invalid:
                raise ValueError(f"Unknown column(s): {', '.join(map(str, invalid))}.")
            matching_cols = list(dict.fromkeys(requested_columns))
        elif scope.get('type') == 'SEMANTIC_COLUMN_SCOPE':
            matching_cols = [c for c in df.columns if _column_matches_semantic_scope(df[c], scope.get('semantic_type'))]
        elif scope.get('type') == 'ALL_COLUMNS':
            matching_cols = list(df.columns)
        elif not column or column not in df.columns:
            # Type-aware column inference: use build_mask for semantic correctness
            matching_cols = []
            for c in df.columns:
                try:
                    candidate_mask = build_mask(df[c], old_lit)
                    masks_by_column[c] = candidate_mask
                    match_count = int(candidate_mask.sum())
                    if logger.isEnabledFor(logging.DEBUG):
                        # Log type/count diagnostics only; never emit cell contents.
                        type_counts = df[c].map(
                            lambda value: type(value).__name__
                        ).value_counts(dropna=False).to_dict()
                        logger.debug(
                            "[VALUE_RESOLVE] dataset=%s column=%s dtype=%s "
                            "literal=%s rows=%d scalar_types=%s matches=%d",
                            dataset_id, c, df[c].dtype, old_lit.display_label(),
                            len(df[c]), type_counts, match_count,
                        )
                    if match_count:
                        matching_cols.append(c)
                except Exception:
                    pass

            if len(matching_cols) == 1 and not op.get('_require_scope_selection'):
                column = matching_cols[0]
            elif len(matching_cols) == 0:
                from transformation.mismatch import find_mismatch_candidate
                candidate_old_lit = find_mismatch_candidate(df, old_lit, list(df.columns))
                if candidate_old_lit:
                    matching_mismatch_cols = [c for c in df.columns if build_mask(df[c], candidate_old_lit).any()]
                    raise ValueTypeMismatchConfirmationRequired(
                        f"I couldn't find any {old_lit.value_type} values matching your request.",
                        requested_value_info=old_lit.to_dict(),
                        candidate_value_info=candidate_old_lit.to_dict(),
                        candidate_columns=matching_mismatch_cols,
                        candidate_count=len(matching_mismatch_cols),
                        operation_payload=op,
                    )

                type_hint = old_lit.display_label()
                return TransformationPlan(
                    plan_id=plan_id, dataset_id=dataset_id,
                    operation_type='REPLACE_VALUES',
                    operation_label=f"Replace {type_hint} with {_literal_label(new_val, new_typed_meta)}",
                    operation_code=f"# Value {old_val!r} ({old_lit.value_type}) not found",
                    operation_payload=op,
                    affected_rows_count=0, affected_cells_count=0,
                    affected_columns=[], before_sample=[],
                    dataset_fingerprint=fingerprint,
                    warnings=[
                        f"Value {old_lit.display_label()} not found in any column. "
                        f"Note: numeric 0 ≠ string '0', boolean false ≠ integer 0."
                    ],
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
            else:
                raise TransformationClarificationRequired(
                    f"Found {old_lit.display_label()} in {len(matching_cols)} columns: "
                    f"{', '.join(matching_cols)}. Please specify which column to change.",
                    missing_slots=['target_scope'], candidate_columns=matching_cols,
                    operation_payload=op,
                )
        else:
            matching_cols = [column]

        from transformation.mismatch import find_mismatch_candidate
        candidate_new_lit = find_mismatch_candidate(df, new_lit, matching_cols)
        if candidate_new_lit:
            # We found that the user's intended replacement value has a strong textual match in the target columns,
            # but they used a different type. Suggest the mismatch.
            raise ValueTypeMismatchConfirmationRequired(
                f"I couldn't find any {new_lit.value_type} values matching your request.",
                requested_value_info=new_lit.to_dict(),
                candidate_value_info=candidate_new_lit.to_dict(),
                candidate_columns=matching_cols,
                candidate_count=len(matching_cols),
                operation_payload=op,
            )

        # Use the same canonical typed matcher for every selected column.
        selected_masks = {}
        samples = []
        total_affected = 0
        for selected_column in matching_cols:
            mask = masks_by_column.get(selected_column)
            if mask is None:
                mask = build_mask(df[selected_column], old_lit)
            selected_masks[selected_column] = mask
            affected = df.loc[mask, [selected_column]]
            count = int(mask.sum())
            if logger.isEnabledFor(logging.DEBUG):
                destination_label = (
                    TypedLiteral.from_dict(new_typed_meta).display_label()
                    if new_typed_meta else _type_label(new_val)
                )
                logger.debug(
                    "[TRANSFORM_MATCH] dataset=%s operation=REPLACE_VALUES column=%s "
                    "dtype=%s source=%s destination=%s rows=%d matches=%d scope=%s",
                    dataset_id, selected_column, df[selected_column].dtype,
                    old_lit.display_label(), destination_label, len(df[selected_column]),
                    count, scope.get('type') or 'EXPLICIT_COLUMNS',
                )
            total_affected += count
            if count:
                samples.extend(self._make_sample(affected, selected_column, new_val))

        old_label = old_lit.display_label()
        new_label = _literal_label(new_val, new_typed_meta)
        if len(matching_cols) == 1:
            label = f"Replace {old_label} with {new_label} in '{matching_cols[0]}'"
        else:
            label = f"Replace {old_label} with {new_label} in {len(matching_cols)} columns"
        code = f"# typed replacement in columns: {', '.join(map(str, matching_cols))}"

        # Carry typed metadata forward so executor/compiler use the same semantics
        payload = dict(op)
        payload['columns'] = list(matching_cols)
        payload['scope'] = scope or {'type': 'EXPLICIT_COLUMNS', 'columns': list(matching_cols)}
        if len(matching_cols) == 1:
            payload['column'] = matching_cols[0]
        else:
            payload.pop('column', None)
        payload['old_value'] = old_val
        payload['new_value'] = new_val
        payload['_old_typed'] = old_lit.to_dict()
        if new_typed_meta:
            payload['_new_typed'] = new_typed_meta

        intelligence_provenance = payload.get('intelligence_provenance')
        
        return TransformationPlan(
            plan_id=plan_id, dataset_id=dataset_id,
            operation_type='REPLACE_VALUES', operation_label=label,
            operation_code=code, operation_payload=payload,
            affected_rows_count=total_affected, affected_cells_count=total_affected,
            affected_columns=list(matching_cols), before_sample=samples[:20],
            dataset_fingerprint=fingerprint,
            created_at=datetime.now(timezone.utc).isoformat(),
            intelligence_provenance=intelligence_provenance,
        )


    def _plan_sort_rows(self, df, op, plan_id, dataset_id, fingerprint):
        column = op.get('column', '')
        ascending = op.get('ascending', True)
        if column not in df.columns:
            raise ValueError(f"Column '{column}' not found.")
        n = len(df)
        direction = 'ascending' if ascending else 'descending'
        label = f"Sort by '{column}' ({direction})"
        code = f"df.sort_values('{column}', ascending={ascending})"
        sample = [AffectedCell(
            row_index=0, source_row_number=1, column=column,
            before='[original order]', after=f'[sorted by {column}]'
        )]
        return TransformationPlan(
            plan_id=plan_id, dataset_id=dataset_id,
            operation_type='SORT_ROWS', operation_label=label,
            operation_code=code, operation_payload=op,
            affected_rows_count=n, affected_cells_count=n,
            affected_columns=[column], before_sample=sample,
            dataset_fingerprint=fingerprint,
            created_at=datetime.now(timezone.utc).isoformat(),
        )


    def _plan_clip(self, df: pd.DataFrame, op: Dict[str, Any], plan_id: str, dataset_id: str, fingerprint: str) -> TransformationPlan:
        raw_col = op.get('column')
        if raw_col is None or str(raw_col).strip().lower() in ('', 'none', 'null'):
            raise ValueError("Target column is required for CLIP operation. Please specify which column to clip.")
        col = self._resolve_column_name(df, raw_col)
        if not col:
            raise ValueError(f"Column '{raw_col}' not found in dataset.")

        num_series = pd.to_numeric(df[col], errors='coerce')
        if num_series.dropna().empty:
            raise ValueError(f"Cannot apply CLIP to non-numeric column '{col}'.")

        min_val = op.get('min')
        max_val = op.get('max')
        if min_val is None and max_val is None:
            raise ValueError(f"CLIP operation on '{col}' requires at least one of 'min' or 'max'.")
        if min_val is not None and max_val is not None and min_val > max_val:
            raise ValueError(f"CLIP 'min' ({min_val}) cannot be greater than 'max' ({max_val}).")

        mask = pd.Series(False, index=df.index)
        if max_val is not None:
            mask |= (num_series > max_val)
        if min_val is not None:
            mask |= (num_series < min_val)

        affected_count = int(mask.sum())
        samples = []
        for idx in df[mask].head(20).index:
            orig_val = df.loc[idx, col]
            num_val = num_series.loc[idx]
            if max_val is not None and num_val > max_val:
                new_val = max_val
            else:
                new_val = min_val
            samples.append(AffectedCell(
                row_index=int(idx),
                source_row_number=int(idx) + 1,
                column=col,
                before=orig_val,
                after=new_val,
            ))

        if min_val is not None and max_val is not None:
            label = f"Clip '{col}' between {min_val} and {max_val}"
            code = f"df['{col}'] = df['{col}'].clip(lower={min_val}, upper={max_val})"
        elif max_val is not None:
            label = f"Cap '{col}' at {max_val}"
            code = f"df['{col}'] = df['{col}'].clip(upper={max_val})"
        else:
            label = f"Floor '{col}' at {min_val}"
            code = f"df['{col}'] = df['{col}'].clip(lower={min_val})"

        payload = dict(op)
        payload['column'] = col
        payload['affected_count'] = affected_count
        expected_ext = {}
        if max_val is not None:
            expected_ext['max'] = max_val
        if min_val is not None:
            expected_ext['min'] = min_val
        payload['expected_extremum'] = expected_ext

        return TransformationPlan(
            plan_id=plan_id,
            dataset_id=dataset_id,
            operation_type='CLIP',
            operation_label=label,
            operation_code=code,
            operation_payload=payload,
            affected_rows_count=affected_count,
            affected_cells_count=affected_count,
            affected_columns=[col],
            before_sample=samples,
            dataset_fingerprint=fingerprint,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def _plan_update_extremum(self, df: pd.DataFrame, op: Dict[str, Any], plan_id: str, dataset_id: str, fingerprint: str) -> TransformationPlan:
        raw_col = op.get('column')
        if raw_col is None or str(raw_col).strip().lower() in ('', 'none', 'null'):
            raise ValueError("Target column is required for UPDATE_EXTREMUM. Please specify which column to update.")
        col = self._resolve_column_name(df, raw_col)
        if not col:
            raise ValueError(f"Column '{raw_col}' not found in dataset.")

        extremum = str(op.get('extremum', 'max')).lower()
        if extremum not in ('max', 'min'):
            raise ValueError(f"Invalid extremum '{extremum}'. Must be 'max' or 'min'.")

        new_val = op.get('new_value')
        if new_val is None:
            raise ValueError("UPDATE_EXTREMUM requires 'new_value'.")

        try:
            import duckdb
            con = duckdb.connect(':memory:')
            con.register('df_view', df)
            agg_fn = 'MAX' if extremum == 'max' else 'MIN'
            curr_ext = con.execute(f'SELECT {agg_fn}("{col}") FROM df_view').fetchone()[0]
            con.close()
            num_series = pd.to_numeric(df[col], errors='coerce')
        except Exception:
            num_series = pd.to_numeric(df[col], errors='coerce')
            if num_series.dropna().empty:
                raise ValueError(f"Cannot update extremum in non-numeric column '{col}'.")
            curr_ext = num_series.max() if extremum == 'max' else num_series.min()
        if pd.isna(curr_ext):
            raise ValueError(f"No numeric values available in column '{col}'.")

        if pd.api.types.is_integer_dtype(num_series.dropna().dtype):
            curr_ext = int(curr_ext)
            if isinstance(new_val, (int, float)) and int(new_val) == new_val:
                new_val = int(new_val)

        mask = (num_series == curr_ext)
        affected_count = int(mask.sum())

        samples = []
        for idx in df[mask].head(20).index:
            samples.append(AffectedCell(
                row_index=int(idx),
                source_row_number=int(idx) + 1,
                column=col,
                before=df.loc[idx, col],
                after=new_val,
            ))

        remaining = num_series[~mask].dropna()
        if not remaining.empty:
            rem_ext = remaining.max() if extremum == 'max' else remaining.min()
            new_ext = max(new_val, rem_ext) if extremum == 'max' else min(new_val, rem_ext)
        else:
            new_ext = new_val

        ext_name = "maximum" if extremum == 'max' else "minimum"
        label = f"Change {ext_name} value ({curr_ext}) to {new_val} in '{col}'"
        code = f"# change {ext_name} in '{col}' to {new_val}\nmask = df['{col}'] == {curr_ext}\ndf.loc[mask, '{col}'] = {new_val}"

        payload = dict(op)
        payload['column'] = col
        payload['extremum'] = extremum
        payload['current_extremum'] = curr_ext
        payload['new_value'] = new_val
        payload['expected_extremum'] = {extremum: new_ext}

        return TransformationPlan(
            plan_id=plan_id,
            dataset_id=dataset_id,
            operation_type='UPDATE_EXTREMUM',
            operation_label=label,
            operation_code=code,
            operation_payload=payload,
            affected_rows_count=affected_count,
            affected_cells_count=affected_count,
            affected_columns=[col],
            before_sample=samples,
            dataset_fingerprint=fingerprint,
            created_at=datetime.now(timezone.utc).isoformat(),
        )


    def _plan_extreme_ambiguity(self, df: pd.DataFrame, op: Dict[str, Any], plan_id: str, dataset_id: str, fingerprint: str) -> TransformationPlan:
        raw_col = op.get('column')
        if raw_col is None or str(raw_col).strip().lower() in ('', 'none', 'null'):
            raise ValueError("Target column is required for extreme value operation. Please specify which column to change.")
        col = self._resolve_column_name(df, raw_col)
        if not col:
            raise ValueError(f"Column '{raw_col}' not found in dataset.")

        extremum = str(op.get('extremum', 'max')).lower()
        target_val = op.get('target_value')
        if target_val is None:
            raise ValueError("Target value is required for extreme value operation.")

        try:
            import duckdb
            con = duckdb.connect(':memory:')
            con.register('df_view', df)
            agg_fn = 'MAX' if extremum == 'max' else 'MIN'
            curr_ext = con.execute(f'SELECT {agg_fn}("{col}") FROM df_view').fetchone()[0]
            con.close()
            num_series = pd.to_numeric(df[col], errors='coerce').dropna()
        except Exception:
            num_series = pd.to_numeric(df[col], errors='coerce').dropna()
            if num_series.empty:
                raise ValueError(f"Column '{col}' contains no numeric values.")
            curr_ext = num_series.max() if extremum == 'max' else num_series.min()
        if pd.api.types.is_integer_dtype(num_series.dtype):
            curr_ext = int(curr_ext)
            if isinstance(target_val, (int, float)) and int(target_val) == target_val:
                target_val = int(target_val)

        ext_word = "maximum" if extremum == 'max' else "minimum"
        if extremum == 'max':
            opt1 = {
                'action': 'CAP_UPPER',
                'label': f"Cap all values above {target_val} at {target_val}",
                'description': f"Values > {target_val} will be capped at {target_val}; lower values remain unchanged.",
                'operation': {'type': 'CLIP', 'column': col, 'max': target_val}
            }
            opt2 = {
                'action': 'UPDATE_EXTREMUM',
                'label': f"Change only the maximum record(s) ({curr_ext}) to {target_val}",
                'description': f"Only row(s) where {col} is currently {curr_ext} will be changed to {target_val}.",
                'operation': {'type': 'UPDATE_EXTREMUM', 'column': col, 'extremum': 'max', 'new_value': target_val}
            }
            question = f"Do you want to cap all values above {target_val} at {target_val}, or change only the record(s) with the current maximum ({curr_ext}) to {target_val}?"
        else:
            opt1 = {
                'action': 'FLOOR_LOWER',
                'label': f"Floor all values below {target_val} at {target_val}",
                'description': f"Values < {target_val} will be floored at {target_val}; higher values remain unchanged.",
                'operation': {'type': 'CLIP', 'column': col, 'min': target_val}
            }
            opt2 = {
                'action': 'UPDATE_EXTREMUM',
                'label': f"Change only the minimum record(s) ({curr_ext}) to {target_val}",
                'description': f"Only row(s) where {col} is currently {curr_ext} will be changed to {target_val}.",
                'operation': {'type': 'UPDATE_EXTREMUM', 'column': col, 'extremum': 'min', 'new_value': target_val}
            }
            question = f"Do you want to floor all values below {target_val} at {target_val}, or change only the record(s) with the current minimum ({curr_ext}) to {target_val}?"

        raise TransformationClarificationRequired(
            question,
            missing_slots=['clarify_extreme_intent'],
            candidate_columns=[col],
            clarification_type='EXTREME_VALUE_AMBIGUITY',
            operation_payload=op,
            ambiguous_options=[opt1, opt2]
        )


transformation_planner = TransformationPlanner()
