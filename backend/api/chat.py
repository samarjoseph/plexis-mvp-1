"""Chat API blueprint — RFC Architecture Evolution v1.2."""
import logging
from flask import Blueprint, request, jsonify

from core.pipeline import request_pipeline
from router.master_router import master_router
from router.engine_registry import engine_registry
from engines.conversation import conversation_engine
from engines.analysis import analysis_engine
from utils.response import success_response, error_response

logger = logging.getLogger(__name__)


def sanitize_for_json(obj):
    """
    Recursively converts non-JSON-serializable types (NumPy scalars/arrays, Pandas types,
    sets, NaN, Infinity) into native JSON-serializable Python structures.
    """
    if obj is None:
        return None
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, str):
        return obj
    if isinstance(obj, (int, float)):
        import math
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj

    try:
        import numpy as np
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj) if not np.isnan(obj) and not np.isinf(obj) else None
        if isinstance(obj, (np.bool_,)):
            return bool(obj)
        if isinstance(obj, np.ndarray):
            return [sanitize_for_json(item) for item in obj.tolist()]
    except ImportError:
        pass

    try:
        import pandas as pd
        if isinstance(obj, pd.Timestamp):
            return obj.isoformat()
        if isinstance(obj, pd.Series):
            return [sanitize_for_json(item) for item in obj.tolist()]
        if isinstance(obj, pd.DataFrame):
            return sanitize_for_json(obj.to_dict(orient="records"))
        if not hasattr(obj, '__iter__') and not isinstance(obj, (dict, list, tuple, set)) and pd.isna(obj):
            return None
    except Exception:
        pass

    if isinstance(obj, dict):
        return {str(k): sanitize_for_json(v) for k, v in obj.items()}

    if isinstance(obj, (list, tuple, set)):
        return [sanitize_for_json(item) for item in obj]

    import datetime
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return obj.isoformat()

    if hasattr(obj, "to_dict") and callable(getattr(obj, "to_dict")):
        try:
            return sanitize_for_json(obj.to_dict())
        except Exception:
            pass

    try:
        import json
        json.dumps(obj)
        return obj
    except (TypeError, OverflowError):
        return str(obj)



def _try_persist_messages(chat_id, user_message, assistant_answer, evidence_dict, ctx):
    """
    Persist user + assistant messages to PostgreSQL.
    Wrapped in try/except — DB errors NEVER affect the /api/ask response.
    """
    from config import Config
    if not Config.DATABASE_URL or not chat_id:
        return
    try:
        from db.session import db_session
        from db.repositories.user_repository import user_repository
        from db.repositories.message_repository import message_repository
        from db.repositories.chat_repository import chat_repository

        email = (request.headers.get('X-User-Email') or '').strip().lower() or 'dev@plexis.local'

        with db_session() as db:
            user = user_repository.get_or_create_by_email(db, email)
            import uuid as _uuid
            chat_uuid = _uuid.UUID(str(chat_id))

            # Verify ownership
            chat = chat_repository.get_by_id(db, chat_uuid, user.id)
            if not chat:
                logger.debug('[DB] chat_id=%s not found for user=%s — skipping message persist', chat_id, email)
                return

            # Persist user message
            message_repository.append_message(
                db, chat_uuid, role='user', content=user_message,
                message_type='conversation',
                metadata_json={'intent': getattr(ctx, 'intent', None)},
            )

            # Persist assistant message
            message_repository.append_message(
                db, chat_uuid, role='assistant', content=assistant_answer,
                message_type='conversation',
                metadata_json={
                    'evidence': evidence_dict,
                    'source': getattr(ctx, 'source', None),
                    'dataset_id': getattr(ctx, 'dataset_id', None),
                },
            )

            # Update chat's last_message_at
            chat_repository.update_last_message(db, chat_uuid)

            logger.debug('[DB] Messages persisted for chat=%s', chat_id)
    except Exception as e:
        logger.warning('[DB] Message persistence failed (non-fatal): %s', e)


def _build_response_actions(intent: str, result_block: dict | None = None) -> dict:
    """
    Determine which result actions are available based on intent and result data.

    Contextual action visibility rules:
      - Retry is always available.
      - Locate is ONLY available if the analytical result has actual rows to locate in spreadsheet.
      - Explain is not a noisy permanent default for self-explanatory summaries.
    """
    is_analytical = intent in (
        "analysis", "DATA_OPERATION", "data_operation",
        "dataset_analysis", "analytical",
    )
    has_rows_to_locate = False
    if is_analytical and result_block:
        has_rows_to_locate = bool(
            result_block.get("row_count", 0) > 0
            or (result_block.get("rows") and len(result_block["rows"]) > 0)
        )
    return {
        "retry": True,
        "explain": False,
        "locate": has_rows_to_locate,
    }


def _build_result_block(ctx) -> dict | None:
    """
    Build the structured result block for analytical responses.

    Reads from ctx.analytical_result if it was set by the analysis engine.
    Returns None for non-analytical responses.
    """
    analytical_result = getattr(ctx, "analytical_result", None)
    if analytical_result is None:
        return None

    try:
        value = getattr(analytical_result, "value", None)
        operation = getattr(analytical_result, "operation", None)
        column = getattr(analytical_result, "column", None)
        row_indices = getattr(analytical_result, "row_indices", []) or []
        matching_rows = getattr(analytical_result, "matching_rows", []) or []
        verified = getattr(analytical_result, "verified", False)

        # Determine result type
        if isinstance(value, (int, float)) and matching_rows:
            result_type = "scalar_with_rows"
        elif isinstance(value, list):
            result_type = "list"
        elif isinstance(value, dict):
            result_type = "grouped"
        else:
            result_type = "scalar"

        details = dict(getattr(analytical_result, "details", {}) or {})
        ranking = None
        if (
            isinstance(value, dict)
            and str(operation or "").lower().startswith("group_")
            and details.get("top_n")
        ):
            # JSON object key ordering is not a ranking contract. Keep one
            # authoritative ordered list through API serialization and UI.
            ordered_entries = details.get("ordered_entries")
            if not isinstance(ordered_entries, list):
                ordered_entries = [
                    {"entity": str(entity), "value": metric, "rank": rank}
                    for rank, (entity, metric) in enumerate(value.items(), start=1)
                ]
            details["ordered_entries"] = ordered_entries  # legacy consumers
            details["requested_count"] = int(details.get("top_n") or 0)
            details["computed_count"] = len(ordered_entries)
            details["serialized_count"] = len(ordered_entries)
            op_name = str(operation).lower()
            if details.get("filter_desc"):
                metric_label = f"count of {details['filter_desc']}"
            elif "mean" in op_name:
                metric_label = f"average {details.get('value_column') or column or 'value'}"
            elif "max" in op_name:
                metric_label = f"maximum {details.get('value_column') or column or 'value'}"
            elif "min" in op_name:
                metric_label = f"minimum {details.get('value_column') or column or 'value'}"
            elif "median" in op_name:
                metric_label = f"median {details.get('value_column') or column or 'value'}"
            elif "sum" in op_name:
                metric_label = f"total {details.get('value_column') or column or 'value'}"
            else:
                metric_label = "entity count"
            ranking = {
                "entity_column": details.get("group_by") or column,
                "metric_column": details.get("value_column"),
                "metric_label": metric_label,
                "aggregation": op_name.removeprefix("group_"),
                "direction": "asc" if str(details.get("order", "desc")).lower() in ("asc", "ascending") else "desc",
                "requested_limit": int(details.get("top_n") or 0),
                "tie_policy": details.get("tie_policy", "metric_value_then_entity_ascending"),
                "eligible_entity_count": int(details.get("eligible_group_count", details.get("total_groups", len(ordered_entries))) or 0),
                "computed_count": len(ordered_entries),
                "serialized_count": len(ordered_entries),
                "items": ordered_entries,
                "dataset_id": str(getattr(ctx, "dataset_id", "")) or None,
                "dataset_fingerprint": getattr(analytical_result, "dataset_fingerprint", None),
            }
        is_ranked_rows = str(operation or "").lower() in ("top_n", "bottom_n")
        rows_to_present = matching_rows if is_ranked_rows else matching_rows[:10]

        # Preserve every requested ranked row; non-ranking evidence keeps its
        # compact preview. Distinguish source row, dataframe index, and rank.
        rows = []
        for i, row in enumerate(rows_to_present):
            clean = {k: v for k, v in row.items() if not k.startswith("_")}
            source_row_number = row.get("source_row_number") or row.get("_row_number")
            if source_row_number is None:
                df_idx = row_indices[i] if i < len(row_indices) else None
                source_row_number = (int(df_idx) + 1) if df_idx is not None else None
            df_index = row.get("dataframe_index")
            if df_index is None:
                df_index = row_indices[i] if i < len(row_indices) else (source_row_number - 1 if source_row_number is not None else None)
            result_rank = row.get("result_rank", i + 1)

            clean["source_row_number"] = source_row_number
            clean["dataframe_index"] = df_index
            clean["result_rank"] = result_rank
            rows.append(clean)

        from analytics.context_enrichment import extract_relevant_context
        context_record = extract_relevant_context(rows[0], target_col=column, max_fields=8) if rows else {}
        total_matching = details.get("matching_count")
        is_extremum = str(operation).lower() in ("max", "min", "idxmax", "idxmin", "longest", "shortest", "highest", "lowest")
        tie_count = total_matching if total_matching is not None else ((len(rows) if rows else 0) if is_extremum else 0)
        is_tied = bool(is_extremum and tie_count > 1)

        # Grounded unit from metadata or explicit None (unknown)
        unit = details.get("unit") or None
        unit_status = details.get("unit_status") or ("known" if unit else "unknown")
        unit_label = details.get("unit_label") or (unit if unit else "Unit not specified in dataset metadata")
        unit_provenance = details.get("unit_provenance")

        return {
            "type": result_type,
            "value": value,
            "unit": unit,
            "unit_status": unit_status,
            "unit_label": unit_label,
            "unit_provenance": unit_provenance,
            "operation": operation,
            "column": column,
            "details": details,
            "ranking": ranking,
            "row_count": len(row_indices),
            "verified": verified,
            "rows": rows,
            "context_record": context_record,
            "tie_count": tie_count,
            "is_tied": is_tied,
            "dataset_id": str(getattr(ctx, "dataset_id", "")) or None,
            "dataset_fingerprint": getattr(analytical_result, "dataset_fingerprint", None),
        }
    except Exception as e:
        logger.warning("[chat] _build_result_block failed: %s", e)
        return None


chat_bp = Blueprint('chat', __name__, url_prefix='/api')

# Register engines on module load
engine_registry.register(conversation_engine, ['conversation', 'title_generation', 'help_system'])
engine_registry.register(analysis_engine, ['analysis'])
# web_search intent currently has no engine — router falls back to conversation

# Register transformation and investigation engines
try:
    from engines.transform import transform_engine
    from engines.investigate import investigate_engine
    engine_registry.register(transform_engine, ['transform'])
    engine_registry.register(investigate_engine, ['investigate'])
except ImportError as e:
    logger.warning("[chat] Transform/Investigate engines not loaded: %s", e)

@chat_bp.route('/ask', methods=['POST'])
def ask():
    """
    Main ask endpoint — the heart of Plexis.

    Request body (JSON):
      message          (str, required)
      dataset_id       (str, optional)
      session_id       (str, optional) — frontend conversation ID
      workspace_state  (dict, optional) — current DatasetWorkspace state snapshot
      workspace_action (dict, optional) — explicit workspace action (e.g. explain_row)

    Response:
      Standard success_response plus:
        evidence         (dict | null)   — EvidenceReference or EvidenceCollection if produced
        analysis_id      (str | null)    — UUID of the persisted AnalysisOperation
        response_actions (dict)          — {retry, explain, locate} availability flags
        result           (dict | null)   — structured analytical result block
    """
    data = request.get_json(silent=True) or {}
    message = data.get('message', '').strip() if isinstance(data.get('message'), str) else ''
    dataset_id = data.get('dataset_id', '') or ''
    chat_id = data.get('chat_id') or None
    session_id = data.get('session_id') or request.headers.get('X-Session-Id')
    workspace_state = data.get('workspace_state') or None
    workspace_action = data.get('workspace_action') or None

    if not message and not workspace_action:
        err_dict, status = error_response(message="Message cannot be empty", status_code=400)
        return jsonify(err_dict), status
    if not message and workspace_action:
        message = str(workspace_action.get("type") or "workspace_action")

    try:
        # 1. Run request through the pipeline (now workspace-aware + chat-scoped L2 restore)
        ctx = request_pipeline.process(
            message=message,
            dataset_id=dataset_id if dataset_id else None,
            session_id=session_id,
            workspace_state=workspace_state,
            workspace_action=workspace_action,
            chat_id=chat_id,    # enables chat-scoped dataset restore on L1 cache miss
        )

        # 1b. Check if dataset metadata exists but data access failed (recoverable error)
        if getattr(ctx, 'dataset_access_failed', False):
            err_msg = getattr(ctx, 'dataset_resolution_error', None) or (
                f"Dataset '{getattr(ctx, 'dataset_filename', 'active dataset')}' is recognized, but its underlying data "
                "could not be loaded from storage. Please re-upload or select the dataset again."
            )
            logger.warning("[ChatAPI] Returning recoverable dataset storage error: %s", err_msg)
            return success_response(
                answer=err_msg,
                intent="conversation",
                confidence=1.0,
                session_id=ctx.session_id,
                execution_class="Class_C",
                error=err_msg,
            )

        # 2. Route to the correct engine
        result = master_router.route(ctx)

        # 3. Compose final response using ResponseComposer
        comp_stage = ctx.start_stage('composition')
        from core.composer import response_composer
        final_result = response_composer.compose(ctx, result)
        ctx.complete_stage(comp_stage)

        # 3b. Strict final sanitization and response boundary against chain-of-thought and prompt leakage
        from utils.response_boundary import enforce_response_boundary
        if final_result and final_result.answer:
            final_result.answer = enforce_response_boundary(final_result.answer, ctx)

        # 4. Record in conversation memory
        request_pipeline.record_response(ctx, message, final_result.answer)

        # 5. Serialize evidence (EvidenceReference or EvidenceCollection → dict)
        evidence_dict = None
        if final_result.evidence is not None:
            try:
                if isinstance(final_result.evidence, list):
                    evidence_dict = [e.to_dict() if hasattr(e, "to_dict") else e for e in final_result.evidence]
                else:
                    evidence_dict = final_result.evidence.to_dict() if hasattr(final_result.evidence, "to_dict") else final_result.evidence
            except Exception as ev_err:
                logger.warning(f"Evidence serialization failed: {ev_err}")

        if evidence_dict and isinstance(final_result.metadata, dict):
            v_info = final_result.metadata.get('verification', {})
            if v_info.get('status') == 'FAIL':
                if isinstance(evidence_dict, dict):
                    evidence_dict['verified'] = False
                elif isinstance(evidence_dict, list):
                    for ev_item in evidence_dict:
                        if isinstance(ev_item, dict):
                            ev_item['verified'] = False

        # 6. Build analysis metadata
        analysis_id = getattr(ctx, "analysis_id", None)
        intent = getattr(ctx, "intent", "conversation") or "conversation"
        result_block = _build_result_block(ctx)
        response_actions = _build_response_actions(intent, result_block)

        # 6b. Finalize stage timings and execution class
        stage_timings = ctx.finalize_timings()
        execution_class = getattr(ctx, "execution_class", "Class_C")

        # 7. Build response
        response = success_response(
            answer=final_result.answer,
            source=final_result.source,
            provider=final_result.provider,
            dataset_info=final_result.dataset_info,
            chart_data=final_result.chart_data,
        )

        # Attach execution class and latency profile
        response['execution_class'] = execution_class
        response['stage_timings'] = stage_timings

        # Attach evidence to response (null if none produced)
        response['evidence'] = evidence_dict

        # Attach analytical result metadata
        response['analysis_id'] = analysis_id
        response['response_actions'] = response_actions
        response['result'] = result_block
        response['verification'] = final_result.metadata.get('verification')

        # Attach transformation plan (from TransformEngine) — null for non-transform responses
        transform_plan = None
        transformation_clarification = None
        transformation_update = None
        trigger_investigation = False
        if final_result.metadata:
            transform_plan = final_result.metadata.get('transform_plan')
            transformation_clarification = final_result.metadata.get('clarification')
            transformation_update = final_result.metadata.get('transformation_update')
            trigger_investigation = bool(final_result.metadata.get('trigger_investigation', False))
        response['transform_plan'] = transform_plan
        response['transformation_clarification'] = transformation_clarification
        response['transformation_update'] = transformation_update
        response['trigger_investigation'] = trigger_investigation

        # Expose chat_id to frontend — may have been auto-created by the
        # analytics pipeline to satisfy FK constraints on analysis_sessions
        resolved_chat_id = getattr(ctx, 'chat_id', None) or chat_id
        response['chat_id'] = str(resolved_chat_id) if resolved_chat_id else None

        # Add pipeline trace metadata for debugging
        response['_debug'] = {
            'intent': ctx.intent,
            'confidence': ctx.confidence,
            'execution_class': execution_class,
            'stage_timings': stage_timings,
            'session_id': ctx.session_id,
            'pipeline_ms': round(ctx.total_pipeline_ms(), 2),
            'trace': ctx.trace_summary(),
            'capability': final_result.metadata.get('capability') if final_result.metadata else None,
            'workspace_summary': ctx.workspace_summary or None,
        }

        # Persist messages to DB (non-blocking — errors never break response)
        _try_persist_messages(chat_id, message, final_result.answer, evidence_dict, ctx)

        # Register chat_id in ConversationMemory for L2 DB fallback
        # This allows get_history() to reconstruct from PostgreSQL on server restart
        if chat_id and ctx.session_id:
            try:
                from memory.conversation import conversation_memory
                conversation_memory.register_chat_id(ctx.session_id, str(chat_id))
            except Exception:
                pass  # Non-critical


        return jsonify(sanitize_for_json(response))

    except Exception as e:
        logger.error(f"Ask endpoint error: {e}", exc_info=True)
        err_dict, status = error_response(message=f"An error occurred: {str(e)}", status_code=500)
        return jsonify(err_dict), status


@chat_bp.route('/title', methods=['POST'])
@chat_bp.route('/chat/title', methods=['POST'])
def generate_title_endpoint():
    """
    Fast endpoint for initial conversation naming using a small, lightweight LLM.
    Generates a natural 2-5 word title based on the user's initial message
    and/or uploaded dataset filename.
    """
    import re
    import os
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    filename = (data.get("filename") or "").strip()

    if not message and not filename:
        return jsonify({"title": "New Chat", "status": "ok"})

    from providers.domain.contracts import AIRequest
    from providers.engine import provider_engine

    if filename and message:
        base_name = os.path.splitext(filename)[0].replace('_', ' ').replace('-', ' ').title()
        prompt = (
            f'A user uploaded a spreadsheet dataset named "{filename}" ({base_name}) and asked: "{message[:200]}". '
            'Create a short, natural, professional conversation title in 2 to 4 words summarizing this session (for example "Flight Delay Analysis" or "Q3 Sales Overview"). '
            'Do NOT include file extensions like .csv or .xlsx in the title. '
            'Return ONLY the title text inside double quotes.'
        )
    elif filename:
        base_name = os.path.splitext(filename)[0].replace('_', ' ').replace('-', ' ').title()
        prompt = (
            f'A user uploaded a spreadsheet dataset named "{filename}" ({base_name}). '
            'Create a short, natural, professional conversation title in 2 to 4 words summarizing this dataset session (for example "Sales Performance Report" or "Customer Churn Analysis"). '
            'Do NOT include file extensions like .csv or .xlsx in the title. '
            'Return ONLY the title text inside double quotes.'
        )
    else:
        prompt = (
            f'Message: "{message[:200]}". '
            'Generate a concise, natural conversation title in 2 to 5 words capturing the subject. '
            'Return ONLY the title text inside double quotes, e.g. "SaaS Revenue Trends".'
        )

    try:
        response = provider_engine.generate(
            AIRequest(
                task='title',
                messages=[{"role": "user", "content": prompt}],
                system_prompt="You generate concise 2-5 word conversation titles. Put the title inside double quotes.",
                temperature=0.3,
                max_tokens=40,
            )
        )
        if response.success and response.text:
            raw_text = response.text.strip()
            # Extract quoted text if present
            quoted = re.findall(r'["\']([^"\']{2,40})["\']', raw_text)
            if quoted and len(quoted[-1].split()) <= 7:
                cleaned = quoted[-1].strip()
            else:
                lines = [l.strip() for l in raw_text.split('\n') if l.strip()]
                last_line = lines[-1] if lines else raw_text
                last_line = re.sub(r'^(title|so something like|suggested title|here is a title):\s*', '', last_line, flags=re.I).strip(' "\'#*')
                words = last_line.split()
                cleaned = " ".join(words[:5]) if len(words) > 5 else last_line

            if cleaned:
                cleaned = re.sub(r'\.(csv|xlsx|xls)$', '', cleaned, flags=re.I).strip()
                return jsonify({"title": cleaned, "status": "ok"})
    except Exception as e:
        logger.warning("[chat] generate_title_endpoint failed: %s", e)

    # Fallback if LLM fails
    if filename:
        name_part = os.path.splitext(filename)[0].replace('_', ' ').replace('-', ' ').title()
        return jsonify({"title": f"{name_part[:25]} Analysis", "status": "fallback"})
    elif message:
        words = message.split()[:4]
        return jsonify({"title": " ".join(words).title(), "status": "fallback"})

    return jsonify({"title": "New Chat", "status": "default"})
