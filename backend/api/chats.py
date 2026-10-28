"""
Chats API — Phase 6

Endpoints:
    POST   /api/chats                 — create chat
    GET    /api/chats                 — list user's chats
    GET    /api/chats/<slug>          — load full chat by slug
    DELETE /api/chats/<id>            — delete chat + cascade
    GET    /api/chats/<id>/messages   — paginated messages
    GET    /api/chats/<id>/memory     — chat memories
    PATCH  /api/chats/<id>/dataset    — update current_dataset_id

All routes require X-User-Email header (DEV mode identity).
Ownership is verified on every resource access.
"""
import logging
from uuid import UUID

from flask import Blueprint, jsonify, request

from db import get_db, identity_resolver, IdentityError
from db.services.chat_service import chat_service
from db.repositories.message_repository import message_repository
from db.repositories.memory_repository import memory_repository
from db.repositories.chat_repository import chat_repository
from db.repositories.dataset_repository import dataset_repository

logger = logging.getLogger(__name__)

chats_bp = Blueprint("chats", __name__, url_prefix="/api/chats")


def _resolve_identity():
    """Resolve current user. Returns (CurrentUser, None) or (None, error_response)."""
    try:
        current_user = identity_resolver.resolve(request)
        return current_user, None
    except IdentityError as e:
        return None, (jsonify({"error": str(e)}), 401)
    except Exception as e:
        logger.error("[CHATS_API] Identity resolution failed: %s", e)
        return None, (jsonify({"error": "Identity resolution failed"}), 500)


def _serialize_chat(chat) -> dict:
    return {
        "id": str(chat.id),
        "slug": chat.slug,
        "title": chat.title,
        "current_dataset_id": str(chat.current_dataset_id) if chat.current_dataset_id else None,
        "created_at": chat.created_at.isoformat() if chat.created_at else None,
        "last_message_at": chat.last_message_at.isoformat() if chat.last_message_at else None,
    }


def _serialize_message(msg) -> dict:
    return {
        "id": str(msg.id),
        "role": msg.role,
        "content": msg.content,
        "message_type": msg.message_type,
        "sequence_number": msg.sequence_number,
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
        "metadata": msg.metadata_json or {},
    }


# ---------------------------------------------------------------------------
# POST /api/chats — create chat
# ---------------------------------------------------------------------------

@chats_bp.route("", methods=["POST"])
def create_chat():
    """Create a new chat for the current user, optionally with initial messages and dataset."""
    current_user, err = _resolve_identity()
    if err:
        return err

    db = get_db()
    body = request.get_json(silent=True) or {}
    title = (body.get("title") or "New Chat").strip()
    dataset_id_raw = body.get("dataset_id")
    messages = body.get("messages")

    current_dataset_id = None
    if dataset_id_raw:
        try:
            ds_uuid = UUID(str(dataset_id_raw))
            ds = dataset_repository.get_by_id(db, ds_uuid, current_user.user_id)
            if ds:
                current_dataset_id = ds_uuid
        except (ValueError, TypeError):
            current_dataset_id = None

    try:
        chat = chat_service.create_chat(
            db,
            user_id=current_user.user_id,
            title=title,
            current_dataset_id=current_dataset_id,
        )

        messages_created = 0
        if isinstance(messages, list) and messages:
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                raw_role = (msg.get("role") or msg.get("sender") or "user").strip().lower()
                if raw_role in ("user", "human"):
                    role = "user"
                elif raw_role in ("system", "system_error", "error"):
                    role = "system"
                else:
                    role = "assistant"

                content = msg.get("content") or msg.get("text") or ""
                msg_type = msg.get("message_type") or msg.get("type") or "conversation"
                metadata = msg.get("metadata") or {}
                if not isinstance(metadata, dict):
                    metadata = {}

                message_repository.append_message(
                    db,
                    chat_id=chat.id,
                    role=role,
                    content=str(content),
                    message_type=str(msg_type),
                    metadata_json=metadata,
                )
                messages_created += 1

            if messages_created > 0:
                chat_repository.update_last_message(db, chat.id)

        db.commit()
        logger.info(
            "[CHATS_API] Created chat slug=%s user=%s messages=%d",
            chat.slug, current_user.email, messages_created
        )
        return jsonify({
            "chat_id": str(chat.id),
            "slug": chat.slug,
            "title": chat.title,
            "current_dataset_id": str(chat.current_dataset_id) if chat.current_dataset_id else None,
            "messages_count": messages_created,
        }), 201
    except Exception as e:
        db.rollback()
        logger.error("[CHATS_API] create_chat failed: %s", e)
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------------------------
# GET /api/chats — list user's chats
# ---------------------------------------------------------------------------

@chats_bp.route("", methods=["GET"])
def list_chats():
    """List all non-archived chats for the current user."""
    current_user, err = _resolve_identity()
    if err:
        return err

    db = get_db()
    try:
        chats = chat_service.list_chats(db, user_id=current_user.user_id)
        return jsonify({"chats": [_serialize_chat(c) for c in chats]}), 200
    except Exception as e:
        logger.error("[CHATS_API] list_chats failed: %s", e)
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# GET /api/chats/<slug_or_id> — load full chat by slug or ID
# ---------------------------------------------------------------------------

@chats_bp.route("/<slug_or_id>", methods=["GET"])
def get_chat(slug_or_id: str):
    """Load full chat state by slug or UUID (ownership verified)."""
    current_user, err = _resolve_identity()
    if err:
        return err

    db = get_db()
    try:
        state = None
        try:
            chat_uuid = UUID(str(slug_or_id))
            state = chat_service.load_chat_by_id(db, chat_id=chat_uuid, user_id=current_user.user_id)
        except (ValueError, TypeError):
            pass

        if not state:
            state = chat_service.load_chat(db, slug=slug_or_id, user_id=current_user.user_id)

        if not state:
            return jsonify({"error": "Chat not found"}), 404

        return jsonify({
            "chat": _serialize_chat(state.chat),
            "messages": [_serialize_message(m) for m in state.messages],
            "current_dataset_id": state.current_dataset_id,
            "analysis_sessions": [
                {
                    "id": str(s.id),
                    "dataset_id": str(s.dataset_id),
                    "created_at": s.created_at.isoformat() if s.created_at else None,
                    "last_active_at": s.last_active_at.isoformat() if s.last_active_at else None,
                }
                for s in state.analysis_sessions
            ],
        }), 200
    except Exception as e:
        logger.error("[CHATS_API] get_chat failed for %s: %s", slug_or_id, e)
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# PATCH /api/chats/<chat_id> — update chat title
# ---------------------------------------------------------------------------

@chats_bp.route("/<chat_id>", methods=["PATCH"])
def update_chat(chat_id: str):
    """Update chat title (ownership verified)."""
    current_user, err = _resolve_identity()
    if err:
        return err

    db = get_db()
    body = request.get_json(silent=True) or {}
    title = body.get("title")
    if not title:
        return jsonify({"error": "title required"}), 400

    try:
        chat_uuid = UUID(str(chat_id))
        updated = chat_repository.update_title(db, chat_uuid, str(title).strip(), current_user.user_id)
        if not updated:
            return jsonify({"error": "Chat not found or not owned"}), 404
        db.commit()
        return jsonify({"updated": True, "title": str(title).strip()}), 200
    except ValueError:
        return jsonify({"error": "Invalid UUID format"}), 400
    except Exception as e:
        db.rollback()
        logger.error("[CHATS_API] update_chat failed for %s: %s", chat_id, e)
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# DELETE /api/chats/<id> — delete chat
# ---------------------------------------------------------------------------

@chats_bp.route("/<chat_id>", methods=["DELETE"])
def delete_chat(chat_id: str):
    """Hard delete a chat (cascades). Ownership verified."""
    current_user, err = _resolve_identity()
    if err:
        return err

    db = get_db()
    try:
        deleted = chat_service.delete_chat(db, UUID(chat_id), current_user.user_id)
        if not deleted:
            return jsonify({"error": "Chat not found"}), 404
        db.commit()
        return jsonify({"deleted": True}), 200
    except ValueError:
        return jsonify({"error": "Invalid chat_id format"}), 400
    except Exception as e:
        db.rollback()
        logger.error("[CHATS_API] delete_chat failed for %s: %s", chat_id, e)
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# GET /api/chats/<id>/messages — paginated messages
# ---------------------------------------------------------------------------

@chats_bp.route("/<chat_id>/messages", methods=["GET"])
def get_messages(chat_id: str):
    """Get all messages for a chat (ownership verified)."""
    current_user, err = _resolve_identity()
    if err:
        return err

    db = get_db()
    try:
        # Verify ownership
        chat = chat_repository.get_by_id(db, UUID(chat_id), current_user.user_id)
        if not chat:
            return jsonify({"error": "Chat not found"}), 404

        messages = message_repository.get_all(db, chat.id)
        return jsonify({"messages": [_serialize_message(m) for m in messages]}), 200
    except ValueError:
        return jsonify({"error": "Invalid chat_id format"}), 400
    except Exception as e:
        logger.error("[CHATS_API] get_messages failed for %s: %s", chat_id, e)
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# GET /api/chats/<id>/memory — chat memories
# ---------------------------------------------------------------------------

@chats_bp.route("/<chat_id>/memory", methods=["GET"])
def get_chat_memory(chat_id: str):
    """Get chat-scoped memory entries (requires both user_id + chat_id)."""
    current_user, err = _resolve_identity()
    if err:
        return err

    db = get_db()
    try:
        chat = chat_repository.get_by_id(db, UUID(chat_id), current_user.user_id)
        if not chat:
            return jsonify({"error": "Chat not found"}), 404

        memories = memory_repository.get_chat_memories(db, current_user.user_id, chat.id)
        return jsonify({
            "memories": [
                {"key": m.key, "value": m.value, "updated_at": m.updated_at.isoformat() if m.updated_at else None}
                for m in memories
            ]
        }), 200
    except ValueError:
        return jsonify({"error": "Invalid chat_id format"}), 400
    except Exception as e:
        logger.error("[CHATS_API] get_chat_memory failed for %s: %s", chat_id, e)
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# PATCH /api/chats/<id>/dataset — update current_dataset_id
# ---------------------------------------------------------------------------

@chats_bp.route("/<chat_id>/dataset", methods=["PATCH"])
def update_chat_dataset(chat_id: str):
    """Update the currently active dataset for a chat."""
    current_user, err = _resolve_identity()
    if err:
        return err

    db = get_db()
    body = request.get_json(silent=True) or {}
    dataset_id = body.get("dataset_id")
    if not dataset_id:
        return jsonify({"error": "dataset_id required"}), 400

    try:
        # Verify dataset ownership
        ds = dataset_repository.get_by_id(db, UUID(dataset_id), current_user.user_id)
        if not ds:
            return jsonify({"error": "Dataset not found or not owned"}), 404

        updated = chat_repository.update_current_dataset(
            db, UUID(chat_id), UUID(dataset_id), current_user.user_id
        )
        if not updated:
            return jsonify({"error": "Chat not found"}), 404

        db.commit()
        return jsonify({"updated": True}), 200
    except ValueError:
        return jsonify({"error": "Invalid UUID format"}), 400
    except Exception as e:
        db.rollback()
        logger.error("[CHATS_API] update_chat_dataset failed: %s", e)
        return jsonify({"error": str(e)}), 500
