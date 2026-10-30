"""Deterministic request processing pipeline."""
import logging
from typing import Optional
from flask import g

from core.context import ExecutionContext
from core.events import event_bus, PipelineStartedEvent, PipelineFinishedEvent
from session.manager import session_manager
from memory.conversation import conversation_memory
from datasets.registry import dataset_registry
from core.normalization import query_normalizer

logger = logging.getLogger(__name__)


class RequestPipeline:
    """Processes every request through ordered stages before engine dispatch."""

    def process(
        self,
        message: str,
        dataset_id: Optional[str] = None,
        session_id: Optional[str] = None,
        workspace_state: Optional[dict] = None,
        workspace_action: Optional[dict] = None,
        chat_id: Optional[str] = None,      # NEW: passed from /api/ask for L2 restore
        client_messages: Optional[List[dict]] = None,
    ) -> ExecutionContext:
        """Run the full pipeline and return an enriched ExecutionContext."""
        ctx = ExecutionContext(message=message)

        event_bus.publish(PipelineStartedEvent(request_id=ctx.request_id))

        # Stage 1: Request ID
        stage = ctx.start_stage('request_id_generation')
        try:
            from flask import has_request_context, has_app_context
            if has_request_context() or has_app_context():
                ctx.request_id = getattr(g, 'request_id', '')
            else:
                ctx.request_id = ''
        except Exception:
            ctx.request_id = ''
        ctx.complete_stage(stage)

        # Stage 2: Session Resolution
        stage = ctx.start_stage('session_resolution')
        session = session_manager.get_or_create(session_id)
        ctx.session_id = session.session_id
        ctx.chat_id = chat_id  # Set for analytical session scoping
        session_manager.increment_message_count(session.session_id)
        ctx.complete_stage(stage)

        # Stage 2.5: User Identity Resolution
        stage = ctx.start_stage('user_identity')
        try:
            from flask import request as _flask_req
            from db.session import db_session
            from db.repositories.user_repository import user_repository
            email = (_flask_req.headers.get('X-User-Email') or '').strip().lower() or 'dev@plexis.local'
            with db_session() as _db:
                _user = user_repository.get_or_create_by_email(_db, email)
                ctx.user_id = _user.id
            ctx.complete_stage(stage)
        except Exception as e:
            ctx.complete_stage(stage, success=False, error=str(e))
            logger.debug("[Pipeline] User identity resolution failed: %s", e)

        # Stage 3: Dataset Verification
        # Priority: explicit dataset_id → chat_id DB restore → session cache → last active
        stage = ctx.start_stage('dataset_verification')
        try:
            self._resolve_dataset(ctx, dataset_id, session, chat_id=chat_id)
            ctx.complete_stage(stage)
        except Exception as e:
            ctx.complete_stage(stage, success=False, error=str(e))
            # Non-fatal: requests without datasets are valid (general chat)
            logger.debug(f"No dataset resolved: {e}")

        # Stage 3.2: Workspace State Injection
        stage = ctx.start_stage('workspace_state_injection')
        try:
            ctx.workspace_state = workspace_state or {}
            ctx.workspace_action = workspace_action or None
            if workspace_state:
                from workspace.interpreter import WorkspaceInterpreter
                ctx.workspace_summary = WorkspaceInterpreter().interpret(workspace_state)
            ctx.complete_stage(stage)
        except Exception as e:
            ctx.complete_stage(stage, success=False, error=str(e))
            logger.debug(f"Workspace state injection failed: {e}")

        # Stage 3.5: Query Normalization
        stage = ctx.start_stage('query_normalization')
        query_normalizer.normalize(ctx)
        ctx.complete_stage(stage)

        # Stage 4: Conversation Memory
        stage = ctx.start_stage('conversation_memory')
        ctx.conversation_history = conversation_memory.get_history(
            ctx.session_id,
            last_n=20,
            dataset_id=ctx.dataset_id,
            chat_id=ctx.chat_id,
            client_messages=client_messages,
        )
        ctx.complete_stage(stage)

        event_bus.publish(PipelineFinishedEvent(
            request_id=ctx.request_id,
            duration_ms=round(ctx.total_pipeline_ms(), 2),
            success=True,
            steps_completed=len(ctx.trace),
        ))

        logger.debug(f"Pipeline complete: {ctx.trace_summary()}")
        return ctx

    def _resolve_dataset(
        self,
        ctx: ExecutionContext,
        dataset_id: Optional[str],
        session,
        chat_id: Optional[str] = None,
    ) -> None:
        """
        Resolve the authoritative dataset for this user/session request.

        Resolution order (first match wins):
          1. Explicit dataset_id → L1 registry lookup
          2. Explicit dataset_id → L2 DB restore (server restart / cold cache)
          3. chat_id → DB current_dataset_id → L2 restore (chat-scoped durable restore)
          4. session.active_dataset_id → L1 lookup or L2 restore

        NO GLOBAL FALLBACK: To prevent state cross-contamination across users or sessions,
        we never silently fall back to an unrelated active dataset.
        """
        entry = None

        # Sanitize explicit dataset_id
        requested_dataset_id = None
        if dataset_id:
            cleaned_ds = str(dataset_id).strip()
            if cleaned_ds.lower() not in ("no active dataset", "none", "null", "undefined", ""):
                dataset_id = cleaned_ds
                requested_dataset_id = cleaned_ds
            else:
                dataset_id = None
        ctx.requested_dataset_id = requested_dataset_id

        # ── Step 1: Explicit dataset_id → L1 ─────────────────────────────────
        if dataset_id:
            entry = dataset_registry.get(dataset_id)
            if not entry:
                entry = dataset_registry.get_by_filename(dataset_id)

        # ── Step 2: Explicit dataset_id → L2 DB restore ───────────────────────
        if not entry and dataset_id:
            entry = self._try_restore_from_db(dataset_id, chat_id)
            if entry:
                logger.info(
                    "[Pipeline] L2 restore succeeded via dataset_id=%s", dataset_id
                )

        # ── Step 3: chat_id → DB current_dataset → L2 restore ────────────────
        if not entry and chat_id:
            entry = self._try_restore_from_chat(chat_id)
            if entry:
                logger.info(
                    "[Pipeline] L2 restore succeeded via chat_id=%s → dataset=%s",
                    chat_id, entry.dataset_id,
                )

        # ── Step 4: Session cache ─────────────────────────────────────────────
        if not entry and session.active_dataset_id:
            entry = dataset_registry.get(session.active_dataset_id)
            if not entry:
                entry = self._try_restore_from_db(session.active_dataset_id, chat_id)

        # ── Step 4b: Check if metadata exists but data access failed ──────────
        if not entry and requested_dataset_id:
            try:
                from db.session import db_session
                from db.repositories.dataset_repository import dataset_repository
                from db.repositories.user_repository import user_repository
                from flask import request as flask_request
                import uuid as _uuid
                email = ((flask_request.headers.get('X-User-Email') or '').strip().lower() or 'dev@plexis.local') if has_request_context() else 'dev@plexis.local'
                with db_session() as db:
                    user = user_repository.get_or_create_by_email(db, email)
                    try:
                        ds_uuid = _uuid.UUID(str(requested_dataset_id))
                        db_ds = dataset_repository.get_by_id(db, ds_uuid, user.id)
                        if db_ds:
                            # Dataset metadata exists for this user, but DataFrame retrieval failed
                            ctx.dataset_id = str(db_ds.id)
                            ctx.dataset_filename = db_ds.original_filename
                            ctx.dataset_access_failed = True
                            ctx.dataset_resolution_error = (
                                f"Dataset '{db_ds.original_filename}' is registered, but its underlying data "
                                "could not be retrieved from storage. Please re-upload or select the dataset again."
                            )
                    except (ValueError, TypeError):
                        pass
            except Exception as ex:
                logger.debug("[Pipeline] Metadata availability check failed: %s", ex)

        # ── Hydrate context ───────────────────────────────────────────────────
        if entry:
            ctx.dataset_id = entry.dataset_id
            ctx.dataset_filename = entry.filename
            ctx.dataset_fingerprint = entry.file_fingerprint
            ctx.schema_fingerprint = entry.schema_fingerprint
            ctx.dataset_profile = entry.profile
            ctx.schema_profile = entry.schema_profile
            ctx.column_profiles = entry.column_profiles
            ctx.dko = entry.dko
            session_manager.update_dataset(ctx.session_id, entry.dataset_id)

    # ── L2 Restore Helpers ────────────────────────────────────────────────────

    def _try_restore_from_db(
        self,
        dataset_id: str,
        chat_id: Optional[str] = None,
    ):
        """
        Restore a specific dataset from PostgreSQL by dataset_id.
        Used when dataset_id is known but L1 cache is cold.
        Returns DatasetEntry or None.
        """
        try:
            from config import Config
            if not Config.DATABASE_URL:
                return None

            from db.session import db_session
            from db.services.dataset_service import dataset_service
            from db.repositories.user_repository import user_repository
            from flask import request as flask_request
            import uuid as _uuid

            from flask import has_request_context
            if has_request_context():
                email = (flask_request.headers.get('X-User-Email') or '').strip().lower() or 'dev@plexis.local'
            else:
                email = 'dev@plexis.local'

            with db_session() as db:
                user = user_repository.get_or_create_by_email(db, email)
                try:
                    ds_uuid = _uuid.UUID(str(dataset_id))
                except (ValueError, TypeError):
                    from db.models.dataset import Dataset
                    db_ds = db.query(Dataset).filter(Dataset.user_id == user.id, Dataset.original_filename == str(dataset_id)).order_by(Dataset.created_at.desc()).first()
                    if db_ds:
                        ds_uuid = db_ds.id
                    else:
                        return None
                _dataset, df = dataset_service.restore_dataset_df(db, ds_uuid, user.id)

            if df is not None:
                # restore_dataset_df re-registers in L1 cache
                return dataset_registry.get(str(ds_uuid))
        except Exception as e:
            logger.debug("[Pipeline] DB restore by dataset_id failed: %s", e)
        return None

    def _try_restore_from_chat(self, chat_id: str):
        """
        Restore a dataset by looking up the chat's current_dataset_id in PostgreSQL.
        This is the primary restart-safety mechanism.

        Flow:
          1. Query chats.current_dataset_id for this chat_id
          2. Call dataset_service.restore_dataset_df() → decompresses BYTEA → DataFrame
          3. dataset_service re-registers the entry in DatasetRegistry L1
          4. Return the freshly restored DatasetEntry

        Chat-scoped: each chat has its own current_dataset_id.
        No cross-chat contamination possible.
        Returns DatasetEntry or None.
        """
        try:
            from config import Config
            if not Config.DATABASE_URL:
                return None

            from db.session import db_session
            from db.repositories.chat_repository import chat_repository
            from db.repositories.user_repository import user_repository
            from db.services.dataset_service import dataset_service
            from flask import request as flask_request
            import uuid as _uuid

            from flask import has_request_context
            if has_request_context():
                email = (flask_request.headers.get('X-User-Email') or '').strip().lower() or 'dev@plexis.local'
            else:
                email = 'dev@plexis.local'

            with db_session() as db:
                user = user_repository.get_or_create_by_email(db, email)
                try:
                    chat_uuid = _uuid.UUID(str(chat_id))
                except (ValueError, TypeError):
                    from db.models.chat import Chat
                    chat_obj = db.query(Chat).filter(Chat.user_id == user.id, Chat.slug == str(chat_id)).first()
                    chat_uuid = chat_obj.id if chat_obj else None

                if not chat_uuid:
                    return None

                # Ownership-verified chat lookup
                chat = chat_repository.get_by_id(db, chat_uuid, user.id)
                if not chat:
                    logger.debug(
                        "[Pipeline] Chat %s not found for user %s — skipping restore",
                        chat_id, email,
                    )
                    return None

                # Resolve the chat's current_dataset_id
                ds_uuid = getattr(chat, 'current_dataset_id', None) or \
                          getattr(chat, 'active_dataset_id', None)
                if not ds_uuid:
                    logger.debug(
                        "[Pipeline] Chat %s has no current_dataset_id — no restore",
                        chat_id,
                    )
                    return None

                # Restore DataFrame from PostgreSQL BYTEA storage
                _dataset, df = dataset_service.restore_dataset_df(db, ds_uuid, user.id)

            if df is not None:
                entry = dataset_registry.get(str(ds_uuid))
                logger.info(
                    "[Pipeline] Chat-scoped L2 restore: chat=%s → dataset=%s (%d rows)",
                    chat_id, ds_uuid, len(df),
                )
                return entry

        except Exception as e:
            logger.debug("[Pipeline] Chat-scoped DB restore failed for chat=%s: %s", chat_id, e)
        return None

    def record_response(
        self,
        ctx: ExecutionContext,
        user_message: str,
        assistant_response: str,
    ) -> None:
        """Record the exchange in conversation memory."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        conversation_memory.append(ctx.session_id, 'user', user_message, now, dataset_id=ctx.dataset_id)
        conversation_memory.append(ctx.session_id, 'assistant', assistant_response, now, dataset_id=ctx.dataset_id)


request_pipeline = RequestPipeline()
