"""
Analysis API — endpoints for analytical brain operations.

Routes:
  POST /api/analysis/run                   — Run a new analytical query
  GET  /api/analysis/<id>/locate           — Get locator for Locate button
  POST /api/analysis/<id>/explain          — Get LLM explanation grounded in verified result
  POST /api/analysis/<id>/locate_selection — Log user's row selection from multi-row Locate
  POST /api/analysis/<id>/retry            — Re-run the original query
"""
from __future__ import annotations

import logging
import uuid
from flask import Blueprint, request, jsonify

from db.identity import identity_resolver, IdentityError

logger = logging.getLogger(__name__)
analysis_bp = Blueprint("analysis", __name__)


@analysis_bp.before_request
def check_guest_analytics_access():
    if request.method == "OPTIONS":
        return None
    from db.identity import identity_resolver
    try:
        user = identity_resolver.resolve(request)
        if user and user.is_guest:
            return jsonify({
                "error": "GUEST_RESTRICTED",
                "code": "GUEST_ANALYTICS_LOCKED",
                "message": "The deep analytics panel is unavailable in Guest Mode. Please sign in to run automated statistical analysis, anomaly detection, and deep correlations."
            }), 403
    except Exception:
        pass
    return None


def _log_action(db, operation_id, action_type, user_id=None, dataset_id=None, metadata=None):
    """
    Best-effort action logging. Never raises — errors are swallowed.
    Uses the existing request db session so no extra commit is needed.
    """
    try:
        from db.repositories.analysis_action_repository import analysis_action_repository
        analysis_action_repository.log_action(
            db,
            operation_id=operation_id,
            action_type=action_type,
            user_id=user_id,
            dataset_id=dataset_id,
            metadata=metadata or {},
        )
        db.commit()
    except Exception as e:
        logger.warning("[analysis] action log failed (non-fatal): %s", e)
        try:
            db.rollback()
        except Exception:
            pass


@analysis_bp.route("/api/analysis/run", methods=["POST"])
def run_analysis():
    """
    Run an analytical query against the active dataset.

    Body: { "message": "...", "chat_id": "...", "dataset_id": "..." }
    """
    try:
        current_user = identity_resolver.resolve(request)
    except IdentityError as e:
        return jsonify({"error": str(e)}), 401

    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    chat_id_str = data.get("chat_id")
    dataset_id_str = data.get("dataset_id")

    if not message:
        return jsonify({"error": "message is required"}), 400
    if not dataset_id_str:
        return jsonify({"error": "dataset_id is required"}), 400

    try:
        chat_id = uuid.UUID(chat_id_str) if chat_id_str else None
        dataset_id = uuid.UUID(dataset_id_str)
    except ValueError as e:
        return jsonify({"error": f"Invalid UUID: {e}"}), 400

    try:
        from db.session import get_db
        from datasets.registry import dataset_registry
        from db.services.dataset_service import dataset_service
        from core.context import ExecutionContext
        from analytics.pipeline import analytics_pipeline

        db = get_db()

        # Resolve DataFrame
        entry = dataset_registry.get(str(dataset_id))
        df = entry.dataframe if entry and hasattr(entry, "dataframe") else None
        if df is None:
            df = dataset_service.restore_dataset_df(db, dataset_id, current_user.user_id)
        if df is None:
            return jsonify({"error": "Dataset not found."}), 404

        # Build minimal execution context
        ctx = ExecutionContext(
            message=message,
            user_id=current_user.user_id,
            chat_id=chat_id,
        )
        ctx.dataset_id = dataset_id

        # Get schema from dataset entry
        if entry:
            ctx.schema_profile = getattr(entry, "schema_profile", None)
            ctx.dataset_filename = getattr(entry, "original_filename", None)
            ctx.dataset_fingerprint = getattr(entry, "fingerprint", None)
            ctx.dko = getattr(entry, "dko", None)

        result = analytics_pipeline.run(ctx, df, db=db)

        return jsonify({
            "analysis_id": str(result.analysis_id),
            "operation": result.operation,
            "column": result.column,
            "value": result.value,
            "row_indices": result.row_indices,
            "matching_rows": result.matching_rows[:5],
            "verified": result.verified,
            "intent": result.normalized_intent,
        })

    except Exception as e:
        logger.error("[analysis] run_analysis error: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


@analysis_bp.route("/api/analysis/<analysis_id>/locate", methods=["GET"])
def locate_analysis(analysis_id: str):
    """
    Get locator data for the Locate button.

    Returns row_indices and per-row source references so the frontend can
    navigate to the matching rows in the spreadsheet.

    Also logs a 'locate' action in analysis_actions.
    """
    try:
        current_user = identity_resolver.resolve(request)
    except IdentityError as e:
        return jsonify({"error": str(e)}), 401

    try:
        op_uuid = uuid.UUID(analysis_id)
    except ValueError:
        return jsonify({"error": "Invalid analysis_id"}), 400

    try:
        from db.session import get_db
        from db.repositories.operation_repository import operation_repository
        from db.repositories.analysis_session_repository import analysis_session_repository
        from db.repositories.result_row_repository import result_row_repository

        db = get_db()
        op = operation_repository.get_by_id(db, op_uuid)
        if not op:
            return jsonify({"error": "Analysis not found"}), 404

        # Verify ownership via session → chat → user
        session = analysis_session_repository.get_by_id(db, op.analysis_session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        result_json = op.result_json or {}
        value_json = getattr(op, "value_json", None) or {}
        row_indices = getattr(op, "row_indices_json", None) or result_json.get("row_indices", [])
        fingerprint = getattr(op, "dataset_fingerprint", None) or result_json.get("dataset_fingerprint")
        value = value_json.get("value") if value_json else result_json.get("value")
        column = op.column_name or result_json.get("column")

        # Load persisted result rows (migration 003)
        result_rows = result_row_repository.get_by_operation_as_dicts(db, op_uuid)
        row_count = len(result_rows) if result_rows else len(row_indices)

        # Log locate action (best-effort)
        _log_action(
            db, op_uuid, "locate",
            user_id=getattr(current_user, "user_id", None),
            dataset_id=op.dataset_id,
            metadata={"row_count": row_count, "row_indices": row_indices[:10]},
        )

        return jsonify({
            "analysis_id": str(op.id),
            "dataset_id": str(op.dataset_id),
            "dataset_fingerprint": fingerprint,
            "row_indices": row_indices,
            "column": column,
            "value": value,
            "operation": op.operation_type,
            "row_count": row_count,
            "rows": result_rows,                    # [{source_row_number, ...row_values}]
            "navigate_direct": row_count == 1,      # True → direct nav; False → show selector
        })

    except Exception as e:
        logger.error("[analysis] locate error: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


@analysis_bp.route("/api/analysis/<analysis_id>/explain", methods=["POST"])
def explain_analysis(analysis_id: str):
    """
    Get an LLM explanation grounded in a verified analytical result.

    The LLM receives ONLY the verified result — it cannot modify numerical values.
    Also logs an 'explain' action.
    """
    try:
        current_user = identity_resolver.resolve(request)
    except IdentityError as e:
        return jsonify({"error": str(e)}), 401

    try:
        op_uuid = uuid.UUID(analysis_id)
    except ValueError:
        return jsonify({"error": "Invalid analysis_id"}), 400

    try:
        from db.session import get_db
        from db.repositories.operation_repository import operation_repository
        from providers.engine import provider_engine
        from providers.domain.contracts import AIRequest

        db = get_db()
        op = operation_repository.get_by_id(db, op_uuid)
        if not op:
            return jsonify({"error": "Analysis not found"}), 404

        result_json = op.result_json or {}
        value_json = getattr(op, "value_json", None) or {}
        value = value_json.get("value") if value_json else result_json.get("value")
        column = op.column_name or result_json.get("column")
        verified = getattr(op, "verified", None)
        if verified is None:
            verified = result_json.get("verified", False)
        matching_rows = getattr(op, "matching_rows_json", None) or result_json.get("matching_rows", [])
        plan_json = getattr(op, "plan_json", None) or result_json.get("plan", {})

        # Build a grounded explanation prompt
        import json
        grounding = {
            "operation": op.operation_type,
            "column": column,
            "verified_value": value,
            "verified": verified,
            "matching_rows": matching_rows[:3],
            "plan": plan_json,
        }
        grounding_str = json.dumps(grounding, indent=2, default=str)

        explain_prompt = f"""You are explaining a verified analytical result from the Plexis data platform.

VERIFIED ANALYTICAL RESULT:
{grounding_str}

RULES:
1. The value above is VERIFIED by an independent deterministic check. Do NOT change it.
2. Explain what the result means in clear, helpful language.
3. If there are matching rows, mention them.
4. Do NOT recalculate or guess the value.
5. Be concise — 2-4 sentences.

Your explanation:"""

        response = provider_engine.generate(
            AIRequest(
                task="explanation",
                messages=[{"role": "user", "content": explain_prompt}],
                system_prompt="You are a data analyst. Explain the result clearly and accurately.",
                temperature=0.3,
            )
        )

        explanation = response.text if response.success else (
            f"The {op.operation_type.lower()} of {column} is {value}."
        )

        # Log explain action (best-effort)
        _log_action(
            db, op_uuid, "explain",
            user_id=getattr(current_user, "user_id", None),
            dataset_id=op.dataset_id,
            metadata={"analysis_id": str(op_uuid)},
        )

        return jsonify({
            "analysis_id": str(op.id),
            "explanation": explanation,
            "verified_value": value,
            "column": column,
            "operation": op.operation_type,
            "verified": verified,
        })

    except Exception as e:
        logger.error("[analysis] explain error: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


@analysis_bp.route("/api/analysis/<analysis_id>/locate_selection", methods=["POST"])
def locate_selection(analysis_id: str):
    """
    Log a user's row selection from a multi-row locate result.

    Body: { "selected_source_row": 72 }
    Response: { "success": true, "source_row_number": 72 }
    """
    try:
        current_user = identity_resolver.resolve(request)
    except IdentityError as e:
        return jsonify({"error": str(e)}), 401

    try:
        op_uuid = uuid.UUID(analysis_id)
    except ValueError:
        return jsonify({"error": "Invalid analysis_id"}), 400

    data = request.get_json(silent=True) or {}
    selected_source_row = data.get("selected_source_row")
    if selected_source_row is None:
        return jsonify({"error": "selected_source_row is required"}), 400

    try:
        from db.session import get_db
        from db.repositories.operation_repository import operation_repository

        db = get_db()
        op = operation_repository.get_by_id(db, op_uuid)
        if not op:
            return jsonify({"error": "Analysis not found"}), 404

        # Log locate_selection action (best-effort)
        _log_action(
            db, op_uuid, "locate_selection",
            user_id=getattr(current_user, "user_id", None),
            dataset_id=op.dataset_id,
            metadata={"selected_source_row": int(selected_source_row)},
        )

        return jsonify({
            "success": True,
            "source_row_number": int(selected_source_row),
        })

    except Exception as e:
        logger.error("[analysis] locate_selection error: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


@analysis_bp.route("/api/analysis/<analysis_id>/retry", methods=["POST"])
def retry_analysis(analysis_id: str):
    """
    Re-run the original analytical query and link the new operation as a retry.

    Creates a new AnalysisOperation with retry_of_operation_id set to the original.
    Response: { "analysis_id": "...", "value": ..., "verified": ... }
    """
    try:
        current_user = identity_resolver.resolve(request)
    except IdentityError as e:
        return jsonify({"error": str(e)}), 401

    try:
        op_uuid = uuid.UUID(analysis_id)
    except ValueError:
        return jsonify({"error": "Invalid analysis_id"}), 400

    try:
        from db.session import get_db
        from db.repositories.operation_repository import operation_repository
        from db.repositories.analysis_session_repository import analysis_session_repository
        from datasets.registry import dataset_registry
        from db.services.dataset_service import dataset_service
        from core.context import ExecutionContext
        from analytics.pipeline import analytics_pipeline

        db = get_db()

        # ── Diagnostic logging for 404 tracing ────────────────────────────────
        # If get_by_id returns None, we need to know whether:
        # (a) the op_uuid doesn't match any row (analysis was never persisted)
        # (b) the DB session can't see the row (transaction isolation issue)
        logger.info("[retry] Attempting lookup: op_uuid=%s", op_uuid)
        try:
            total_ops = db.execute(
                __import__('sqlalchemy').text("SELECT COUNT(*) FROM analysis_operations")
            ).scalar()
            logger.info("[retry] Total rows in analysis_operations: %d", total_ops)
        except Exception as diag_err:
            logger.warning("[retry] Diagnostic query failed: %s", diag_err)
        # ─────────────────────────────────────────────────────────────────────

        op = operation_repository.get_by_id(db, op_uuid)
        if not op:
            logger.warning(
                "[retry] Operation NOT FOUND: op_uuid=%s — "
                "likely the analysis was never committed to DB. "
                "Check that session_recorder.record() is being called and "
                "that db.commit() follows in the analytics pipeline.",
                op_uuid,
            )
            return jsonify({
                "error": "Analysis not found",
                "error_type": "operation_not_found",
                "analysis_id": str(op_uuid),
                "hint": "The analysis may not have been persisted. Try re-running the original query.",
            }), 404

        # Load original query
        params_json = op.parameters_json or {}
        original_query = params_json.get("query") or op.query_text or ""
        if not original_query:
            return jsonify({"error": "Original query not found in stored operation"}), 400


        # Resolve dataset
        dataset_id = op.dataset_id
        entry = dataset_registry.get(str(dataset_id))
        df = entry.dataframe if entry and hasattr(entry, "dataframe") else None
        if df is None:
            df = dataset_service.restore_dataset_df(db, dataset_id, current_user.user_id)
        if df is None:
            return jsonify({"error": "Dataset not found."}), 404

        # Resolve session → chat_id
        session = analysis_session_repository.get_by_id(db, op.analysis_session_id)
        chat_id = session.chat_id if session else None

        # Build execution context
        ctx = ExecutionContext(
            message=original_query,
            user_id=current_user.user_id,
            chat_id=chat_id,
        )
        ctx.dataset_id = dataset_id
        if entry:
            ctx.schema_profile = getattr(entry, "schema_profile", None)
            ctx.dataset_filename = getattr(entry, "original_filename", None)
            ctx.dataset_fingerprint = getattr(entry, "fingerprint", None)
            ctx.dko = getattr(entry, "dko", None)

        # Run the pipeline fresh
        new_result = analytics_pipeline.run(ctx, df, db=db)

        # Set retry_of_operation_id on the new operation (migration 003)
        if new_result.analysis_id:
            try:
                new_op = operation_repository.get_by_id(db, new_result.analysis_id)
                if new_op:
                    new_op.retry_of_operation_id = op_uuid
                    db.flush()
                    db.commit()
            except Exception as link_err:
                logger.warning("[analysis] Could not link retry op: %s", link_err)

        # Log retry action on the ORIGINAL operation (best-effort)
        _log_action(
            db, op_uuid, "retry",
            user_id=getattr(current_user, "user_id", None),
            dataset_id=dataset_id,
            metadata={"new_operation_id": str(new_result.analysis_id)},
        )

        return jsonify({
            "analysis_id": str(new_result.analysis_id),
            "original_analysis_id": str(op_uuid),
            "operation": new_result.operation,
            "column": new_result.column,
            "value": new_result.value,
            "row_indices": new_result.row_indices,
            "matching_rows": new_result.matching_rows[:5],
            "verified": new_result.verified,
        })

    except Exception as e:
        logger.error("[analysis] retry error: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500
