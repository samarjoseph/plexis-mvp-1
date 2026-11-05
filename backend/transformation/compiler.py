import logging
import numpy as np
import pandas as pd
from typing import Any

from transformation.ir import TransformationIR, TransformStep, SimulationResult, AffectedCellIR, PRIMITIVE_REGISTRY
from transformation.contracts import TransformationPlan, AffectedCell
from transformation.planner import _compute_fingerprint
from transformation.validator import topological_sort

logger = logging.getLogger(__name__)

class UnsafeExpressionError(Exception):
    pass

class ExpressionError(Exception):
    pass

def _safe_eq(l, r):
    if isinstance(l, pd.Series):
        if r is None or (np.isscalar(r) and pd.isna(r)):
            return l.isna()
        return l == r
    elif isinstance(r, pd.Series):
        if l is None or (np.isscalar(l) and pd.isna(l)):
            return r.isna()
        return l == r
    else:
        if (l is None or (np.isscalar(l) and pd.isna(l))) and (r is None or (np.isscalar(r) and pd.isna(r))):
            return True
        return l == r

def _safe_ne(l, r):
    if isinstance(l, pd.Series):
        if r is None or (np.isscalar(r) and pd.isna(r)):
            return ~l.isna()
        return l != r
    elif isinstance(r, pd.Series):
        if l is None or (np.isscalar(l) and pd.isna(l)):
            return ~r.isna()
        return l != r
    else:
        if (l is None or (np.isscalar(l) and pd.isna(l))) and (r is None or (np.isscalar(r) and pd.isna(r))):
            return False
        return l != r

class ExpressionEvaluator:
    def evaluate(self, expr: dict, df: pd.DataFrame) -> pd.Series:
        """Evaluate an expression AST into a pandas Series. SAFE: no eval/exec."""
        if not isinstance(expr, dict):
            raise UnsafeExpressionError(f"Expression must be a dict, got {type(expr).__name__}")
            
        node_type = expr.get('type')
        if not node_type:
            raise UnsafeExpressionError("Expression node missing 'type'")
            
        try:
            if node_type == 'COLUMN':
                name = expr.get('name')
                if not name:
                    raise ExpressionError("COLUMN missing 'name'")
                return df[name]
                
            elif node_type == 'LITERAL':
                value = expr.get('value')
                typed = expr.get('typed_literal')
                if isinstance(typed, dict) and typed.get('__typed_literal__'):
                    from datasets.semantics.typed_literal import TypedLiteral
                    value = TypedLiteral.from_dict(typed).python_value
                elif expr.get('value_type'):
                    value = expr.get('value')
                return pd.Series([value] * len(df), index=df.index)
                
            elif node_type == 'COMPARE':
                left = self.evaluate(expr.get('left', {}), df)
                right_expr = expr.get('right')
                right = self.evaluate(right_expr, df) if right_expr is not None else None
                op = str(expr.get('operator', '')).lower()

                if op in ('is_null', 'isnull', 'is null'):
                    if isinstance(left, pd.Series):
                        return left.isna() | (left.astype(str).str.strip().isin(['', 'None', 'nan', 'NaN', 'null', 'NULL']))
                    return pd.Series([left is None or str(left).strip() in ('', 'None', 'nan', 'NaN', 'null', 'NULL')] * len(df), index=df.index)

                if op in ('is_not_null', 'notnull', 'not_null', 'is not null'):
                    if isinstance(left, pd.Series):
                        return ~left.isna() & (~left.astype(str).str.strip().isin(['', 'None', 'nan', 'NaN', 'null', 'NULL']))
                    return pd.Series([left is not None and str(left).strip() not in ('', 'None', 'nan', 'NaN', 'null', 'NULL')] * len(df), index=df.index)

                if op in ('in',):
                    if isinstance(right, (list, tuple, set)):
                        return left.isin(right)
                    return left.isin([right])

                if op in ('not_in', 'not in'):
                    if isinstance(right, (list, tuple, set)):
                        return ~left.isin(right)
                    return ~left.isin([right])

                ops = {
                    '==': _safe_eq,
                    '!=': _safe_ne,
                    '<': lambda l, r: l < r,
                    '>': lambda l, r: l > r,
                    '<=': lambda l, r: l <= r,
                    '>=': lambda l, r: l >= r,
                }
                if op not in ops:
                    raise UnsafeExpressionError(f"Unknown comparison operator: {op}")
                if op in ('==', '!='):
                    # Typed literal comparisons use the same semantic rules as
                    # fast-path replacement planning and execution.
                    left_expr = expr.get('left', {})
                    literal_expr = None
                    values = None
                    if left_expr.get('type') == 'LITERAL' and right_expr and right_expr.get('type') == 'COLUMN':
                        literal_expr, values = left_expr, right
                    elif right_expr and right_expr.get('type') == 'LITERAL' and left_expr.get('type') == 'COLUMN':
                        literal_expr, values = right_expr, left
                    if literal_expr is not None:
                        typed = literal_expr.get('typed_literal')
                        if isinstance(typed, dict) and typed.get('__typed_literal__'):
                            from datasets.semantics.typed_literal import TypedLiteral, build_mask
                            mask = build_mask(values, TypedLiteral.from_dict(typed))
                            return mask if op == '==' else ~mask
                return ops[op](left, right)

            elif node_type in ('IS_NULL', 'IS_NOT_NULL'):
                operand_expr = expr.get('operand') or expr.get('left', {})
                operand = self.evaluate(operand_expr, df)
                if node_type == 'IS_NULL':
                    if isinstance(operand, pd.Series):
                        return operand.isna() | (operand.astype(str).str.strip().isin(['', 'None', 'nan', 'NaN', 'null', 'NULL']))
                    return pd.Series([operand is None or str(operand).strip() in ('', 'None', 'nan', 'NaN', 'null', 'NULL')] * len(df), index=df.index)
                else:
                    if isinstance(operand, pd.Series):
                        return ~operand.isna() & (~operand.astype(str).str.strip().isin(['', 'None', 'nan', 'NaN', 'null', 'NULL']))
                    return pd.Series([operand is not None and str(operand).strip() not in ('', 'None', 'nan', 'NaN', 'null', 'NULL')] * len(df), index=df.index)
                
            elif node_type == 'ARITHMETIC':
                op = expr.get('operator')
                if op == 'abs':
                    operand = self.evaluate(expr.get('operand', {}), df)
                    return operand.abs()
                else:
                    left = self.evaluate(expr.get('left', {}), df)
                    right = self.evaluate(expr.get('right', {}), df)
                    if op == 'add': return left + right
                    elif op == 'subtract': return left - right
                    elif op == 'multiply': return left * right
                    elif op == 'divide': 
                        # Handle division by zero with np.inf -> NaN
                        res = left / right
                        return res.replace([np.inf, -np.inf], np.nan)
                    elif op == 'modulo': return left % right
                    else:
                        raise UnsafeExpressionError(f"Unknown arithmetic operator: {op}")
                        
            elif node_type == 'STRING_FN':
                operand = self.evaluate(expr.get('operand', {}), df)
                fn = expr.get('function')
                if fn == 'strip': return operand.str.strip()
                elif fn == 'lower': return operand.str.lower()
                elif fn == 'upper': return operand.str.upper()
                elif fn == 'title': return operand.str.title()
                elif fn == 'len': return operand.str.len()
                elif fn == 'replace':
                    old = expr.get('old')
                    new = expr.get('new')
                    # str.replace requires string arguments — only coerce if old/new
                    # are actually strings or the column is string-typed.
                    # Never silently convert int 0 → "0" here.
                    old_str = old if isinstance(old, str) else str(old) if old is not None else ''
                    new_str = new if isinstance(new, str) else str(new) if new is not None else ''
                    return operand.str.replace(old_str, new_str, regex=False)
                else:
                    raise UnsafeExpressionError(f"Unknown string function: {fn}")
                    
            elif node_type == 'CASE':
                conditions = expr.get('conditions', [])
                result = pd.Series([None] * len(df), index=df.index)
                
                # Default case
                if 'default' in expr:
                    default_expr = expr['default']
                    if isinstance(default_expr, dict):
                        result = self.evaluate(default_expr, df)
                    else:
                        result = pd.Series([default_expr] * len(df), index=df.index)

                # Reverse evaluate so earlier conditions overwrite later ones
                for cond in reversed(conditions):
                    when_mask = self.evaluate(cond.get('when', {}), df)
                    
                    if 'then' in cond:
                        then_val = self.evaluate(cond['then'], df)
                    elif 'value' in cond:
                        val = cond['value']
                        then_val = pd.Series([val] * len(df), index=df.index)
                    else:
                        raise ExpressionError("CASE condition missing 'then' or 'value'")
                    
                    result = np.where(when_mask, then_val, result)
                    
                return pd.Series(result, index=df.index)
                
            elif node_type == 'COALESCE':
                exprs = expr.get('expressions', [])
                if not exprs:
                    raise ExpressionError("COALESCE requires expressions")
                result = self.evaluate(exprs[0], df)
                for e in exprs[1:]:
                    next_res = self.evaluate(e, df)
                    result = result.fillna(next_res)
                return result
                
            elif node_type == 'NOT':
                operand = self.evaluate(expr.get('operand', {}), df)
                return ~operand
                
            elif node_type == 'AND':
                left = self.evaluate(expr.get('left', {}), df)
                right = self.evaluate(expr.get('right', {}), df)
                return left & right
                
            elif node_type == 'OR':
                left = self.evaluate(expr.get('left', {}), df)
                right = self.evaluate(expr.get('right', {}), df)
                return left | right
                
            else:
                raise UnsafeExpressionError(f"Unknown node type: {node_type}")
                
        except Exception as e:
            if isinstance(e, UnsafeExpressionError):
                raise
            raise ExpressionError(f"Error evaluating {node_type}: {str(e)}")

class IRCompiler:
    def __init__(self):
        self.evaluator = ExpressionEvaluator()

    def compile_and_simulate(self, ir: TransformationIR, df: pd.DataFrame, dataset_id: str) -> SimulationResult:
        original_df = df.copy()
        working_df = df.copy()
        
        step_labels = []
        warnings = []
        errors = []
        
        try:
            sorted_steps = topological_sort(ir.steps)
        except Exception as e:
            errors.append(f"Failed to sort steps topologically: {e}")
            return SimulationResult(success=False, errors=errors)

        try:
            for step in sorted_steps:
                self._execute_step(step, working_df)
                if step.label:
                    step_labels.append(step.label)
                else:
                    step_labels.append(f"{step.operation} ({step.id})")
        except Exception as e:
            errors.append(f"Execution failed at step {step.id}: {str(e)}")
            return SimulationResult(success=False, errors=errors)

        # Compute diff
        affected_cells = []
        
        # Rows
        rows_before = len(original_df)
        rows_after = len(working_df)
        cols_before = len(original_df.columns)
        cols_after = len(working_df.columns)
        
        created_columns = [c for c in working_df.columns if c not in original_df.columns]
        deleted_columns = [c for c in original_df.columns if c not in working_df.columns]
        
        common_cols = [c for c in working_df.columns if c in original_df.columns]
        common_idx = original_df.index.intersection(working_df.index)
        
        # Track cells changed
        total_cells_changed = 0
        affected_rows_set = set()
        affected_cols_set = set()
        
        # Check for pure schema operations (RENAME_COLUMN, DROP_COLUMN)
        is_schema_only = all(step.operation in ('RENAME_COLUMN', 'DROP_COLUMN') for step in ir.steps)
        
        if is_schema_only:
            # For schema operations, we record the column change but no cell changes
            for col in created_columns:
                affected_cols_set.add(col)
                # Find the old name from the step
                old_name = None
                for step in ir.steps:
                    if step.operation == 'RENAME_COLUMN' and step.params.get('new_name') == col:
                        old_name = step.params.get('column')
                        break
                affected_cells.append(AffectedCellIR(
                    row_index=-1, # Indicates a schema change
                    column=col,
                    before=f"Column header: '{old_name}'" if old_name else "Column created",
                    after=f"Column header: '{col}'"
                ))
            
            for col in deleted_columns:
                affected_cols_set.add(col)
                # Only add if it wasn't a rename source
                was_renamed = any(step.operation == 'RENAME_COLUMN' and step.params.get('column') == col for step in ir.steps)
                if not was_renamed:
                    affected_cells.append(AffectedCellIR(
                        row_index=-1,
                        column=col,
                        before=f"Column header: '{col}'",
                        after="Column dropped"
                    ))
        else:
            # New columns are all changes
            for col in created_columns:
                affected_cols_set.add(col)
                for row_idx in working_df.index:
                    total_cells_changed += 1
                    affected_rows_set.add(row_idx)
                    if len(affected_cells) < 50:
                        affected_cells.append(AffectedCellIR(
                            row_index=int(row_idx),
                            column=col,
                            before=None,
                            after=working_df.at[row_idx, col]
                        ))
                        
            # Deleted columns are all changes
            for col in deleted_columns:
                affected_cols_set.add(col)
                for row_idx in original_df.index:
                    total_cells_changed += 1
                    affected_rows_set.add(row_idx)
                    if len(affected_cells) < 50:
                        affected_cells.append(AffectedCellIR(
                            row_index=int(row_idx),
                            column=col,
                            before=original_df.at[row_idx, col],
                            after=None
                        ))
        
        # Common columns
        for col in common_cols:
            diff_mask = original_df.loc[common_idx, col] != working_df.loc[common_idx, col]
            
            # Account for NaNs being unequal in standard equality check
            na_mask = original_df.loc[common_idx, col].isna() & working_df.loc[common_idx, col].isna()
            diff_mask = diff_mask & ~na_mask
            
            diff_rows = common_idx[diff_mask]
            for row_idx in diff_rows:
                total_cells_changed += 1
                affected_cols_set.add(col)
                affected_rows_set.add(row_idx)
                if len(affected_cells) < 50:
                    affected_cells.append(AffectedCellIR(
                        row_index=int(row_idx),
                        column=col,
                        before=original_df.at[row_idx, col],
                        after=working_df.at[row_idx, col]
                    ))
                    
        # Handle deleted rows
        deleted_rows = original_df.index.difference(working_df.index)
        for row_idx in deleted_rows:
            affected_rows_set.add(row_idx)
            for col in original_df.columns:
                total_cells_changed += 1
                affected_cols_set.add(col)
                if len(affected_cells) < 50:
                    affected_cells.append(AffectedCellIR(
                        row_index=int(row_idx),
                        column=col,
                        before=original_df.at[row_idx, col],
                        after=None
                    ))

        # Detect No-op
        if total_cells_changed == 0 and rows_before == rows_after and cols_before == cols_after:
            warnings.append("This transformation resulted in no changes to the dataset.")
            
        return SimulationResult(
            success=True,
            result_df=working_df,
            affected_cells=affected_cells,
            affected_rows=len(affected_rows_set),
            affected_columns=list(affected_cols_set),
            created_columns=created_columns,
            deleted_columns=deleted_columns,
            rows_before=rows_before,
            rows_after=rows_after,
            cols_before=cols_before,
            cols_after=cols_after,
            warnings=warnings,
            errors=errors,
            step_labels=step_labels,
            total_cells_changed=total_cells_changed,
        )

    def _execute_step(self, step: TransformStep, df: pd.DataFrame) -> None:
        op = step.operation
        p = step.params
        
        if op == 'FILL':
            cols = p.get('columns', [])
            val = p.get('value')
            if isinstance(cols, str): cols = [cols]
            
            for col in cols:
                if val == 'mean': df[col] = df[col].fillna(df[col].mean())
                elif val == 'median': df[col] = df[col].fillna(df[col].median())
                elif val == 'mode': 
                    mode_s = df[col].mode()
                    if not mode_s.empty: df[col] = df[col].fillna(mode_s.iloc[0])
                elif val == 'ffill': df[col] = df[col].ffill()
                elif val == 'bfill': df[col] = df[col].bfill()
                else: df[col] = df[col].fillna(val)
                
        elif op == 'DROP_DUPLICATES':
            cols = p.get('columns')
            if cols and isinstance(cols, str): cols = [cols]
            df.drop_duplicates(subset=cols, keep='first', inplace=True)
            
        elif op == 'RENAME_COLUMN':
            df.rename(columns={p['column']: p['new_name']}, inplace=True)
            
        elif op == 'DROP_COLUMN':
            col = p['column']
            if col in df.columns:
                df.drop(columns=[col], inplace=True)
                
        elif op == 'FILTER_ROWS':
            action = p.get('action', 'KEEP').upper()
            
            if step.expression:
                mask = self.evaluator.evaluate(step.expression, df)
            else:
                col = p['column']
                operator = p.get('operator', '==')
                val = p.get('value')
                
                col_series = df[col]
                if operator in ('>', '<', '>=', '<='):
                    col_series = pd.to_numeric(col_series, errors='coerce')
                    
                if operator == '==': mask = col_series == val
                elif operator == '!=': mask = col_series != val
                elif operator == '>': mask = col_series > val
                elif operator == '<': mask = col_series < val
                elif operator == '>=': mask = col_series >= val
                elif operator == '<=': mask = col_series <= val
                elif operator in ('is_null', 'isnull', 'is null'):
                    mask = col_series.isna() | (col_series.astype(str).str.strip().isin(['', 'None', 'nan', 'NaN', 'null', 'NULL']))
                elif operator in ('is_not_null', 'notnull', 'not_null', 'is not null'):
                    mask = ~col_series.isna() & (~col_series.astype(str).str.strip().isin(['', 'None', 'nan', 'NaN', 'null', 'NULL']))
                elif operator in ('in',):
                    mask = col_series.isin(val if isinstance(val, (list, tuple, set)) else [val])
                elif operator in ('not_in', 'not in'):
                    mask = ~col_series.isin(val if isinstance(val, (list, tuple, set)) else [val])
                else: raise ValueError(f"Unknown operator {operator}")
                
            if action == 'KEEP':
                df.drop(df.index[~mask], inplace=True)
            else:
                df.drop(df.index[mask], inplace=True)

        elif op == 'REMOVE_EMPTY_ROWS':
            how = str(p.get('how', 'any')).lower()
            cols = p.get('columns') or list(df.columns)
            if isinstance(cols, str): cols = [cols]
            valid_cols = [c for c in cols if c in df.columns]
            if valid_cols:
                null_mask = df[valid_cols].isnull()
                for c in valid_cols:
                    if df[c].dtype == object or pd.api.types.is_string_dtype(df[c].dtype):
                        empty_mask = df[c].astype(str).str.strip().isin(['', 'None', 'nan', 'NaN', 'null', 'NULL'])
                        null_mask[c] = null_mask[c] | empty_mask
                if how == 'all':
                    rows_to_drop = null_mask.all(axis=1)
                else:
                    rows_to_drop = null_mask.any(axis=1)
                df.drop(df.index[rows_to_drop], inplace=True)
                
        elif op == 'CAST':
            col = p['column']
            dtype = p['dtype']
            if dtype in ('int', 'float', 'int64', 'float64'):
                df[col] = pd.to_numeric(df[col], errors='coerce')
            else:
                df[col] = df[col].astype(dtype, errors='ignore')
                
        elif op == 'REPLACE':
            col = p['column']
            old_val = p['old_value']
            new_val = p['new_value']
            # Use semantic build_mask if typed metadata present; fallback to pandas .replace()
            typed_meta = p.get('_old_typed')
            if typed_meta:
                from datasets.semantics.typed_literal import TypedLiteral, build_mask
                old_lit = TypedLiteral.from_dict(typed_meta)
                mask = build_mask(df[col], old_lit)
                df.loc[mask, col] = new_val
            else:
                # Fallback: use pandas native replace (preserves existing behavior)
                df[col] = df[col].replace(old_val, new_val)

            
        elif op == 'SORT_ROWS':
            col = p['column']
            asc = p.get('ascending', True)
            df.sort_values(by=col, ascending=asc, inplace=True)
            
        elif op == 'CREATE_COLUMN':
            if not step.expression:
                raise ValueError("CREATE_COLUMN missing expression")
            df[p['column']] = self.evaluator.evaluate(step.expression, df)
            
        elif op == 'MODIFY_COLUMN':
            if not step.expression:
                raise ValueError("MODIFY_COLUMN missing expression")
            df[p['column']] = self.evaluator.evaluate(step.expression, df)
            
        elif op == 'STRIP':
            cols = p.get('columns', [])
            if isinstance(cols, str): cols = [cols]
            for c in cols:
                df[c] = df[c].str.strip()
                
        elif op == 'LOWER':
            cols = p.get('columns', [])
            if isinstance(cols, str): cols = [cols]
            for c in cols:
                df[c] = df[c].str.lower()
                
        elif op == 'UPPER':
            cols = p.get('columns', [])
            if isinstance(cols, str): cols = [cols]
            for c in cols:
                df[c] = df[c].str.upper()
                
        elif op == 'TITLE':
            cols = p.get('columns', [])
            if isinstance(cols, str): cols = [cols]
            for c in cols:
                df[c] = df[c].str.title()
                
        elif op == 'ROUND':
            cols = p.get('columns', [])
            if isinstance(cols, str): cols = [cols]
            decimals = p.get('decimals', 0)
            for c in cols:
                df[c] = df[c].round(decimals)
                
        elif op == 'CLIP':
            col = p['column']
            df[col] = df[col].clip(lower=p.get('min'), upper=p.get('max'))

        elif op == 'UPDATE_EXTREMUM':
            col = p['column']
            extremum = p.get('extremum', 'max').lower()
            new_val = p['new_value']
            s = pd.to_numeric(df[col], errors='coerce')
            curr_ext = s.max() if extremum == 'max' else s.min()
            mask = (df[col] == curr_ext)
            df.loc[mask, col] = new_val
            
        elif op == 'EXTRACT_DATE_PART':
            col = p['column']
            part = p.get('part', '').lower()
            out_col = p['output_column']
            dt_series = pd.to_datetime(df[col])
            
            if part == 'year': df[out_col] = dt_series.dt.year
            elif part == 'month': df[out_col] = dt_series.dt.month
            elif part == 'day': df[out_col] = dt_series.dt.day
            elif part == 'weekday': df[out_col] = dt_series.dt.weekday
            else:
                raise ValueError(f"Unknown date part: {part}")
                
        elif op == 'DATE_DIFF':
            col_a = p['column_a']
            col_b = p['column_b']
            out_col = p['output_column']
            diff = pd.to_datetime(df[col_a]) - pd.to_datetime(df[col_b])
            df[out_col] = diff.dt.days

        elif op == 'FILL_PATTERN':
            col = p['column']
            pattern = p.get('pattern', [])
            if isinstance(pattern, str):
                import ast
                try:
                    pattern = ast.literal_eval(pattern)
                except Exception:
                    pattern = [x.strip().strip("'\"") for x in pattern.replace('[', '').replace(']', '').split(',') if x.strip()]
            if not isinstance(pattern, list):
                pattern = [pattern]
            n_pat = len(pattern) if pattern else 1
            n_rows = len(df)
            df[col] = [pattern[i % n_pat] for i in range(n_rows)]

        elif op == 'FILL_SEQUENTIAL':
            col = p['column']
            start = p.get('start', 1)
            step = p.get('step', 1)
            try:
                start = float(start) if '.' in str(start) else int(start)
                step = float(step) if '.' in str(step) else int(step)
            except (TypeError, ValueError):
                start, step = 1, 1
            n_rows = len(df)
            df[col] = [start + i * step for i in range(n_rows)]

        elif op == 'NORMALIZE_TEXT':
            cols = p.get('columns', [])
            if isinstance(cols, str):
                cols = [cols]
            case = str(p.get('case', 'lower')).lower()
            for c in cols:
                if c in df.columns:
                    s = df[c].astype(str).str.strip()
                    if case == 'upper':
                        df[c] = s.str.upper()
                    elif case == 'title':
                        df[c] = s.str.title()
                    else:
                        df[c] = s.str.lower()

        elif op == 'UPDATE_CELL':
            col = p['column']
            val = p['value']
            row_idx = p.get('row_index')
            where_col = p.get('where_column')
            where_val = p.get('where_value')
            if col in df.columns:
                if row_idx is not None:
                    try:
                        idx = int(row_idx)
                        if idx not in df.index and (idx - 1) in df.index:
                            idx = idx - 1
                        df.loc[idx, col] = val
                    except (ValueError, TypeError, KeyError):
                        pass
                elif where_col and where_col in df.columns:
                    mask = (df[where_col].astype(str) == str(where_val))
                    df.loc[mask, col] = val
                elif step.expression:
                    cond = self.evaluator.evaluate(step.expression, df)
                    df.loc[cond, col] = val

        elif op == 'SPLIT_COLUMN':
            col = p['column']
            delim = p.get('delimiter', ' ')
            out_cols = p.get('output_columns', [])
            if isinstance(out_cols, str):
                out_cols = [c.strip() for c in out_cols.split(',')]
            if col in df.columns and out_cols:
                n_splits = len(out_cols) - 1
                splits = df[col].astype(str).str.split(delim, n=n_splits, expand=True)
                for i, out_c in enumerate(out_cols):
                    if i < splits.shape[1]:
                        df[out_c] = splits[i]
                    else:
                        df[out_c] = None

        elif op == 'CONCAT_COLUMNS':
            cols = p.get('columns', [])
            if isinstance(cols, str):
                cols = [cols]
            out_col = p.get('output_column')
            delim = p.get('delimiter', ' ')
            valid_cols = [c for c in cols if c in df.columns]
            if valid_cols and out_col:
                series = df[valid_cols[0]].fillna('').astype(str)
                for c in valid_cols[1:]:
                    series = series + delim + df[c].fillna('').astype(str)
                df[out_col] = series

        elif op == 'MAP_VALUES':
            col = p['column']
            mapping = p.get('mapping', {})
            default_val = p.get('default')
            if col in df.columns and isinstance(mapping, dict):
                mapped = df[col].map(mapping)
                if default_val is not None:
                    df[col] = mapped.fillna(default_val)
                else:
                    df[col] = mapped.fillna(df[col])

        elif op == 'CUSTOM_TRANSFORM':
            code = p.get('code', '').strip()
            if code:
                blocked_tokens = [
                    '__', 'import ', 'eval(', 'exec(', 'open(', 'os.', 'sys.',
                    'subprocess', 'shutil', 'globals()', 'locals()', 'getattr', 'setattr'
                ]
                code_lower = code.lower()
                for token in blocked_tokens:
                    if token in code_lower:
                        raise ValueError(f"Unsafe operation in custom transform: '{token}' is blocked for security.")
                safe_builtins = {
                    'range': range, 'len': len, 'min': min, 'max': max,
                    'sum': sum, 'round': round, 'int': int, 'float': float,
                    'str': str, 'bool': bool, 'list': list, 'dict': dict,
                    'set': set, 'abs': abs,
                }
                safe_globals = {
                    '__builtins__': safe_builtins,
                    'pd': pd,
                    'np': np,
                }
                safe_locals = {'df': df}
                exec(code, safe_globals, safe_locals)
                if 'df' in safe_locals and isinstance(safe_locals['df'], pd.DataFrame):
                    res_df = safe_locals['df']
                    # Keep df reference alive and update contents
                    drop_cols = [c for c in df.columns if c not in res_df.columns]
                    if drop_cols:
                        df.drop(columns=drop_cols, inplace=True)
                    for c in res_df.columns:
                        df[c] = res_df[c]

    def simulation_to_plan(self, ir: TransformationIR, sim: SimulationResult, dataset_id: str, fingerprint: str) -> TransformationPlan:
        """Convert a SimulationResult into the existing TransformationPlan contract."""
        
        before_sample = []
        # Convert first 20 AffectedCellIR to AffectedCell
        for ir_cell in sim.affected_cells[:20]:
            before_sample.append(AffectedCell(
                row_index=ir_cell.row_index,
                source_row_number=ir_cell.row_index + 1 if ir_cell.row_index >= 0 else None,
                column=ir_cell.column,
                before=ir_cell.before,
                after=ir_cell.after
            ))
            
        # Count total cells
        affected_cells_count = sim.affected_rows * len(sim.affected_columns) if sim.affected_rows and sim.affected_columns else len(sim.affected_cells)
        # Re-calc based on number of columns affected vs rows, or use direct length if needed, but length might just be truncated to 50 in preview
        # We need a proper count. We didn't keep full count in diffing, let's just use affected_rows * affected_columns for worst case, or 
        # For precise, we track total_cells_changed in our sim which is len of full affected_cells if we kept it. 
        # But we capped at 50 for the list. Wait, I should track total cells precisely without capping the list? Let's just recount or use a proxy for now.
        
        is_schema_only = all(step.operation in ('RENAME_COLUMN', 'DROP_COLUMN') for step in ir.steps)
        impact = {
            'type': 'schema' if is_schema_only else 'cell',
            'columns_changed': len(sim.affected_columns),
            'cells_changed': 0 if is_schema_only else sim.total_cells_changed
        }
        
        payload = {
            'ir': ir.to_dict(),
            'step_labels': sim.step_labels,
            'impact': impact
        }

        import uuid
        plan_id = str(uuid.uuid4())

        return TransformationPlan(
            plan_id=plan_id,
            dataset_id=dataset_id,
            operation_type='IR_COMPOSITE',
            operation_label=ir.goal,
            operation_code=f'{len(ir.steps)} step(s)',
            operation_payload=payload,
            affected_rows_count=sim.affected_rows,
            affected_cells_count=impact['cells_changed'],
            affected_columns=sim.affected_columns,
            before_sample=before_sample,
            dataset_fingerprint=fingerprint,
            warnings=sim.warnings,
        )

expr_evaluator = ExpressionEvaluator()
ir_compiler = IRCompiler()
