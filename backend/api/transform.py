"""
Transformation API — plan, apply, undo, versions, export.

Routes:
  POST /api/transform/plan              — Generate a plan (no mutation)
  POST /api/transform/apply             — Apply an approved plan by plan_id
  POST /api/transform/undo              — Undo last transformation for dataset
  GET  /api/transform/versions/<id>     — List version history
  GET  /api/transform/export/<id>       — Export current dataset as CSV download
"""
from __future__ import annotations
import io
import logging
import uuid
from typing import Optional

from flask import Blueprint, request, jsonify, Response, stream_with_context

from db.session import get_db
from db.identity import identity_resolver, IdentityError

logger = logging.getLogger(__name__)
transform_bp = Blueprint('transform', __name__, url_prefix='/api/transform')


def _resolve_user(req):
    try:
        return identity_resolver.resolve(req), None
    except IdentityError as e:
        return None, (jsonify({'error': str(e)}), 401)
    except Exception as e:
        return None, (jsonify({'error': 'Identity error'}), 500)


@transform_bp.route('/plan', methods=['POST'])
def create_plan():
    """
    Generate a TransformationPlan from a natural language command.
    Does NOT mutate the dataset.

    Body: { message: str, dataset_id: str }
    """
    current_user, _ = _resolve_user(request)
    if current_user and current_user.is_guest:
        from core.guest import check_guest_transformation_quota
        guest_ds_id = f"guest_ds_{current_user.session_id}"
        req_ds = (request.get_json(silent=True) or {}).get('dataset_id', '')
        if req_ds and req_ds not in (guest_ds_id, "sample_customer.csv", "sample_customers.csv"):
            return jsonify({'error': 'Guests cannot access other datasets'}), 403

        allowed, used, limit = check_guest_transformation_quota(get_db(), current_user.guest_id or "")
        if not allowed:
            return jsonify({
                "error": "GUEST_LIMIT_REACHED",
                "code": "GUEST_LIMIT_EXHAUSTED",
                "message": f"Guest Mode is limited to {limit} transformations per month (you have used {used}). Sign in with Google to upload your own datasets and get unlimited transformations.",
                "transformations_used": used,
                "transformations_max": limit,
                "transformations_remaining": 0,
                "plan": "guest",
            }), 403

    data = request.get_json(silent=True) or {}
    message = (data.get('message') or '').strip()
    dataset_id = (data.get('dataset_id') or '').strip()

    if not message:
        return jsonify({'error': 'message is required'}), 400
    if not dataset_id:
        return jsonify({'error': 'dataset_id is required'}), 400

    try:
        from datasets.registry import dataset_registry
        from transformation.planner import transformation_planner, ALLOWED_OPERATIONS
        from transformation.store import transformation_store

        entry = dataset_registry.get(dataset_id)
        if entry is None or entry.dataframe is None:
            return jsonify({'error': 'Dataset not found'}), 404

        df = entry.dataframe

        # Simple keyword-to-operation mapping for direct API calls
        # (when called directly, not via /api/ask TRANSFORM flow)
        operation = _parse_operation_from_message(message, df)
        if not operation:
            return jsonify({
                'error': (
                    'Could not parse transformation from message. '
                    'Try using the chat interface.'
                )
            }), 400

        plan = transformation_planner.plan(df, operation, dataset_id)
        transformation_store.store(plan)

        return jsonify({'plan': _serialize_plan(plan)}), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.error('[transform/plan] Error: %s', e, exc_info=True)
        return jsonify({'error': str(e)}), 500


@transform_bp.route('/apply', methods=['POST'])
def apply_plan():
    """
    Apply an approved TransformationPlan.

    Body: { plan_id: str, dataset_id: str }
    """
    current_user, err = _resolve_user(request)
    if err:
        current_user = None  # graceful degradation in stateless mode

    data = request.get_json(silent=True) or {}
    plan_id = (data.get('plan_id') or '').strip()
    dataset_id = (data.get('dataset_id') or '').strip()

    if not plan_id:
        return jsonify({'error': 'plan_id is required'}), 400
    if not dataset_id:
        return jsonify({'error': 'dataset_id is required'}), 400

    try:
        from transformation.store import transformation_store
        from transformation.executor import transformation_executor
        from datasets.registry import dataset_registry
        from db.repositories.transformation_repository import transformation_repository

        # Get plan from L1 cache
        plan = transformation_store.get(plan_id)

        # L2 fallback: check PostgreSQL
        if plan is None:
            db = get_db()
            if db is not None:
                try:
                    db_plan = transformation_repository.get_plan(db, uuid.UUID(plan_id))
                    if db_plan:
                        plan = _db_plan_to_contract(db_plan)
                except Exception as e:
                    logger.warning('[apply] L2 plan fetch failed: %s', e)

        if plan is None:
            return jsonify({
                'error': 'Plan not found or expired. Please regenerate the transformation plan.'
            }), 404

        # Verify plan is pending (if DB is available)
        db = get_db()
        if db is not None:
            try:
                db_plan = transformation_repository.get_plan(db, uuid.UUID(plan_id))
                if db_plan and db_plan.status != 'pending':
                    return jsonify({
                        'error': f"Plan already {db_plan.status}. Cannot apply again."
                    }), 409
            except Exception:
                pass  # If can't check status, proceed (stateless fallback)

        # ── Quota Check: Guest Mode (Max 2 transformations per month) ──
        if current_user and current_user.is_guest:
            from core.guest import check_guest_transformation_quota
            guest_ds_id = f"guest_ds_{current_user.session_id}"
            if dataset_id != guest_ds_id:
                if dataset_id in ("sample_customer.csv", "sample_customers.csv"):
                    dataset_id = guest_ds_id
                else:
                    return jsonify({'error': 'Guests cannot access other datasets'}), 403

            guest_allowed, guest_used, guest_limit = check_guest_transformation_quota(db, current_user.guest_id or "")
            if not guest_allowed:
                return jsonify({
                    "error": "GUEST_LIMIT_REACHED",
                    "code": "GUEST_LIMIT_EXHAUSTED",
                    "message": f"Guest Mode is limited to {guest_limit} transformations per month (you have used {guest_used}). Sign in with Google to unlock custom uploads, live spreadsheet, and unlimited transformations.",
                    "transformations_used": guest_used,
                    "transformations_max": guest_limit,
                    "transformations_remaining": 0,
                    "plan": "guest",
                }), 403
        else:
            # ── Quota Check: Daily Transformations (Free plan max 4/day) ──
            user_plan = getattr(current_user, "plan", "free") if current_user else "free"
            user_id = current_user.user_id if current_user else None
            from core.plans import check_daily_transformation_quota
            allowed, used_today, limit = check_daily_transformation_quota(db, user_id, user_plan)
            if not allowed:
                return jsonify({
                    "error": "PLAN_LIMIT_REACHED",
                    "message": f"Free plan is limited to {limit} transformations per day (you have used {used_today}). Upgrade to Plus for unlimited transformations.",
                    "used_today": used_today,
                    "limit": limit,
                    "plan": user_plan,
                }), 403

        # Resolve DataFrame
        entry = dataset_registry.get(dataset_id)
        if entry is None or entry.dataframe is None:
            return jsonify({'error': 'Dataset not found'}), 404

        # Get original DB dataset ID
        db = get_db()
        original_db_dataset_id = None
        try:
            original_db_dataset_id = uuid.UUID(str(entry.dataset_id))
        except Exception:
            pass

        user_id = uuid.UUID(str(current_user.user_id)) if current_user else None

        result = transformation_executor.apply(
            plan=plan,
            df=entry.dataframe,
            dataset_id=dataset_id,
            db=db,
            original_db_dataset_id=original_db_dataset_id,
            user_id=user_id,
            base_filename=getattr(entry, 'filename', 'dataset.csv'),
        )

        if not result.success:
            if result.error and 'DATASET_CHANGED' in result.error:
                return jsonify({'error': result.error, 'conflict': True}), 409
            return jsonify({'error': result.error or 'Transformation failed'}), 500

        # Record transformation quota consumption for guests only on successful commit
        guest_count = None
        if current_user and current_user.is_guest:
            from core.guest import record_guest_transformation
            guest_count = record_guest_transformation(db, current_user.guest_id or "")

        # Remove from L1 cache
        transformation_store.remove(plan_id)
        transformation_store.finish_plan_context(plan_id, 'APPLIED')

        resp_payload = {
            'success': True,
            'version_id': result.version_id,
            'version_number': result.version_number,
            'changed_cells': result.changed_cells,
            'operation_label': result.operation_label,
            'intelligence_provenance': getattr(result, 'intelligence_provenance', None),
        }
        if current_user and current_user.is_guest:
            resp_payload["transformations_used"] = guest_count
            resp_payload["transformations_max"] = 2
            resp_payload["transformations_remaining"] = max(0, 2 - (guest_count or 0))

        return jsonify(resp_payload), 200

    except Exception as e:
        logger.error('[transform/apply] Error: %s', e, exc_info=True)
        return jsonify({'error': str(e)}), 500


@transform_bp.route('/undo', methods=['POST'])
def undo_transformation():
    """
    Undo the last transformation for a dataset.

    Uses in-memory TransformationHistory as primary source.
    Falls back to PostgreSQL DatasetVersion records if in-memory history
    is unavailable.

    Body: { dataset_id: str }
    """
    data = request.get_json(silent=True) or {}
    dataset_id = (data.get('dataset_id') or '').strip()
    if not dataset_id:
        return jsonify({'error': 'dataset_id is required'}), 400

    try:
        from datasets.registry import dataset_registry
        from transformation.history import transformation_history

        entry = dataset_registry.get(dataset_id)
        if entry is None:
            return jsonify({'error': 'Dataset not found'}), 404

                # ── Check durable spreadsheet history first ──
        db = get_db()
        if db is not None:
            try:
                from spreadsheet.persistence import spreadsheet_history_manager
                history_info = spreadsheet_history_manager.get_history(db, dataset_id)
                if history_info.get('can_undo'):
                    res = spreadsheet_history_manager.undo(db, dataset_id)
                    if res.get('success'):
                        return jsonify({
                            'success': True,
                            'message': res.get('message'),
                            'restored_rows': res.get('restored_rows'),
                            'restored_columns': res.get('restored_columns'),
                            'undo_depth': res.get('current_revision'),
                        }), 200
            except Exception as e:
                logger.debug('[transform/undo] spreadsheet history undo skipped: %s', e)

        # ── Primary: in-memory undo history ──
        if transformation_history.has_history(dataset_id):
            undo_entry = transformation_history.pop(dataset_id)
            if undo_entry is None:
                return jsonify({
                    'success': False,
                    'error_code': 'NO_HISTORY',
                    'error': 'Nothing to undo.',
                }), 404

            # Restore the previous DataFrame
            entry.dataframe = undo_entry.previous_df.copy()
            entry.row_count = len(entry.dataframe)
            entry.column_count = len(entry.dataframe.columns)

            # Also try to clean up the DB version record (best effort)
            try:
                db = get_db()
                if db is not None:
                    original_db_dataset_id = None
                    try:
                        original_db_dataset_id = uuid.UUID(str(entry.dataset_id))
                    except Exception:
                        pass

                    if original_db_dataset_id:
                        from db.repositories.transformation_repository import transformation_repository
                        from db.models.transformation import DatasetVersion
                        latest = transformation_repository.get_latest_version(db, original_db_dataset_id)
                        if latest is not None:
                            db.delete(latest)
                            db.commit()
            except Exception as e:
                logger.warning('[undo] DB cleanup failed (non-fatal): %s', e)

            return jsonify({
                'success': True,
                'message': f"Undone: {undo_entry.operation_label}",
                'restored_rows': len(entry.dataframe),
                'restored_columns': len(entry.dataframe.columns),
                'undo_depth': transformation_history.get_depth(dataset_id),
            }), 200

        # ── Fallback: PostgreSQL version history ──
        import gzip
        import pandas as pd
        from db.models.dataset import DatasetFile
        from db.models.transformation import DatasetVersion
        from db.repositories.transformation_repository import transformation_repository

        db = get_db()
        if db is None:
            return jsonify({
                'success': False,
                'error_code': 'NO_HISTORY',
                'error': 'Nothing to undo.',
            }), 404

        original_db_dataset_id = None
        try:
            original_db_dataset_id = uuid.UUID(str(entry.dataset_id))
        except Exception:
            return jsonify({'error': 'Cannot resolve dataset DB ID'}), 400

        # Get the latest version
        latest = transformation_repository.get_latest_version(db, original_db_dataset_id)
        if latest is None:
            return jsonify({
                'success': False,
                'error_code': 'NO_HISTORY',
                'error': 'Nothing to undo.',
            }), 404

        # Find the version to restore (parent of latest)
        if latest.parent_version_id:
            parent_version = db.get(DatasetVersion, latest.parent_version_id)
            restore_dataset_id = parent_version.version_dataset_id if parent_version else None
        else:
            # No parent = restore from original dataset
            restore_dataset_id = original_db_dataset_id

        if restore_dataset_id is None:
            return jsonify({'error': 'Cannot find previous version to restore.'}), 404

        # Load the DataFrame from the restore dataset's file
        restore_file = db.query(DatasetFile).filter(
            DatasetFile.dataset_id == restore_dataset_id
        ).first()
        if restore_file is None:
            return jsonify({'error': 'Version file not found in storage.'}), 404

        snapshot_bytes = gzip.decompress(restore_file.content)
        restore_dataset = db.get(Dataset, restore_dataset_id)
        if restore_dataset is None:
            return jsonify({'error': 'Version dataset not found in storage.'}), 404
        from db.services.dataset_service import dataset_service
        restored_df = dataset_service._bytes_to_dataframe(
            snapshot_bytes, restore_dataset.original_filename
        )

        # Update L1 registry
        entry.dataframe = restored_df
        entry.row_count = len(restored_df)
        entry.column_count = len(restored_df.columns)

        # Delete the latest version record
        db.delete(latest)
        db.commit()

        # Increment in-memory version (undo is a state change)
        transformation_history.increment_version(dataset_id)

        return jsonify({
            'success': True,
            'message': f"Undone: {latest.operation_label}",
            'restored_rows': len(restored_df),
            'restored_columns': len(restored_df.columns),
            'undo_depth': 0,
        }), 200

    except Exception as e:
        logger.error('[transform/undo] Error: %s', e, exc_info=True)
        return jsonify({'error': str(e)}), 500


@transform_bp.route('/versions/<dataset_id>', methods=['GET'])
def list_versions(dataset_id):
    """List transformation version history for a dataset."""
    try:
        from datasets.registry import dataset_registry
        from db.repositories.transformation_repository import transformation_repository

        db = get_db()
        if db is None:
            return jsonify({'versions': [], 'note': 'PostgreSQL not available'}), 200

        entry = dataset_registry.get(dataset_id)
        original_db_id = None
        if entry:
            try:
                original_db_id = uuid.UUID(str(entry.dataset_id))
            except Exception:
                pass

        if original_db_id is None:
            try:
                original_db_id = uuid.UUID(dataset_id)
            except Exception:
                return jsonify({'error': 'Invalid dataset_id'}), 400

        versions = transformation_repository.get_versions_for_dataset(db, original_db_id)
        return jsonify({'versions': [{
            'version_id': str(v.id),
            'version_number': v.version_number,
            'operation_label': v.operation_label,
            'changed_cells': v.changed_cells_count,
            'created_at': v.created_at.isoformat() if v.created_at else None,
            'plan_id': str(v.plan_id) if v.plan_id else None,
        } for v in versions]}), 200
    except Exception as e:
        logger.error('[transform/versions] Error: %s', e, exc_info=True)
        return jsonify({'error': str(e)}), 500


@transform_bp.route('/export/<dataset_id>', methods=['GET'])
def export_dataset(dataset_id):
    """Export the current (possibly transformed) dataset as a streamed CSV download."""
    try:
        from datasets.registry import dataset_registry

        entry = dataset_registry.get(dataset_id)
        if entry is None or entry.dataframe is None:
            # Cold-worker fallback: restore from PostgreSQL
            try:
                from db.session import db_session
                from db.services.dataset_service import dataset_service
                with db_session() as db:
                    _, restored_df = dataset_service.restore_dataset_df(db, uuid.UUID(dataset_id), user_id=None)  # type: ignore[arg-type]
                    if restored_df is not None:
                        entry = dataset_registry.get(dataset_id)
            except Exception as restore_err:
                logger.warning('[transform/export] DB restore failed for %s: %s', dataset_id, restore_err)

        if entry is None or entry.dataframe is None:
            return jsonify({'error': 'Dataset not found'}), 404

        df = entry.dataframe
        filename = getattr(entry, 'filename', 'dataset.csv')
        if filename.lower().endswith('.csv'):
            export_name = filename[:-4] + '_transformed.csv'
        elif '.' in filename:
            export_name = filename.rsplit('.', 1)[0] + '_transformed.csv'
        else:
            export_name = f"{filename}_transformed.csv"

        total = len(df)
        estimated_bytes = 0
        if total > 0:
            sample_n = min(1000, total)
            sample_slice = df.iloc[:sample_n]
            header_str = df.iloc[:0].to_csv(index=False)
            header_bytes = len(header_str.encode('utf-8'))
            sample_body_bytes = len(sample_slice.to_csv(index=False, header=False).encode('utf-8'))
            avg_row_bytes = sample_body_bytes / sample_n
            estimated_bytes = header_bytes + int(avg_row_bytes * total)

        def _generate_csv():
            chunk_size = 15000
            if total == 0:
                yield df.to_csv(index=False)
                return
            for start in range(0, total, chunk_size):
                end = min(start + chunk_size, total)
                yield df.iloc[start:end].to_csv(index=False, header=(start == 0))

        return Response(
            stream_with_context(_generate_csv()),
            mimetype='text/csv',
            headers={
                'Content-Disposition': f'attachment; filename="{export_name}"',
                'X-Estimated-Bytes': str(estimated_bytes),
                'X-Total-Bytes': str(estimated_bytes),
                'X-Total-Rows': str(total),
                'Cache-Control': 'no-store',
                'Access-Control-Expose-Headers': 'Content-Disposition, Content-Length, X-Estimated-Bytes, X-Total-Bytes, X-Total-Rows',
            },
        )
    except Exception as e:
        logger.error('[transform/export] Error: %s', e, exc_info=True)
        return jsonify({'error': str(e)}), 500


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_operation_from_message(message: str, df) -> Optional[dict]:
    """Parse operation from message — delegates to master_router for consistency."""
    from router.master_router import _parse_transform_from_message
    return _parse_transform_from_message(message)


import math
import pandas as pd
def _is_nan(v):
    try:
        if v is None: return False
        if isinstance(v, float) and math.isnan(v): return True
        if pd.isna(v): return True
        return False
    except:
        return False

def _serialize_plan(plan) -> dict:
    return {
        'plan_id': plan.plan_id,
        'operation_type': plan.operation_type,
        'operation_label': plan.operation_label,
        'operation_code': plan.operation_code,
        'affected_rows_count': plan.affected_rows_count,
        'affected_cells_count': plan.affected_cells_count,
        'affected_columns': plan.affected_columns,
        'before_sample': [{
            'row_index': c.row_index,
            'source_row_number': c.source_row_number,
            'column': c.column,
            'before': None if _is_nan(c.before) else (c.before if c.before is None or isinstance(c.before, (int, float, bool, str)) else str(c.before)),
            'after': None if _is_nan(c.after) else (c.after if c.after is None or isinstance(c.after, (int, float, bool, str)) else str(c.after)),
        } for c in plan.before_sample],
        'warnings': plan.warnings,
        'created_at': plan.created_at,
        'intelligence_provenance': getattr(plan, 'intelligence_provenance', None),
    }


def _db_plan_to_contract(db_plan) -> Optional['TransformationPlan']:
    """Convert a DB TransformationPlan ORM object to a contracts.TransformationPlan."""
    try:
        from transformation.contracts import TransformationPlan, AffectedCell
        sample_data = db_plan.before_sample_json or []
        sample = [AffectedCell(
            row_index=r.get('row_index', 0),
            source_row_number=r.get('source_row_number', 1),
            column=r.get('column', ''),
            before=r.get('before'),
            after=r.get('after'),
        ) for r in sample_data]
        payload = db_plan.operation_payload or {}
        return TransformationPlan(
            plan_id=str(db_plan.id),
            dataset_id=str(db_plan.dataset_id),
            operation_type=db_plan.operation_type,
            operation_label=payload.get('label', db_plan.operation_type),
            operation_code=db_plan.operation_code or '',
            operation_payload=payload,
            affected_rows_count=db_plan.affected_rows_count,
            affected_cells_count=db_plan.affected_cells_count,
            affected_columns=payload.get('columns', []),
            before_sample=sample,
            dataset_fingerprint=db_plan.dataset_fingerprint or '',
            intelligence_provenance=payload.get('intelligence_provenance'),
            created_at=db_plan.created_at.isoformat() if db_plan.created_at else None,
        )
    except Exception as e:
        logger.warning('[_db_plan_to_contract] Failed: %s', e)
        return None

@transform_bp.route('/redo', methods=['POST'])
def redo_transformation():
    """
    Redo the next undone transformation / action for a dataset.
    """
    from db.session import get_db
    data = request.get_json(silent=True) or {}
    dataset_id = (data.get('dataset_id') or '').strip()
    if not dataset_id:
        return jsonify({'error': 'dataset_id is required'}), 400

    db = get_db()
    if db is None:
        return jsonify({'error': 'Database unavailable'}), 500

    try:
        from spreadsheet.persistence import spreadsheet_history_manager
        res = spreadsheet_history_manager.redo(db, dataset_id)
        status_code = 200 if res.get('success') else 404
        return jsonify(res), status_code
    except Exception as e:
        logger.error('[transform/redo] Error: %s', e, exc_info=True)
        return jsonify({'error': str(e)}), 500
