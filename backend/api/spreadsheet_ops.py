"""
Spreadsheet Operations API blueprint.

Provides deterministic analytical operations on datasets.
The LLM decides WHAT to do; this module executes HOW.

Routes:
  POST /api/spreadsheet/operate   — execute a deterministic operation
  POST /api/spreadsheet/interpret — interpret a natural language query into a structured operation

IMPORTANT:
  - These endpoints NEVER mutate the original dataset.
  - All operations are read-only views of the dataset.
  - The LLM in /interpret produces a structured operation dict; this endpoint validates + routes it.
"""
import logging
import json
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)

spreadsheet_bp = Blueprint('spreadsheet', __name__, url_prefix='/api/spreadsheet')


@spreadsheet_bp.before_request
def check_guest_spreadsheet_access():
    if request.method == "OPTIONS":
        return None
    from db.identity import identity_resolver
    try:
        user = identity_resolver.resolve(request)
        if user and user.is_guest:
            return jsonify({
                "error": "GUEST_RESTRICTED",
                "code": "GUEST_SPREADSHEET_LOCKED",
                "message": "The interactive spreadsheet panel is unavailable in Guest Mode. Please sign in to explore and edit datasets directly in the spreadsheet view."
            }), 403
    except Exception:
        pass
    return None


# ─── Dataset store helper ────────────────────────────────────────────────────

def _get_dataframe(dataset_id: str):
    """Retrieve the dataset dataframe from the registry (same pattern as datasets_data.py)."""
    try:
        from datasets.registry import dataset_registry
        entry = dataset_registry.get_by_id(dataset_id)
        if entry is None:
            return None, f"Dataset '{dataset_id}' not found."
        df = entry.dataframe
        if df is None or df.empty:
            return None, f"Dataset '{dataset_id}' dataframe not available."
        return df, None
    except Exception as e:
        logger.error(f"_get_dataframe error for {dataset_id}: {e}", exc_info=True)
        return None, str(e)


# ─── /api/spreadsheet/operate ────────────────────────────────────────────────

@spreadsheet_bp.route('/operate', methods=['POST'])
def operate():
    """
    Execute a deterministic operation on a dataset.

    Request body:
      dataset_id  (str, required)
      operation   (str, required) — operation type
      + operation-specific params

    Supported operations:
      TOP_N          { column, n, order }
      BOTTOM_N       { column, n }
      FIND_DUPLICATES{ columns }
      FIND_MISSING   { columns }
      FIND_OUTLIERS  { column, method }
      AGGREGATE      { column, func, group_by }

    Response:
      { operation, row_indices, result_count, summary, column_stats, success }
    """
    data = request.get_json(silent=True) or {}
    dataset_id = data.get('dataset_id', '').strip()
    operation = (data.get('operation') or '').upper()

    if not dataset_id:
        return jsonify({'success': False, 'error': 'dataset_id is required'}), 400
    if not operation:
        return jsonify({'success': False, 'error': 'operation is required'}), 400

    df, err = _get_dataframe(dataset_id)
    if err:
        return jsonify({'success': False, 'error': err}), 404

    try:
        result = _execute_operation(df, operation, data)
        return jsonify({'success': True, **result})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logger.error(f"Spreadsheet operate error [{operation}]: {e}", exc_info=True)
        return jsonify({'success': False, 'error': f'Operation failed: {str(e)}'}), 500


def _execute_operation(df, operation: str, params: dict) -> dict:
    """Route operation to the correct handler."""
    handlers = {
        'TOP_N':            _op_top_n,
        'BOTTOM_N':         _op_bottom_n,
        'FIND_DUPLICATES':  _op_find_duplicates,
        'FIND_MISSING':     _op_find_missing,
        'FIND_OUTLIERS':    _op_find_outliers,
        'AGGREGATE':        _op_aggregate,
    }
    handler = handlers.get(operation)
    if not handler:
        raise ValueError(f"Unsupported operation: '{operation}'")
    return handler(df, params)


def _op_top_n(df, params):
    import pandas as pd
    column = params.get('column')
    n = int(params.get('n', 10))
    order = (params.get('order') or params.get('direction') or 'desc').lower()

    if not column:
        raise ValueError("column is required for TOP_N")
    if column not in df.columns:
        raise ValueError(f"Column '{column}' not found. Available: {list(df.columns)}")

    ascending = order in ('asc', 'ascending')
    try:
        sorted_df = df.sort_values(by=column, ascending=ascending, na_position='last')
    except TypeError:
        # Non-numeric column: sort as string
        sorted_df = df.sort_values(by=column, ascending=ascending, na_position='last', key=lambda x: x.astype(str))

    top = sorted_df.head(n)
    row_indices = top.index.tolist()  # original row indices

    col_stats = {}
    try:
        numeric_vals = pd.to_numeric(df[column], errors='coerce').dropna()
        if len(numeric_vals) > 0:
            col_stats = {
                'max': float(numeric_vals.max()),
                'min': float(numeric_vals.min()),
                'mean': round(float(numeric_vals.mean()), 4),
            }
    except Exception:
        pass

    return {
        'operation': 'TOP_N',
        'row_indices': row_indices,
        'result_count': len(row_indices),
        'summary': f"Top {n} rows by {column} ({order})",
        'column_stats': col_stats,
    }


def _op_bottom_n(df, params):
    params = {**params, 'order': 'asc'}
    result = _op_top_n(df, params)
    result['operation'] = 'BOTTOM_N'
    result['summary'] = result['summary'].replace('Top', 'Bottom')
    return result


def _op_find_duplicates(df, params):
    columns = params.get('columns') or []
    if not columns:
        raise ValueError("columns is required for FIND_DUPLICATES")

    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise ValueError(f"Columns not found: {missing}. Available: {list(df.columns)}")

    dupes = df[df.duplicated(subset=columns, keep=False)]
    row_indices = dupes.index.tolist()

    return {
        'operation': 'FIND_DUPLICATES',
        'row_indices': row_indices,
        'result_count': len(row_indices),
        'summary': f"{len(row_indices)} duplicate rows found in {', '.join(columns)}",
        'column_stats': {},
    }


def _op_find_missing(df, params):
    columns = params.get('columns') or []
    if not columns:
        # Check all columns
        columns = list(df.columns)

    missing_in = [c for c in columns if c not in df.columns]
    if missing_in:
        raise ValueError(f"Columns not found: {missing_in}. Available: {list(df.columns)}")

    mask = df[columns].isnull().any(axis=1)
    missing_rows = df[mask]
    row_indices = missing_rows.index.tolist()

    missing_counts = {c: int(df[c].isnull().sum()) for c in columns if df[c].isnull().any()}

    return {
        'operation': 'FIND_MISSING',
        'row_indices': row_indices,
        'result_count': len(row_indices),
        'summary': f"{len(row_indices)} rows with missing values",
        'column_stats': missing_counts,
    }


def _op_find_outliers(df, params):
    import pandas as pd
    column = params.get('column')
    method = (params.get('method') or 'iqr').lower()

    if not column:
        raise ValueError("column is required for FIND_OUTLIERS")
    if column not in df.columns:
        raise ValueError(f"Column '{column}' not found. Available: {list(df.columns)}")

    series = pd.to_numeric(df[column], errors='coerce').dropna()
    if len(series) == 0:
        raise ValueError(f"Column '{column}' has no numeric values")

    if method == 'iqr':
        Q1 = series.quantile(0.25)
        Q3 = series.quantile(0.75)
        IQR = Q3 - Q1
        lower = Q1 - 1.5 * IQR
        upper = Q3 + 1.5 * IQR
        outlier_mask = (pd.to_numeric(df[column], errors='coerce') < lower) | \
                       (pd.to_numeric(df[column], errors='coerce') > upper)
    elif method == 'zscore':
        mean = series.mean()
        std = series.std()
        z = ((pd.to_numeric(df[column], errors='coerce') - mean) / std).abs()
        outlier_mask = z > 3
    else:
        raise ValueError(f"Unknown outlier method: '{method}'. Use 'iqr' or 'zscore'.")

    outlier_rows = df[outlier_mask]
    row_indices = outlier_rows.index.tolist()

    return {
        'operation': 'FIND_OUTLIERS',
        'row_indices': row_indices,
        'result_count': len(row_indices),
        'summary': f"{len(row_indices)} outliers found in {column} ({method.upper()})",
        'column_stats': {
            'min': float(series.min()),
            'max': float(series.max()),
            'mean': round(float(series.mean()), 4),
            'std': round(float(series.std()), 4),
        },
    }


def _op_aggregate(df, params):
    import pandas as pd
    column = params.get('column')
    func = (params.get('func') or params.get('function') or 'mean').lower()
    group_by = params.get('group_by')

    if not column:
        raise ValueError("column is required for AGGREGATE")
    if column not in df.columns:
        raise ValueError(f"Column '{column}' not found. Available: {list(df.columns)}")

    numeric_col = pd.to_numeric(df[column], errors='coerce')
    agg_funcs = {
        'mean': lambda s: round(float(s.mean()), 4),
        'sum': lambda s: float(s.sum()),
        'count': lambda s: int(s.count()),
        'min': lambda s: float(s.min()),
        'max': lambda s: float(s.max()),
        'median': lambda s: round(float(s.median()), 4),
        'std': lambda s: round(float(s.std()), 4),
    }
    if func not in agg_funcs:
        raise ValueError(f"Unknown aggregate function '{func}'. Use: {list(agg_funcs)}")

    result_val = agg_funcs[func](numeric_col)

    group_result = {}
    if group_by and group_by in df.columns:
        grouped = df.groupby(group_by)[column].apply(lambda s: agg_funcs[func](pd.to_numeric(s, errors='coerce')))
        group_result = grouped.to_dict()

    return {
        'operation': 'AGGREGATE',
        'row_indices': [],  # Aggregate doesn't highlight rows
        'result_count': 0,
        'summary': f"{func}({column}) = {result_val}",
        'column_stats': {
            'result': result_val,
            'function': func,
            'column': column,
            'groups': group_result,
        },
    }


# ─── /api/spreadsheet/interpret ──────────────────────────────────────────────

@spreadsheet_bp.route('/interpret', methods=['POST'])
def interpret():
    """
    Interpret a natural language query into a structured spreadsheet operation.

    The LLM determines intent; this endpoint returns a structured operation dict.
    The frontend deterministic engine executes that operation.

    Request body:
      dataset_id        (str, required)
      session_id        (str, optional)
      query             (str, required)
      spreadsheet_context (dict, optional) — current spreadsheet state snapshot

    Response:
      { operation: { type, ...params }, explanation, confidence }
    """
    data = request.get_json(silent=True) or {}
    dataset_id = data.get('dataset_id', '').strip()
    query = (data.get('query') or '').strip()
    session_id = data.get('session_id')
    spreadsheet_context = data.get('spreadsheet_context') or {}

    if not query:
        return jsonify({'success': False, 'error': 'query is required'}), 400

    # Get column info for context
    columns_info = []
    if dataset_id:
        df, err = _get_dataframe(dataset_id)
        if df is not None:
            columns_info = list(df.columns)

    # Build a structured system prompt for operation interpretation
    context_str = _build_context_str(spreadsheet_context, columns_info)

    try:
        operation_result = _call_llm_for_operation(query, context_str, columns_info, session_id, dataset_id)
        return jsonify({'success': True, **operation_result})
    except Exception as e:
        logger.error(f"Spreadsheet interpret error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': f'Interpretation failed: {str(e)}'}), 500


def _build_context_str(ctx: dict, columns: list) -> str:
    """Build a concise context string for the LLM system prompt."""
    parts = []
    if columns:
        parts.append(f"Dataset columns: {', '.join(columns)}")
    dataset = ctx.get('dataset', {})
    if dataset.get('totalRows'):
        parts.append(f"Total rows: {dataset['totalRows']:,}")
    view = ctx.get('view', {})
    if view.get('sort'):
        s = view['sort']
        parts.append(f"Currently sorted by {s.get('col')} {s.get('dir')}")
    if view.get('filters'):
        parts.append(f"Active filters: {len(view['filters'])}")
    sel = ctx.get('selection', {})
    if sel.get('selectedRows'):
        parts.append(f"Selected rows: {sel['selectedRows'][:10]}")
    ref = ctx.get('referenceContext', {})
    if ref.get('target'):
        parts.append(f"Reference target: {ref['target']}")
    history = ctx.get('operationHistory', {})
    if history.get('recent'):
        recent = history['recent']
        parts.append(f"Recent operations: {[op.get('type') for op in recent]}")
    return '; '.join(parts)



def _call_llm_for_operation(query: str, context_str: str, columns: list, session_id: str, dataset_id: str) -> dict:
    """
    Call the LLM to interpret the query into a structured spreadsheet operation.

    IMPORTANT: This function BYPASSES the normal conversation pipeline
    (request_pipeline -> master_router -> ConversationEngine).

    The conversation pipeline is designed for chat responses, not structured JSON
    operations. Routing through it causes:
      1. Intent classification marks spreadsheet queries as 'conversation'
      2. ConversationEngine generates prose, not JSON
      3. The prose cannot be parsed as a structured operation

    This calls provider_engine.generate() DIRECTLY. This is the SPREADSHEET AGENT
    path, completely separate from the CHAT path. Both share providers/models but
    NOT the routing/engine pipeline.
    """
    col_list = ', '.join(columns) if columns else 'unknown'
    ctx_line = f"Context: {context_str}" if context_str else ""

    system_prompt = (
        "You are the Plexis spreadsheet assistant.\n\n"
        "Your job: convert the user query into a structured JSON operation AND write a natural, concise explanation.\n\n"
        + (f"{ctx_line}\n\n" if ctx_line else "")
        + "Supported operations and schemas:\n"
        + '- TOP_N:           {"type": "TOP_N",           "column": "col", "n": 16,  "order": "desc"}\n'
        + '- BOTTOM_N:        {"type": "BOTTOM_N",         "column": "col", "n": 10,  "order": "asc"}\n'
        + '- FILTER:          {"type": "FILTER",           "column": "col", "operator": ">", "value": 40}\n'
        + '- SORT:            {"type": "SORT",             "column": "col", "direction": "desc"}\n'
        + '- FIND_DUPLICATES: {"type": "FIND_DUPLICATES",  "columns": ["email"]}\n'
        + '- FIND_MISSING:    {"type": "FIND_MISSING",     "columns": ["email"]}\n'
        + '- FIND_OUTLIERS:   {"type": "FIND_OUTLIERS",    "column": "age", "method": "iqr"}\n'
        + '- AGGREGATE:       {"type": "AGGREGATE",        "column": "age", "func": "mean"}\n'
        + '- NAVIGATE:        {"type": "NAVIGATE",         "row": 8273}\n'
        + '- HIGHLIGHT:       {"type": "HIGHLIGHT",        "rows": [1, 2, 3]}\n'
        + '- SELECT_ROWS:     {"type": "SELECT_ROWS",      "rows": [1, 2, 3]}\n'
        + '- CLEAR:           {"type": "CLEAR"}\n'
        + '- UNDO:            {"type": "UNDO"}\n'
        + '- SEARCH:          {"type": "SEARCH",           "query": "text"}\n'
        + '- EXPLAIN_ROW:     {"type": "EXPLAIN_ROW",      "row": 42}\n'
        + f"\nAvailable columns: [{col_list}]\n\n"
        + "Rules:\n"
        + '1. Return ONLY a valid JSON object - no prose, no markdown, no code fences.\n'
        + '2. Top-level keys must be exactly: "operation", "explanation", "confidence"\n'
        + "3. NEVER invent column names not in available columns list.\n"
        + "4. highest/top/largest/greatest/maximum -> TOP_N (n=1 if no number given)\n"
        + "5. lowest/bottom/smallest/minimum -> BOTTOM_N (n=1 if no number given)\n"
        + "6. sort by X -> SORT; filter X > Y -> FILTER; duplicate -> FIND_DUPLICATES; missing/null/empty -> FIND_MISSING\n"
        + "7. undo/go back -> UNDO; clear/reset -> CLEAR\n"
        + "8. If the query is a general knowledge question (not about the data), return: {\"operation\": null, \"explanation\": \"That's a general question — let me answer it in the main chat.\", \"confidence\": 0.0}\n"
        + '9. If unable to determine operation: {"operation": null, "explanation": "I wasn\'t sure how to interpret that. Could you rephrase it?", "confidence": 0.0}\n\n'
        + "EXPLANATION RULES (important):\n"
        + "- The explanation field is the AI\'s message to the user in the main chat.\n"
        + "- Write it as a warm, natural 1-2 sentence response, not a technical label.\n"
        + "- Do NOT prefix with I\'m going to or I will — just state what is happening.\n"
        + "- Keep it under 100 characters.\n\n"
        + "Examples:\n"
        + 'Q: "highest age"  -> {"operation": {"type": "TOP_N", "column": "age", "n": 1, "order": "desc"}, "explanation": "I found the highest age in the dataset.", "confidence": 0.97}\n'
        + 'Q: "show 16 highest ages" -> {"operation": {"type": "TOP_N", "column": "age", "n": 16, "order": "desc"}, "explanation": "Here are the top 16 ages in the dataset.", "confidence": 0.97}\n'
        + 'Q: "sort by age desc" -> {"operation": {"type": "SORT", "column": "age", "direction": "desc"}, "explanation": "Sorted by age from highest to lowest.", "confidence": 0.98}\n'
        + 'Q: "show people older than 50" -> {"operation": {"type": "FILTER", "column": "age", "operator": ">", "value": 50}, "explanation": "Filtered to show records where age is greater than 50.", "confidence": 0.96}\n'
        + 'Q: "find duplicates in email" -> {"operation": {"type": "FIND_DUPLICATES", "columns": ["email"]}, "explanation": "Looking for duplicate values in the email column.", "confidence": 0.95}\n'
        + 'Q: "what is standard deviation" -> {"operation": null, "explanation": "That\'s a general question \u2014 let me answer it in the main chat.", "confidence": 0.0}'
    )

    try:
        from providers import provider_engine
        from providers.domain.contracts import AIRequest

        # Model resolution diagnostics (no API keys logged)
        try:
            from providers.model_registry import model_registry
            from providers.circuit_breaker import circuit_breaker
            interpret_models = model_registry.get_models_for_task('spreadsheet_interpret')
            ok_models = [m for m in interpret_models if circuit_breaker.can_call(m.provider)]
            logger.info(
                f"[SpreadsheetAgent] model resolution: task=spreadsheet_interpret "
                f"configured={len(interpret_models)} circuit_breaker_ok={len(ok_models)} "
                f"providers={list(set(m.provider for m in ok_models))}"
            )
        except Exception as diag_err:
            logger.debug(f"[SpreadsheetAgent] model diagnostics skipped: {diag_err}")

        # Direct call -- bypasses conversation pipeline entirely
        # task='spreadsheet_interpret' ensures the model selector picks a model
        # with spreadsheet_interpret affinity, not a generic chat model.
        response = provider_engine.generate(
            AIRequest(
                task='spreadsheet_interpret',
                messages=[{"role": "user", "content": f"Query: {query}"}],
                system_prompt=system_prompt,
                temperature=0.1,
                json_mode=False,
            )
        )

        if not response.success:
            logger.warning(
                f"[SpreadsheetAgent] LLM unavailable: provider={response.provider} "
                f"error={response.error}. Falling back to heuristic."
            )
            return _parse_operation_json_heuristic(query, columns)

        logger.info(
            f"[SpreadsheetAgent] LLM ok: provider={response.provider} "
            f"model={response.model} latency={response.latency_ms:.0f}ms"
        )
        return _parse_operation_json(response.text or '')

    except Exception as e:
        logger.warning(f"[SpreadsheetAgent] Exception: {e}. Using heuristic fallback.")
        return _parse_operation_json_heuristic(query, columns)


def _parse_operation_json(text: str) -> dict:
    """Extract and parse JSON from LLM response text."""
    import re
    text = text.strip()
    # Strip markdown code fences if present
    text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'```\s*$', '', text, flags=re.MULTILINE)
    text = text.strip()

    try:
        parsed = json.loads(text)
        return {
            'operation': parsed.get('operation'),
            'explanation': parsed.get('explanation', ''),
            'confidence': float(parsed.get('confidence', 0.8)),
            'source': 'llm',
        }
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
                return {
                    'operation': parsed.get('operation'),
                    'explanation': parsed.get('explanation', ''),
                    'confidence': float(parsed.get('confidence', 0.7)),
                    'source': 'llm',
                }
            except Exception:
                pass
        return {
            'operation': None,
            'explanation': "I wasn't sure how to interpret that. Could you rephrase it?",
            'confidence': 0.0,
            'source': 'parse_error',
        }


def _parse_operation_json_heuristic(query: str, columns: list) -> dict:
    """
    Keyword-based heuristic fallback when LLM is unavailable.
    Not a replacement for LLM interpretation -- graceful degradation only.
    """
    import re
    q = query.lower()

    # Extract explicit number N from query (e.g. "top 5", "16 highest")
    n_match = re.search(r'\b(\d+)\b', q)
    n_explicit = int(n_match.group(1)) if n_match else None

    def find_col():
        for c in columns:
            if c.lower() in q:
                return c
        return columns[0] if columns else None

    # TOP N / HIGHEST
    top_match = re.search(r'\btop\s+(\d+)\b', q)
    if top_match:
        n = int(top_match.group(1))
        col = find_col()
        return {'operation': {'type': 'TOP_N', 'column': col, 'n': n, 'order': 'desc'},
                'explanation': f'Top {n} rows by {col}.', 'confidence': 0.65, 'source': 'heuristic'}

    if any(w in q for w in ('highest', 'largest', 'greatest', 'maximum')):
        col = find_col()
        n = n_explicit if n_explicit and n_explicit > 1 else 1
        return {'operation': {'type': 'TOP_N', 'column': col, 'n': n, 'order': 'desc'},
                'explanation': f'{"Top " + str(n) if n > 1 else "Highest"} row(s) by {col}.',
                'confidence': 0.65, 'source': 'heuristic'}

    # BOTTOM N / LOWEST
    bottom_match = re.search(r'\bbottom\s+(\d+)\b', q)
    if bottom_match:
        n = int(bottom_match.group(1))
        col = find_col()
        return {'operation': {'type': 'BOTTOM_N', 'column': col, 'n': n, 'order': 'asc'},
                'explanation': f'Bottom {n} rows by {col}.', 'confidence': 0.65, 'source': 'heuristic'}

    if any(w in q for w in ('lowest', 'smallest', 'minimum')):
        col = find_col()
        n = n_explicit if n_explicit and n_explicit > 1 else 1
        return {'operation': {'type': 'BOTTOM_N', 'column': col, 'n': n, 'order': 'asc'},
                'explanation': f'{"Bottom " + str(n) if n > 1 else "Lowest"} row(s) by {col}.',
                'confidence': 0.65, 'source': 'heuristic'}

    # SORT
    if 'sort' in q or 'order by' in q:
        col = find_col()
        direction = 'asc' if any(w in q for w in ('asc', 'ascending', 'low to high')) else 'desc'
        return {'operation': {'type': 'SORT', 'column': col, 'direction': direction},
                'explanation': f'Sort by {col} {direction}.', 'confidence': 0.60, 'source': 'heuristic'}

    # FILTER
    filter_gt = re.search(r'(?:above|over|greater than|more than|older than|>)\s*(\d+(?:\.\d+)?)', q)
    if filter_gt:
        col = find_col()
        return {'operation': {'type': 'FILTER', 'column': col, 'operator': '>', 'value': float(filter_gt.group(1))},
                'explanation': f'Filter {col} > {filter_gt.group(1)}.', 'confidence': 0.65, 'source': 'heuristic'}

    filter_lt = re.search(r'(?:below|under|less than|fewer than|younger than|<)\s*(\d+(?:\.\d+)?)', q)
    if filter_lt:
        col = find_col()
        return {'operation': {'type': 'FILTER', 'column': col, 'operator': '<', 'value': float(filter_lt.group(1))},
                'explanation': f'Filter {col} < {filter_lt.group(1)}.', 'confidence': 0.65, 'source': 'heuristic'}

    # FIND DUPLICATES
    if any(w in q for w in ('duplicate', 'duplicates', 'repeated')):
        col = find_col()
        return {'operation': {'type': 'FIND_DUPLICATES', 'columns': [col] if col else columns},
                'explanation': 'Finding duplicate rows.', 'confidence': 0.70, 'source': 'heuristic'}

    # FIND MISSING
    if any(w in q for w in ('missing', 'null', 'empty', 'blank', 'none', 'nan')):
        col = find_col()
        return {'operation': {'type': 'FIND_MISSING', 'columns': [col] if col else columns},
                'explanation': 'Finding rows with missing values.', 'confidence': 0.70, 'source': 'heuristic'}

    # UNDO
    if any(w in q for w in ('undo', 'go back', 'revert', 'previous')):
        return {'operation': {'type': 'UNDO'}, 'explanation': 'Undoing the last operation.',
                'confidence': 0.80, 'source': 'heuristic'}

    # CLEAR
    if any(w in q for w in ('clear', 'reset', 'start over', 'show all')):
        return {'operation': {'type': 'CLEAR'}, 'explanation': 'Clearing workspace state.',
                'confidence': 0.75, 'source': 'heuristic'}

    # SEARCH
    search_match = re.search(r'(?:search|find|look for)\s+["\'"]?(.+)["\'"]?', q)
    if search_match:
        term = search_match.group(1).strip().strip('"\' ')
        return {'operation': {'type': 'SEARCH', 'query': term},
                'explanation': f'Searching for "{term}".', 'confidence': 0.60, 'source': 'heuristic'}

    return {
        'operation': None,
        'explanation': 'Could not determine a spreadsheet operation from this query.',
        'confidence': 0.0,
        'source': 'heuristic',
    }


# ─── Spreadsheet Editing and Revision Routes ────────────────────────────────

@spreadsheet_bp.route('/edit', methods=['POST'])
def edit_cell():
    """
    Apply a single cell edit with optimistic concurrency check.
    """
    from db.session import get_db
    from spreadsheet.persistence import spreadsheet_history_manager, ConcurrencyConflictError

    data = request.get_json(silent=True) or {}
    dataset_id = str(data.get('dataset_id') or '').strip()
    if not dataset_id:
        return jsonify({'success': False, 'error': 'dataset_id is required'}), 400

    if 'row' not in data or 'col' not in data:
        return jsonify({'success': False, 'error': 'row and col are required'}), 400

    try:
        row_idx = int(data['row'])
    except (ValueError, TypeError):
        return jsonify({'success': False, 'error': 'row must be an integer'}), 400

    col_name = str(data['col'])
    new_value = data.get('value')
    base_revision = int(data.get('base_revision', 0))

    db = get_db()
    if db is None:
        return jsonify({'success': False, 'error': 'Database unavailable'}), 500

    try:
        result = spreadsheet_history_manager.apply_cell_edit(
            db=db,
            dataset_id=dataset_id,
            row_idx=row_idx,
            col_name=col_name,
            new_value=new_value,
            base_revision=base_revision,
        )
        return jsonify(result), 200
    except ConcurrencyConflictError as cce:
        return jsonify({
            'success': False,
            'conflict': True,
            'error': str(cce),
            'expected_revision': cce.expected_revision,
            'actual_revision': cce.actual_revision,
        }), 409
    except (IndexError, KeyError, ValueError) as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    except Exception as e:
        logger.error("[spreadsheet/edit] Error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@spreadsheet_bp.route('/batch-edit', methods=['POST'])
def batch_edit():
    """
    Apply a batch of cell edits atomically with optimistic concurrency check.
    """
    from db.session import get_db
    from spreadsheet.persistence import spreadsheet_history_manager, ConcurrencyConflictError

    data = request.get_json(silent=True) or {}
    dataset_id = str(data.get('dataset_id') or '').strip()
    if not dataset_id:
        return jsonify({'success': False, 'error': 'dataset_id is required'}), 400

    edits = data.get('edits')
    if not isinstance(edits, list) or not edits:
        return jsonify({'success': False, 'error': 'edits list is required and cannot be empty'}), 400

    base_revision = int(data.get('base_revision', 0))

    db = get_db()
    if db is None:
        return jsonify({'success': False, 'error': 'Database unavailable'}), 500

    try:
        result = spreadsheet_history_manager.apply_batch_edit(
            db=db,
            dataset_id=dataset_id,
            edits=edits,
            base_revision=base_revision,
        )
        return jsonify(result), 200
    except ConcurrencyConflictError as cce:
        return jsonify({
            'success': False,
            'conflict': True,
            'error': str(cce),
            'expected_revision': cce.expected_revision,
            'actual_revision': cce.actual_revision,
        }), 409
    except (IndexError, KeyError, ValueError) as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    except Exception as e:
        logger.error("[spreadsheet/batch-edit] Error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@spreadsheet_bp.route('/undo', methods=['POST'])
def undo_action():
    """
    Undo the latest committed spreadsheet action.
    """
    from db.session import get_db
    from spreadsheet.persistence import spreadsheet_history_manager

    data = request.get_json(silent=True) or {}
    dataset_id = str(data.get('dataset_id') or '').strip()
    if not dataset_id:
        return jsonify({'success': False, 'error': 'dataset_id is required'}), 400

    db = get_db()
    if db is None:
        return jsonify({'success': False, 'error': 'Database unavailable'}), 500

    try:
        result = spreadsheet_history_manager.undo(db=db, dataset_id=dataset_id)
        status_code = 200 if result.get('success') else 404
        return jsonify(result), status_code
    except Exception as e:
        logger.error("[spreadsheet/undo] Error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@spreadsheet_bp.route('/redo', methods=['POST'])
def redo_action():
    """
    Redo the next undone spreadsheet action.
    """
    from db.session import get_db
    from spreadsheet.persistence import spreadsheet_history_manager

    data = request.get_json(silent=True) or {}
    dataset_id = str(data.get('dataset_id') or '').strip()
    if not dataset_id:
        return jsonify({'success': False, 'error': 'dataset_id is required'}), 400

    db = get_db()
    if db is None:
        return jsonify({'success': False, 'error': 'Database unavailable'}), 500

    try:
        result = spreadsheet_history_manager.redo(db=db, dataset_id=dataset_id)
        status_code = 200 if result.get('success') else 404
        return jsonify(result), status_code
    except Exception as e:
        logger.error("[spreadsheet/redo] Error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@spreadsheet_bp.route('/history/<dataset_id>', methods=['GET'])
def get_action_history(dataset_id: str):
    """
    Retrieve full persistent action history, current revision, and undo/redo capabilities.
    """
    from db.session import get_db
    from spreadsheet.persistence import spreadsheet_history_manager

    db = get_db()
    if db is None:
        return jsonify({'success': False, 'error': 'Database unavailable'}), 500

    try:
        result = spreadsheet_history_manager.get_history(db=db, dataset_id=dataset_id)
        return jsonify({'success': True, **result}), 200
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 404
    except Exception as e:
        logger.error("[spreadsheet/history] Error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
