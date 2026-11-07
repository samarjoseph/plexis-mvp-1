"""
Windowed Dataset Data API — GET /api/datasets/{dataset_id}/data

Provides paginated, filterable, sortable access to the raw dataset rows.
Used by DatasetWorkspace for virtual scrolling — only ~30-100 rows loaded at a time.

Query parameters:
  offset    (int, default 0)     — start row
  limit     (int, default 100, max 500)  — rows per page
  sort_col  (str)                — column to sort by
  sort_dir  (str: asc|desc)     — sort direction
  search    (str)                — global search across all columns
  filter_col (str)               — column to filter
  filter_val (str)               — filter value (exact match or contains)

Response:
  { columns: [...], rows: [...], total_rows: N, offset: N, limit: N }

_row_idx contract:
  Each row in the response includes '_row_idx' = the original DataFrame index
  (integer, absolute, pre-sort, pre-filter). This is stable across all view states.
  The frontend Locate feature uses this to find rows after sort/filter reorders the view.
"""
import logging
import re
from typing import Tuple, Any

import pandas as pd
from flask import Blueprint, jsonify, request

from datasets.registry import dataset_registry
from utils.response import error_response

logger = logging.getLogger(__name__)

datasets_data_bp = Blueprint('datasets_data', __name__, url_prefix='/api')


def _try_restore_from_db(dataset_id: str) -> pd.DataFrame | None:
    """
    Attempt to restore a dataset DataFrame from PostgreSQL when L1 cache is cold.
    This handles server restart / multi-worker scenarios where the in-memory
    DatasetRegistry is empty but the data exists in the DB.
    Returns DataFrame or None if restore fails.
    """
    try:
        from db.session import db_session
        from db.services.dataset_service import dataset_service
        from uuid import UUID
        with db_session() as db:
            _, df = dataset_service.restore_dataset_df(db, UUID(dataset_id), user_id=None)  # type: ignore[arg-type]
            # restore_dataset_df re-registers in cache, so re-check
            if df is not None:
                logger.info("[DATASETS_DATA] Restored dataset %s from DB into cache", dataset_id)
                return df
    except Exception as e:
        logger.warning("[DATASETS_DATA] DB restore failed for dataset %s: %s", dataset_id, e)
    return None


@datasets_data_bp.route('/datasets/<dataset_id>/data', methods=['GET'])
def get_dataset_data(dataset_id: str) -> Tuple[Any, int]:
    """
    Return a windowed slice of dataset rows for virtual scrolling.
    All filtering and sorting is done server-side via pandas.
    Falls back to PostgreSQL restore if dataset not in L1 cache.
    """
    from db.identity import identity_resolver
    try:
        user = identity_resolver.resolve(request)
        if user and user.is_guest:
            guest_ds_id = f"guest_ds_{user.session_id}"
            if dataset_id != guest_ds_id:
                err, status = error_response("Guests cannot access other datasets", 403)
                return jsonify(err), status
    except Exception:
        pass

    entry = dataset_registry.get_by_id(dataset_id)
    df: pd.DataFrame | None = entry.dataframe if entry else None

    # L2 DB restore on cache miss (handles server restart / multi-worker cold start)
    if df is None or (hasattr(df, 'empty') and df.empty):
        df = _try_restore_from_db(dataset_id)
        # Re-check entry after potential restore
        entry = dataset_registry.get_by_id(dataset_id)

    if entry is None or df is None or df.empty:
        err, status = error_response(f"Dataset '{dataset_id}' not found", 404)
        return jsonify(err), status

    try:
        # ── Parse query params ───────────────────────────────────────────────
        offset    = max(0, int(request.args.get('offset', 0)))
        limit     = min(500, max(1, int(request.args.get('limit', 100))))
        sort_col  = request.args.get('sort_col', '').strip() or None
        sort_dir  = request.args.get('sort_dir', 'asc').strip().lower()
        search    = request.args.get('search', '').strip()
        filter_col = request.args.get('filter_col', '').strip() or None
        filter_val = request.args.get('filter_val', '').strip()

        # ── Resolve plan and enforce spreadsheet rendering limits ──
        user_plan = "free"
        try:
            from db.identity import identity_resolver
            curr_user = identity_resolver.resolve(request)
            if curr_user and getattr(curr_user, "plan", None):
                user_plan = str(curr_user.plan).strip().lower()
        except Exception:
            pass

        working = df.copy()
        is_plan_capped = False
        untruncated_total_rows = len(df)

        # Free tier: max 15,000 rows rendered on spreadsheet
        # Plus tier: renders whole dataset
        if user_plan == "free" and len(working) > 15000:
            working = working.iloc[:15000]
            is_plan_capped = True

        # ── Global search (all columns, case-insensitive contains, NO regex) ─
        # regex=False prevents re.error crashes when users type regex chars ([(+*. etc.)
        if search:
            safe_search = re.escape(search) if len(search) > 1 else search
            mask = working.apply(
                lambda col: col.astype(str).str.contains(safe_search, case=False, na=False, regex=False)
            ).any(axis=1)
            working = working[mask]

        # ── Column filter ────────────────────────────────────────────────────
        if filter_col and filter_col in working.columns and filter_val:
            col_series = working[filter_col].astype(str)
            working = working[col_series.str.contains(filter_val, case=False, na=False, regex=False)]

        # ── Sort ─────────────────────────────────────────────────────────────
        # Use key= lambda to stringify mixed-type columns, preventing TypeError on '<' not supported
        if sort_col and sort_col in working.columns:
            ascending = sort_dir != 'desc'
            try:
                working = working.sort_values(
                    by=sort_col,
                    ascending=ascending,
                    na_position='last',
                )
            except TypeError:
                # Mixed type column — fall back to string sort
                working = working.sort_values(
                    by=sort_col,
                    ascending=ascending,
                    na_position='last',
                    key=lambda s: s.astype(str),
                )

        total_rows = len(working)

        # ── Windowed slice ───────────────────────────────────────────────────
        page = working.iloc[offset: offset + limit]

        # ── Serialize safely ─────────────────────────────────────────────────
        columns = list(page.columns)
        rows = []
        for original_idx, row in page.iterrows():
            safe_row = {}
            # _row_idx = original DataFrame index (absolute, pre-sort, pre-filter)
            # Used by frontend Locate to navigate to the correct row even after
            # sorting/filtering. Must NOT be display_index or filtered_index.
            # Safely convert to int — handles RangeIndex, Int64Index, MultiIndex fallback
            try:
                safe_row['_row_idx'] = int(original_idx)
            except (TypeError, ValueError):
                safe_row['_row_idx'] = hash(str(original_idx))  # fallback for exotic indices
            for col in columns:
                val = row[col]
                try:
                    if pd.isna(val):
                        safe_row[col] = None
                        continue
                except (TypeError, ValueError):
                    pass  # pd.isna raises on unhashable types
                if hasattr(val, 'item'):          # numpy scalar
                    safe_row[col] = val.item()
                else:
                    safe_row[col] = val
            rows.append(safe_row)

        return jsonify({
            'columns': columns,
            'all_columns': list(df.columns),   # full column list from original DataFrame
            'rows': rows,
            'total_rows': total_rows,
            'total_columns': len(df.columns),  # total columns in original (not filtered)
            'offset': offset,
            'limit': limit,
            'has_more': (offset + limit) < total_rows,
            'dataset_id': dataset_id,
            'filename': entry.filename,
            'plan': user_plan,
            'plan_capped': is_plan_capped,
            'plan_max_rows': 15000 if user_plan == "free" else None,
            'untruncated_total_rows': untruncated_total_rows,
        }), 200

    except ValueError as ve:
        logger.warning(f"datasets_data bad param for {dataset_id}: {ve}")
        err, status = error_response(f"Invalid query parameter: {ve}", 400)
        return jsonify(err), status
    except Exception as e:
        logger.error(f"datasets_data error for {dataset_id}: {e}", exc_info=True)
        err, status = error_response(f"Failed to retrieve data: {str(e)}", 500)
        return jsonify(err), status
