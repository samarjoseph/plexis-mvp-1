"""
Datasets API — RFC-002 v2.0

Changes from v1.0:
  - Upload flow now wires KnowledgeDecomposer + SemanticImportanceRanker + ModuleRegistry
  - SSE emits: analyzing → module_ready (per module) → chunk (narrative) → done
  - done event includes: dataset_id + available_modules list
  - /datasets/active routes REPLACED with explicit /datasets/{dataset_id} routes
    (audit fix C-1: multi-tab safe, no session collision)
  - New module endpoints:
      GET  /api/datasets/{dataset_id}/modules
      GET  /api/datasets/{dataset_id}/modules/{module_id}
      POST /api/datasets/{dataset_id}/modules/{module_id}/explain  (SSE)
"""

import json
import uuid
import logging
from datetime import datetime, timezone
from typing import Tuple, Any
import pandas as pd

from flask import Blueprint, Response, request, jsonify, stream_with_context

from datasets.storage import dataset_storage
from datasets.loader import dataset_loader
from datasets.fingerprint import file_fingerprint
from datasets.profiler import dataset_profiler
from datasets.registry import dataset_registry, DatasetEntry
from dataset_intelligence import dataset_intelligence_engine
from core.events import (
    event_bus, DatasetUploadedEvent, DatasetLoadedEvent,
    DatasetIntelligenceCompleteEvent, DatasetProfiledEvent
)
from core.errors import ValidationError
from config import Config
from utils.response import success_response, error_response

# Presentation layer
from presentation import (
    knowledge_decomposer,
    importance_ranker,
    module_registry,
    presentation_engine,
    module_explainer,
    session_manager,
)
from presentation.presentation_engine import SOURCE_LLM, SOURCE_FALLBACK
from presentation.domain.errors import BundleExpiredError, ModuleNotAvailableError

logger = logging.getLogger(__name__)

datasets_bp = Blueprint('datasets', __name__, url_prefix='/api')

ALLOWED_EXTENSIONS = {'.csv', '.xlsx', '.xls'}

# ---------------------------------------------------------------------------
# SSE Streaming Helpers
# ---------------------------------------------------------------------------

def _sse(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"

def _sse_chunk(text: str) -> str:
    return _sse("chunk", {"text": text})

def _sse_done(data: dict) -> str:
    return _sse("done", data)

def _sse_error(message: str) -> str:
    return _sse("error", {"message": message})

def _sse_analyzing(status: str) -> str:
    return _sse("analyzing", {"status": status})

def _sse_module_ready(module_id: str, display_name: str, richness: float, preview: str) -> str:
    return _sse("module_ready", {
        "module_id": module_id,
        "display_name": display_name,
        "richness_score": richness,
        "preview": preview,
    })


# DB persistence layer (gracefully degraded if DATABASE_URL not set)
def _try_persist_dataset_to_db(raw_bytes: bytes, filename: str, dataset_id: str, dko, df, chat_id=None, summary_text=""):
    """
    Persist dataset to PostgreSQL after successful analysis and bind to user's chat.
    Also persists the user's file card and the assistant's upload summary message
    so chat history never vanishes upon reload or device sync.
    """
    from config import Config
    if not Config.DATABASE_URL:
        return None, str(chat_id) if chat_id else None, None

    try:
        from db.session import db_session
        from db.services.dataset_service import dataset_service
        from db.services.chat_service import chat_service
        from db.repositories.chat_repository import chat_repository
        from db.repositories.message_repository import message_repository
        from db.isolation import compute_account_tag, compute_chat_tag

        email = (request.headers.get('X-User-Email') or '').strip().lower()
        if not email:
            email = 'dev@plexis.local'

        import io as _io
        with db_session() as db:
            from db.repositories.user_repository import user_repository
            user = user_repository.get_or_create_by_email(db, email)

            file_like = _io.BytesIO(raw_bytes)
            file_like.name = filename
            db_dataset = dataset_service.persist_dataset(
                db, user.id, file_like, filename
            )

            db_dataset_id = str(db_dataset.id)
            dataset_registry.rekey(dataset_id, db_dataset_id)

            schema_json = {
                'columns': [{'name': c, 'dtype': str(df[c].dtype)} for c in df.columns]
            } if df is not None else None
            dataset_service.update_metadata(
                db, db_dataset.id, user.id,
                schema_json=schema_json,
                row_count=len(df) if df is not None else None,
                column_count=len(df.columns) if df is not None else None,
            )

            if dko is not None:
                dataset_service.update_dko(db, db_dataset.id, user.id, dko)

            # Resolve or auto-create chat
            resolved_chat = None
            if chat_id:
                try:
                    import uuid as _uuid
                    chat_uuid = _uuid.UUID(str(chat_id))
                    resolved_chat = chat_repository.get_by_id(db, chat_uuid, user.id)
                except Exception:
                    resolved_chat = chat_repository.get_by_slug(db, str(chat_id), user.id)

            if not resolved_chat:
                chat_title = f"Dataset: {filename}"
                resolved_chat = chat_service.create_chat(
                    db,
                    user_id=user.id,
                    title=chat_title,
                    current_dataset_id=db_dataset.id,
                )
            else:
                chat_repository.update_current_dataset(
                    db, resolved_chat.id, db_dataset.id, user.id
                )

            # 1. Persist User's File Card Message
            message_repository.append_message(
                db,
                chat_id=resolved_chat.id,
                role="user",
                content="",
                message_type="conversation",
                metadata_json={
                    "isFileCard": True,
                    "fileName": filename,
                    "fileSize": f"{len(raw_bytes) / 1024:.1f} KB",
                    "dataset_id": db_dataset_id,
                },
            )

            # 2. Persist Assistant's Dataset Summary Message
            if summary_text:
                message_repository.append_message(
                    db,
                    chat_id=resolved_chat.id,
                    role="assistant",
                    content=summary_text,
                    message_type="conversation",
                    metadata_json={
                        "dataset_id": db_dataset_id,
                        "filename": filename,
                        "row_count": len(df) if df is not None else None,
                        "column_count": len(df.columns) if df is not None else None,
                    },
                )

            chat_repository.update_last_message(db, resolved_chat.id)
            db.commit()

            acc_tag = compute_account_tag(resolved_chat.user_id)
            c_tag = compute_chat_tag(resolved_chat.id)
            canonical_slug = resolved_chat.slug or f"{acc_tag}-{c_tag}"

            logger.info(
                '[DB] Dataset and messages persisted: db_id=%s chat_id=%s slug=%s user=%s',
                db_dataset_id, resolved_chat.id, canonical_slug, email,
            )

            return db_dataset_id, str(resolved_chat.id), canonical_slug

    except Exception as e:
        logger.error('[DB] Dataset persistence failed (non-fatal): %s', e, exc_info=True)
        return None, str(chat_id) if chat_id else None, None

def _build_deterministic_upload_summary(dko, filename: str, df) -> str:
    """
    Build a structured factual upload summary from the DKO and DataFrame.

    ARCHITECTURE: This replaces the LLM essay. The Investigation workspace
    provides detailed analysis. This message is a rich-but-compact confirmation.

    Uses pandas facts only — NO LLM interpretation.
    """
    import pandas as pd

    rows = len(df)
    cols = len(df.columns)

    # Count missing values from the actual DataFrame (deterministic)
    total_missing = int(df.isnull().sum().sum())
    missing_by_col = df.isnull().sum()
    cols_with_missing = {col: int(count) for col, count in missing_by_col.items() if count > 0}

    # Count duplicates
    try:
        dup_count = int(df.duplicated().sum())
    except Exception:
        dup_count = 0

    # Completeness
    complete_rows = rows - int(df.isnull().any(axis=1).sum())
    complete_pct = round((complete_rows / rows) * 100) if rows > 0 else 100

    # Column type breakdown
    numeric_cols = df.select_dtypes(include='number').columns.tolist()
    cat_cols = [c for c in df.columns if c not in numeric_cols]

    # Build structured message
    parts = [f"**{filename}**", f"", f"{rows:,} rows x {cols} columns"]

    # Data Quality section
    quality_lines = []
    if total_missing > 0:
        col_details = ", ".join(
            f"`{col}` ({count})" for col, count in list(cols_with_missing.items())[:5]
        )
        if len(cols_with_missing) > 5:
            col_details += f" (+{len(cols_with_missing) - 5} more)"
        quality_lines.append(
            f"- {total_missing} missing {'cell' if total_missing == 1 else 'cells'} "
            f"across {len(cols_with_missing)} column{'s' if len(cols_with_missing) != 1 else ''}: "
            f"{col_details}"
        )
    if dup_count > 0:
        quality_lines.append(f"- {dup_count} duplicate row{'s' if dup_count != 1 else ''}")
    quality_lines.append(f"- {complete_pct}% complete rows ({complete_rows:,}/{rows:,})")

    if quality_lines:
        parts.append("")
        parts.append("**Data Quality**")
        parts.extend(quality_lines)

    # Column table (compact)
    parts.append("")
    parts.append("| Column | Type | Missing | Unique |")
    parts.append("|--------|------|--------:|-------:|")
    for col_name in df.columns:
        col_type = "numeric" if col_name in numeric_cols else "text"
        n_missing = int(missing_by_col[col_name])
        try:
            n_unique = int(df[col_name].nunique())
        except Exception:
            n_unique = 0
        parts.append(f"| {col_name} | {col_type} | {n_missing} | {n_unique} |")

    parts.append("")
    parts.append("Investigation ready.")

    logger.info(
        "[UPLOAD] dataset=%s rows=%d columns=%d missing=%d duplicates=%d complete_pct=%d",
        filename, rows, cols, total_missing, dup_count, complete_pct,
    )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Upload Endpoint — full RFC-002 pipeline
# ---------------------------------------------------------------------------

@datasets_bp.route('/upload', methods=['POST'])
def upload_dataset() -> Tuple[Any, int]:
    """
    Upload and process a dataset.

    SSE event sequence:
      event: analyzing   — immediate, confirms connection
      event: analyzing   — status updates during intelligence phases
      event: module_ready — one per knowledge module as built
      event: chunk       — streamed LLM narrative text
      event: done        — generation complete; payload: {dataset_id, dataset_info, available_modules}
      event: error       — if a fatal error occurs
    """
    if 'file' not in request.files:
        err, status = error_response('No file provided', 400)
        return jsonify(err), status

    file = request.files['file']
    if file.filename == '':
        err, status = error_response('No file selected', 400)
        return jsonify(err), status

    filename = file.filename
    ext = '.' + filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if ext not in ALLOWED_EXTENSIONS:
        err, status = error_response(
            f'Unsupported file type: {ext}. Allowed: .csv, .xlsx, .xls', 400
        )
        return jsonify(err), status

    try:
        # CRITICAL: Read raw bytes BEFORE dataset_storage.save() consumes the file.
        # After save(), the file object is closed/exhausted and cannot be read again.
        # These bytes are passed to _try_persist_dataset_to_db to avoid
        # 'seek of closed file' errors during PostgreSQL persistence.
        raw_bytes = file.read()
        file.seek(0)  # Reset so dataset_storage.save() can read from the start

        # ── CSV File-size enforcement (Free-tier 35 MB limit) ────────────────
        file_size_mb = len(raw_bytes) / (1024 * 1024)
        ext_lower = ext.lower()
        csv_limit_mb = Config.MAX_UPLOAD_SIZE_MB

        if ext_lower == '.csv' and file_size_mb > csv_limit_mb:
            return jsonify({
                "error": "File too large",
                "error_code": "FILE_TOO_LARGE",
                "limit_mb": csv_limit_mb,
                "file_size_mb": round(file_size_mb, 1),
                "status": 413,
                "user_message": (
                    "**Need to upload a larger dataset?**\n\n"
                    f"Plexis is currently in its free development phase, with CSV uploads limited to {csv_limit_mb} MB.\n\n"
                    "We're working toward expanded upload limits on our upcoming paid plans. "
                    "We apologize for the inconvenience and appreciate your patience as we continue improving Plexis."
                ),
            }), 413
        # ────────────────────────────────────────────────────────────────────

        saved_path = dataset_storage.save(file, filename)
        event_bus.publish(DatasetUploadedEvent(filename=filename))
        fp = file_fingerprint(saved_path)

        # Removed file-hash cache check. Same file must get a NEW dataset_id and fresh state.

        # Load DataFrame
        df = dataset_loader.load(saved_path)
        event_bus.publish(DatasetLoadedEvent(filename=filename, rows=len(df), columns=len(df.columns)))

        profile = dataset_profiler.profile(df)

        # Dataset ID assigned before streaming begins — frontend receives it in done event
        dataset_id = str(uuid.uuid4())

        @stream_with_context
        def stream_new():
            accumulated_chunks = []
            try:
                # Heartbeat: connection confirmed
                yield _sse_analyzing("starting")

                # DIE Stages 1–10
                yield _sse_analyzing("running_intelligence_engine")
                dko = dataset_intelligence_engine.analyze(df, dataset_name=filename, skip_presentation=True)
                event_bus.publish(DatasetIntelligenceCompleteEvent(
                    filename=filename,
                    domain=dko.get_dataset_identity().probable_purpose
                ))

                # Register in dataset registry (before streaming narrative)
                column_profiles = {}
                for k, v in dko.columns.items():
                    if v.numeric_stats:
                        column_profiles[k] = v.numeric_stats.to_dict()
                    elif v.categorical_stats:
                        column_profiles[k] = v.categorical_stats.to_dict()
                    elif v.datetime_stats:
                        column_profiles[k] = v.datetime_stats.to_dict()
                    else:
                        column_profiles[k] = {}

                entry = DatasetEntry(
                    dataset_id=dataset_id,
                    filename=filename,
                    file_path=saved_path,
                    upload_timestamp=datetime.now(timezone.utc).isoformat(),
                    row_count=len(df),
                    column_count=len(df.columns),
                    file_fingerprint=fp,
                    schema_fingerprint=dko.fingerprint,
                    dataframe=df,
                    profile=profile,
                    schema_profile=[c.to_dict() for c in dko.columns.values()],
                    column_profiles=column_profiles,
                    ontology=None,
                    dko=dko,
                )
                dataset_registry.register(entry)
                event_bus.publish(DatasetProfiledEvent(filename=filename, dataset_id=dataset_id))

                # Knowledge Decomposition — emit module_ready per module
                yield _sse_analyzing("decomposing_knowledge")
                bundle = knowledge_decomposer.decompose(dko, dataset_id)
                for module in bundle.all_modules():
                    yield _sse_module_ready(
                        module.module_id,
                        module.display_name,
                        module.richness_score,
                        module.preview,
                    )

                # Semantic Importance Ranking
                yield _sse_analyzing("ranking_intelligence")
                summary = importance_ranker.rank(bundle, dko)

                # Store in registry (dataset_id keyed, TTL=24h, LRU max 200)
                module_registry.store(summary)

                # ── Deterministic Upload Summary (replaces LLM essay) ─────────
                # ARCHITECTURE: Upload produces a SHORT factual summary.
                # The Investigation workspace provides the full analysis.
                # The LLM must NOT generate the upload message.
                presentation_source = "deterministic"
                summary_text = _build_deterministic_upload_summary(dko, filename, df)
                accumulated_chunks.append(summary_text)
                yield _sse_chunk(summary_text)

                # Build frontend payload and module list
                frontend_payload = dataset_intelligence_engine.get_frontend_payload(dko)
                available_modules = _safe_list_modules(dataset_id)

                # Persist to PostgreSQL (non-blocking — errors never break SSE)
                # Pass pre-read raw_bytes — not the file object (which is closed by now)
                chat_id = request.form.get('chat_id') or request.args.get('chat_id')
                persist_res = _try_persist_dataset_to_db(
                    raw_bytes, filename, dataset_id, dko, df, chat_id=chat_id, summary_text=summary_text
                )
                db_id = persist_res[0] if isinstance(persist_res, tuple) else persist_res
                resolved_chat_id = persist_res[1] if isinstance(persist_res, tuple) else (str(chat_id) if chat_id else None)
                resolved_chat_slug = persist_res[2] if isinstance(persist_res, tuple) else None

                final_dataset_id = db_id or dataset_id

                yield _sse_done({
                    "dataset_id": final_dataset_id,
                    "dataset_info": frontend_payload,
                    "presentation_source": presentation_source,
                    "trigger_investigation": True,  # Signal frontend to open Investigation tab
                    "chat_id": resolved_chat_id,
                    "slug": resolved_chat_slug,
                    "summary_text": summary_text,
                })
                
                # After yielding done event, auto-start investigation
                try:
                    from autonomous_analysis.engine import autonomous_investigation_engine
                    autonomous_investigation_engine.start_background(final_dataset_id, dko, df)
                except Exception as e:
                    logger.warning(f"Auto-investigation failed to start: {e}")

            except GeneratorExit:
                logger.info(f"SSE stream for '{filename}' closed by client")
            except Exception as e:
                logger.error(f"Upload stream error for '{filename}': {e}", exc_info=True)
                yield _sse_error(f"Processing failed: {str(e)}")
                try:
                    frontend_payload = dataset_intelligence_engine.get_frontend_payload(
                        dataset_registry.get_by_id(dataset_id).dko
                    ) if dataset_registry.get_by_id(dataset_id) else {}
                    yield _sse_done({"dataset_id": dataset_id, "dataset_info": frontend_payload})
                except Exception:
                    yield _sse_done({"dataset_id": dataset_id, "dataset_info": {}})

        return _sse_response(stream_new)

    except ValidationError as ve:
        logger.warning(f"Upload validation error: {ve}")
        err, status = error_response(str(ve), 400)
        return jsonify(err), status
    except Exception as e:
        logger.error(f"Upload error: {e}", exc_info=True)
        err, status = error_response(f'Failed to process dataset: {str(e)}', 500)
        return jsonify(err), status


# ---------------------------------------------------------------------------
# Dataset listing
# ---------------------------------------------------------------------------

@datasets_bp.route('/datasets', methods=['GET'])
def list_datasets() -> Tuple[Any, int]:
    """List all registered datasets."""
    entries = dataset_registry.all_datasets()
    datasets = []
    for e in entries:
        datasets.append({
            'dataset_id': e.dataset_id,
            'filename': e.filename,
            'upload_timestamp': e.upload_timestamp,
            'rows': e.row_count,
            'columns': e.column_count,
            'memory_usage': e.profile.get('memory_usage', 'unknown'),
        })
    return jsonify({'datasets': datasets}), 200


# ---------------------------------------------------------------------------
# Dataset by ID — replaces /datasets/active (audit fix C-1)
# ---------------------------------------------------------------------------

@datasets_bp.route('/datasets/<dataset_id>', methods=['GET'])
def get_dataset(dataset_id: str) -> Tuple[Any, int]:
    """Get the intelligence profile for a specific dataset."""
    entry = dataset_registry.get_by_id(dataset_id)
    if not entry:
        err, status = error_response(f"Dataset '{dataset_id}' not found", 404)
        return jsonify(err), status

    if entry.dko is not None:
        payload = dataset_intelligence_engine.get_frontend_payload(entry.dko)
        return jsonify({
            'dataset_id': entry.dataset_id,
            'filename': entry.filename,
            'intelligence': payload,
        }), 200

    return jsonify({
        'dataset_id': entry.dataset_id,
        'filename': entry.filename,
        'profile': entry.profile,
        'schema': entry.schema_profile,
    }), 200


@datasets_bp.route('/datasets/<dataset_id>/context', methods=['GET'])
def get_dataset_context(dataset_id: str) -> Tuple[Any, int]:
    """Get the LLM-ready context summary for a specific dataset."""
    entry = dataset_registry.get_by_id(dataset_id)
    if not entry or entry.dko is None:
        err, status = error_response(f"Dataset '{dataset_id}' not found or not analyzed", 404)
        return jsonify(err), status

    context_summary = dataset_intelligence_engine.get_context_summary(entry.dko)
    planner_context = dataset_intelligence_engine.get_planner_context(entry.dko)

    return jsonify({
        'context_summary': context_summary,
        'planner_context': planner_context,
    }), 200


# ---------------------------------------------------------------------------
# Legacy /datasets/active — kept for backward compat, uses most-recent entry
# ---------------------------------------------------------------------------

@datasets_bp.route('/datasets/active', methods=['GET'])
def get_active_dataset() -> Tuple[Any, int]:
    """
    LEGACY: Get the most recently registered dataset.
    Prefer GET /api/datasets/{dataset_id} for multi-tab safety.
    """
    entry = dataset_registry.get_active_dataset()
    if not entry:
        err, status = error_response('No active dataset', 404)
        return jsonify(err), status

    if entry.dko is not None:
        payload = dataset_intelligence_engine.get_frontend_payload(entry.dko)
        return jsonify({
            'dataset_id': entry.dataset_id,
            'filename': entry.filename,
            'intelligence': payload,
            'profile': entry.profile,
            'schema': entry.schema_profile,
        }), 200

    return jsonify({
        'dataset_id': entry.dataset_id,
        'filename': entry.filename,
        'profile': entry.profile,
        'schema': entry.schema_profile,
    }), 200


@datasets_bp.route('/datasets/active/context', methods=['GET'])
def get_active_dataset_context() -> Tuple[Any, int]:
    """LEGACY: Get context for active dataset."""
    entry = dataset_registry.get_active_dataset()
    if not entry or entry.dko is None:
        err, status = error_response('No active dataset with intelligence profile', 404)
        return jsonify(err), status

    context_summary = dataset_intelligence_engine.get_context_summary(entry.dko)
    planner_context = dataset_intelligence_engine.get_planner_context(entry.dko)

    return jsonify({
        'context_summary': context_summary,
        'planner_context': planner_context,
    }), 200


# ---------------------------------------------------------------------------
# Module Endpoints — RFC-002 §11 + §21
# ---------------------------------------------------------------------------

@datasets_bp.route('/datasets/<dataset_id>/modules', methods=['GET'])
def list_modules(dataset_id: str) -> Tuple[Any, int]:
    """
    List all available Knowledge Modules for a dataset.
    Returns module metadata sorted by richness_score (executive always first).
    """
    try:
        _ensure_bundle(dataset_id)
        modules = module_registry.list_available_modules(dataset_id)
        return jsonify({'dataset_id': dataset_id, 'modules': modules}), 200
    except BundleExpiredError:
        err, status = error_response(
            "Module session expired. Please re-upload the dataset.", 410
        )
        return jsonify(err), status
    except Exception as e:
        logger.error(f"list_modules error for {dataset_id}: {e}")
        err, status = error_response(str(e), 500)
        return jsonify(err), status


@datasets_bp.route('/datasets/<dataset_id>/modules/<module_id>', methods=['GET'])
def get_module(dataset_id: str, module_id: str) -> Tuple[Any, int]:
    """
    Get the raw JSON data for a specific Knowledge Module.
    Returns the full structured payload per RFC-002 §10 schemas.
    """
    try:
        _ensure_bundle(dataset_id)
        module = module_registry.get_module(dataset_id, module_id)
        return jsonify({
            'dataset_id': dataset_id,
            'module_id': module_id,
            'display_name': module.display_name,
            'richness_score': module.richness_score,
            'preview': module.preview,
            'data': module.data,
        }), 200
    except BundleExpiredError:
        err, status = error_response("Module session expired. Please re-upload the dataset.", 410)
        return jsonify(err), status
    except ModuleNotAvailableError as e:
        err, status = error_response(str(e), 404)
        return jsonify(err), status
    except Exception as e:
        logger.error(f"get_module error for {dataset_id}/{module_id}: {e}")
        err, status = error_response(str(e), 500)
        return jsonify(err), status


@datasets_bp.route('/datasets/<dataset_id>/modules/<module_id>/explain', methods=['POST'])
def explain_module(dataset_id: str, module_id: str) -> Tuple[Any, int]:
    """
    Stream an LLM explanation for a specific module.

    Request body (JSON, all optional):
      {
        "user_query": "string",         // personalize the explanation
        "verbosity": "brief|standard|detailed",
        "audience": "technical|non-technical|executive",
        "previously_stated": "string"   // what the primary narrative already said
      }

    Returns: text/event-stream (SSE)
    SSE events: chunk (text), done, error
    """
    try:
        _ensure_bundle(dataset_id)
        module = module_registry.get_module(dataset_id, module_id)
    except BundleExpiredError:
        err, status = error_response("Module session expired. Please re-upload the dataset.", 410)
        return jsonify(err), status
    except ModuleNotAvailableError as e:
        err, status = error_response(str(e), 404)
        return jsonify(err), status
    except Exception as e:
        err, status = error_response(str(e), 500)
        return jsonify(err), status

    body = request.get_json(silent=True) or {}
    user_query = body.get("user_query") or None
    verbosity = body.get("verbosity", "standard")
    audience = body.get("audience", "technical")
    previously_stated = body.get("previously_stated") or None

    # Get the fingerprint for cache keying
    try:
        bundle = module_registry.get_bundle(dataset_id)
        fingerprint = bundle.fingerprint
    except Exception:
        fingerprint = dataset_id

    @stream_with_context
    def stream_explanation():
        try:
            for chunk in module_explainer.explain_stream(
                module=module,
                dataset_fingerprint=fingerprint,
                dataset_id=dataset_id,
                user_query=user_query,
                verbosity=verbosity,
                audience=audience,
                previously_stated=previously_stated,
            ):
                if chunk:
                    yield _sse_chunk(chunk)
            yield _sse("done", {"module_id": module_id})
        except Exception as e:
            logger.error(f"explain_module SSE error for {dataset_id}/{module_id}: {e}")
            yield _sse_error(f"Explanation failed: {str(e)}")

    return _sse_response(stream_explanation)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sse_response(generator_fn):
    """Wrap a generator function in an SSE Response."""
    return Response(
        generator_fn(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Access-Control-Allow-Origin': '*',
        },
    )


def _ensure_bundle(dataset_id: str) -> None:
    """Re-decompose into registry from DKO if TTL-evicted."""
    if not module_registry.has_bundle(dataset_id):
        entry = dataset_registry.get_by_id(dataset_id)
        if entry and entry.dko:
            logger.info(f"Re-decomposing evicted bundle for dataset_id={dataset_id}")
            _decompose_and_store(entry.dko, dataset_id)
        else:
            raise BundleExpiredError(dataset_id)


def _decompose_and_store(dko, dataset_id: str) -> None:
    """Helper: decompose DKO and store into registry."""
    bundle = knowledge_decomposer.decompose(dko, dataset_id)
    summary = importance_ranker.rank(bundle, dko)
    module_registry.store(summary)


def _safe_list_modules(dataset_id: str) -> list:
    """Return module list or empty list on any error."""
    try:
        return module_registry.list_available_modules(dataset_id)
    except Exception as e:
        logger.warning(f"_safe_list_modules error for {dataset_id}: {e}")
        return []


def _persist_presentation(dko, chunks, dataset_id: str) -> None:
    """Persist the completed LLM narrative back into the DKO."""
    try:
        from dataset_intelligence.presentation_engine import PRESENTATION_VERSION
        from dataset_intelligence.models import DatasetPresentation
        from dataset_intelligence.knowledge_synthesizer import knowledge_synthesizer

        full_text = "".join(chunks)
        pres = DatasetPresentation(
            presentation=full_text,
            version=PRESENTATION_VERSION,
            model_used="groq",
            generated_at=datetime.now(timezone.utc).isoformat(),
            generated_by="llm",
        )
        knowledge_synthesizer.finalize(dko, pres)
    except Exception as e:
        logger.warning(f"Failed to persist presentation for {dataset_id}: {e}")

# ---------------------------------------------------------------------------
# Investigation Engine API Routes
# ---------------------------------------------------------------------------

@datasets_bp.route("/datasets/<dataset_id>/investigate", methods=["POST"])
def start_investigation(dataset_id):
    """Trigger autonomous investigation. Returns 202 immediately."""
    entry = dataset_registry.get_by_id(dataset_id)
    if not entry:
        return jsonify({"error": "Dataset not found"}), 404
        
    from autonomous_analysis.engine import autonomous_investigation_engine
    payload = request.get_json(silent=True) or {}
    if payload.get("refresh"):
        # Investigation reports are keyed to dataset semantics. Transformations
        # keep the dataset ID, so derive a new fingerprint from the live frame.
        import hashlib
        try:
            frame_hash = pd.util.hash_pandas_object(entry.dataframe, index=True).values.tobytes()
        except Exception:
            frame_hash = entry.dataframe.to_csv(index=True).encode("utf-8")
        fingerprint = hashlib.sha256(frame_hash).hexdigest()
        # Recompute schema/statistical semantics from the post-transform frame.
        # This request runs asynchronously from the user's apply/undo response.
        refreshed_dko = dataset_intelligence_engine.analyze(
            entry.dataframe,
            dataset_name=entry.filename,
            use_cache=False,
            skip_presentation=True,
        )
        from dataclasses import replace
        entry.dko = replace(
            refreshed_dko,
            fingerprint=fingerprint,
            row_count=len(entry.dataframe),
            column_count=len(entry.dataframe.columns),
        )
        autonomous_investigation_engine.invalidate_dataset(dataset_id)
    elif autonomous_investigation_engine.is_running(dataset_id):
        return jsonify({"error": "Investigation already in progress"}), 409
        
    autonomous_investigation_engine.start_background(dataset_id, entry.dko, entry.dataframe)
    return jsonify({"message": "Investigation started", "dataset_id": dataset_id}), 202

@datasets_bp.route("/datasets/<dataset_id>/investigation/stream")
def stream_investigation(dataset_id):
    """SSE stream of investigation events."""
    from autonomous_analysis.engine import autonomous_investigation_engine
    def generate():
        for event in autonomous_investigation_engine.stream_events(dataset_id):
            yield event.to_sse()
    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@datasets_bp.route("/datasets/<dataset_id>/investigation", methods=["GET"])
def get_investigation(dataset_id):
    """Get the completed investigation report."""
    from autonomous_analysis.engine import autonomous_investigation_engine
    report = autonomous_investigation_engine.get_report(dataset_id)
    if report:
        return jsonify(report)
    
    from datasets.registry import dataset_registry
    from autonomous_analysis.deterministic_overview import generate_deterministic_overview
    
    entry = dataset_registry.get_by_id(dataset_id)
    if entry is not None and entry.dataframe is not None:
        fallback_report = generate_deterministic_overview(entry.dataframe, entry.filename, dataset_id)
        if autonomous_investigation_engine.is_running(dataset_id):
            fallback_report["status"] = "in_progress"
            return jsonify(fallback_report), 202
        else:
            return jsonify(fallback_report), 200
    
    if autonomous_investigation_engine.is_running(dataset_id):
        return jsonify({"status": "in_progress"}), 202
    
    return jsonify({"status": "not_started"}), 200
