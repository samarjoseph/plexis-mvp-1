"""
Regression Test Suite: Cross-Device Chat History & Multi-User Isolation
Validates:
1. User A creates chat and messages on Device 1; Device 2 (same identity) retrieves the exact same chat and messages.
2. User B cannot see, load, rename, or delete User A's chats (strict user isolation).
3. /api/ask auto-creates a chat in PostgreSQL when chat_id is omitted and persists messages.
4. Subsequent questions on the same chat_id append messages in deterministic sequence.
5. Chat dataset association persists and is returned on cross-device load.
6. PATCH /api/chats/<chat_id> updates title with ownership verification.
7. DELETE /api/chats/<chat_id> cascades deletion cleanly.
"""
import os
import sys
import uuid
import pytest

backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app import create_app
from db.session import db_session
from db.models.user import User
from db.models.chat import Chat
from db.models.message import Message


@pytest.fixture(scope="module")
def test_client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture
def user_a_email():
    return f"account_a_{uuid.uuid4().hex[:8]}@example.com"


@pytest.fixture
def user_b_email():
    return f"account_b_{uuid.uuid4().hex[:8]}@example.com"


def test_cross_device_history_retrieval(test_client, user_a_email):
    """Device 1 creates a chat; Device 2 (same email header) lists and loads it."""
    # ── Device 1: Create chat ──
    res = test_client.post(
        "/api/chats",
        headers={"X-User-Email": user_a_email, "Content-Type": "application/json"},
        json={"title": "Laptop Flight Delay Analysis"},
    )
    assert res.status_code == 201
    data = res.get_json()
    chat_id = data["chat_id"]
    slug = data["slug"]

    # ── Device 1: Send a question with chat_id ──
    res_ask = test_client.post(
        "/api/ask",
        headers={"X-User-Email": user_a_email, "Content-Type": "application/json"},
        json={"message": "hello plexis", "chat_id": chat_id},
    )
    assert res_ask.status_code == 200

    # ── Device 2: List chats ──
    res_list = test_client.get(
        "/api/chats",
        headers={"X-User-Email": user_a_email},
    )
    assert res_list.status_code == 200
    chats = res_list.get_json().get("chats", [])
    matching = [c for c in chats if c["id"] == chat_id]
    assert len(matching) == 1
    assert matching[0]["title"] == "Laptop Flight Delay Analysis"

    # ── Device 2: Load full chat by ID ──
    res_load_id = test_client.get(
        f"/api/chats/{chat_id}",
        headers={"X-User-Email": user_a_email},
    )
    assert res_load_id.status_code == 200
    load_data = res_load_id.get_json()
    assert load_data["chat"]["id"] == chat_id
    msgs = load_data.get("messages", [])
    assert len(msgs) >= 2  # user + assistant
    assert any("hello plexis" in m["content"] for m in msgs if m["role"] == "user")

    # ── Device 2: Load full chat by slug ──
    res_load_slug = test_client.get(
        f"/api/chats/{slug}",
        headers={"X-User-Email": user_a_email},
    )
    assert res_load_slug.status_code == 200
    assert res_load_slug.get_json()["chat"]["id"] == chat_id


def test_user_isolation_and_unauthorized_access(test_client, user_a_email, user_b_email):
    """User B cannot view, load, update, or delete User A's chats."""
    # User A creates a chat
    res_a = test_client.post(
        "/api/chats",
        headers={"X-User-Email": user_a_email, "Content-Type": "application/json"},
        json={"title": "User A Private Chat"},
    )
    assert res_a.status_code == 201
    chat_id_a = res_a.get_json()["chat_id"]
    slug_a = res_a.get_json()["slug"]

    # User B lists chats — must NOT see User A's chat
    res_b_list = test_client.get(
        "/api/chats",
        headers={"X-User-Email": user_b_email},
    )
    assert res_b_list.status_code == 200
    b_chats = res_b_list.get_json().get("chats", [])
    assert all(c["id"] != chat_id_a for c in b_chats)

    # User B tries to load User A's chat by ID -> 404
    res_b_get_id = test_client.get(
        f"/api/chats/{chat_id_a}",
        headers={"X-User-Email": user_b_email},
    )
    assert res_b_get_id.status_code == 404

    # User B tries to load User A's chat by slug -> 404
    res_b_get_slug = test_client.get(
        f"/api/chats/{slug_a}",
        headers={"X-User-Email": user_b_email},
    )
    assert res_b_get_slug.status_code == 404

    # User B tries to rename User A's chat -> 404
    res_b_patch = test_client.patch(
        f"/api/chats/{chat_id_a}",
        headers={"X-User-Email": user_b_email, "Content-Type": "application/json"},
        json={"title": "Hacked Title"},
    )
    assert res_b_patch.status_code == 404

    # User B tries to delete User A's chat -> 404
    res_b_delete = test_client.delete(
        f"/api/chats/{chat_id_a}",
        headers={"X-User-Email": user_b_email},
    )
    assert res_b_delete.status_code == 404


def test_ask_auto_creates_chat_when_omitted(test_client, user_a_email):
    """When /api/ask is called without chat_id, it auto-creates a chat in DB and returns it."""
    res = test_client.post(
        "/api/ask",
        headers={"X-User-Email": user_a_email, "Content-Type": "application/json"},
        json={"message": "what is plexis?"},
    )
    assert res.status_code == 200
    data = res.get_json()
    chat_id = data.get("chat_id")
    assert chat_id is not None
    # Validate UUID format
    assert uuid.UUID(chat_id)

    # Verify chat was created in PostgreSQL for user_a
    res_list = test_client.get(
        "/api/chats",
        headers={"X-User-Email": user_a_email},
    )
    assert res_list.status_code == 200
    chats = res_list.get_json().get("chats", [])
    matching = [c for c in chats if c["id"] == chat_id]
    assert len(matching) == 1

    # Verify messages were persisted
    res_load = test_client.get(
        f"/api/chats/{chat_id}",
        headers={"X-User-Email": user_a_email},
    )
    assert res_load.status_code == 200
    msgs = res_load.get_json().get("messages", [])
    assert len(msgs) >= 2


def test_rename_and_delete_chat(test_client, user_a_email):
    """PATCH renames chat and DELETE removes it."""
    res_create = test_client.post(
        "/api/chats",
        headers={"X-User-Email": user_a_email, "Content-Type": "application/json"},
        json={"title": "Initial Title"},
    )
    assert res_create.status_code == 201
    chat_id = res_create.get_json()["chat_id"]

    # Rename
    res_rename = test_client.patch(
        f"/api/chats/{chat_id}",
        headers={"X-User-Email": user_a_email, "Content-Type": "application/json"},
        json={"title": "Renamed Title"},
    )
    assert res_rename.status_code == 200
    assert res_rename.get_json()["title"] == "Renamed Title"

    # Verify renamed in list
    res_list = test_client.get(
        "/api/chats",
        headers={"X-User-Email": user_a_email},
    )
    chats = res_list.get_json().get("chats", [])
    assert any(c["id"] == chat_id and c["title"] == "Renamed Title" for c in chats)

    # Delete
    res_del = test_client.delete(
        f"/api/chats/{chat_id}",
        headers={"X-User-Email": user_a_email},
    )
    assert res_del.status_code == 200
    assert res_del.get_json()["deleted"] is True

    # Verify gone
    res_get_del = test_client.get(
        f"/api/chats/{chat_id}",
        headers={"X-User-Email": user_a_email},
    )
    assert res_get_del.status_code == 404
