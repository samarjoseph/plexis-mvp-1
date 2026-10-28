"""
Test suite: Title Pipeline Quality, Title Uniqueness, and URL Link Isolation.

Validates:
1. Title generation does NOT verbatim echo or title-case user messages (e.g. 'hello bro' != 'Hello Bro').
2. Title uniqueness per account (e.g. 'Casual Greeting', 'Casual Greeting 2').
3. Multi-user title isolation (User B can also have 'Casual Greeting' without colliding with User A).
4. URL Link Isolation:
   - Account tag is exactly 6 alphanumeric chars (mixed digits and letters).
   - Chat tag is exactly 8 alphanumeric chars (mixed digits and letters).
   - Combined slug is '<6-char account tag>-<8-char chat tag>'.
   - User can load chat via '<account_tag>-<chat_tag>'.
   - Other users cannot access User A's isolated link (404 enforced).
"""
import os
import sys
import uuid
import pytest

backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app import create_app
from db.isolation import compute_account_tag, compute_chat_tag, generate_chat_slug


@pytest.fixture(scope="module")
def test_client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture
def user_a_email():
    return f"iso_user_a_{uuid.uuid4().hex[:8]}@example.com"


@pytest.fixture
def user_b_email():
    return f"iso_user_b_{uuid.uuid4().hex[:8]}@example.com"


def test_isolation_tags_format_and_determinism():
    """Verify tags are 6 and 8 chars, mixed letters and digits, and deterministic."""
    uid = str(uuid.uuid4())
    cid = str(uuid.uuid4())

    acc_tag = compute_account_tag(uid)
    chat_tag = compute_chat_tag(cid)
    slug = generate_chat_slug(uid, cid)

    # Length checks
    assert len(acc_tag) == 6, f"Expected 6 chars, got {acc_tag}"
    assert len(chat_tag) == 8, f"Expected 8 chars, got {chat_tag}"
    assert slug == f"{acc_tag}-{chat_tag}"
    assert len(slug) == 15

    # Alphanumeric with both digits and letters
    assert acc_tag.isalnum()
    assert any(c.isalpha() for c in acc_tag), "Account tag should have letters"
    assert any(c.isdigit() for c in acc_tag), "Account tag should have digits"

    assert chat_tag.isalnum()
    assert any(c.isalpha() for c in chat_tag), "Chat tag should have letters"
    assert any(c.isdigit() for c in chat_tag), "Chat tag should have digits"

    # Determinism
    assert compute_account_tag(uid) == acc_tag
    assert compute_chat_tag(cid) == chat_tag


def test_title_pipeline_avoids_verbatim_echo(test_client, user_a_email):
    """Title generation for 'hello bro' must NOT be 'Hello Bro'."""
    res = test_client.post(
        "/api/title",
        headers={"X-User-Email": user_a_email, "Content-Type": "application/json"},
        json={"message": "hello bro"},
    )
    assert res.status_code == 200
    title = res.get_json().get("title")
    assert title is not None
    # Strictly must NOT be the exact words 'Hello Bro'
    assert title.lower() != "hello bro", f"Title should not echo input: {title}"
    assert "hello" not in title.lower()
    assert "bro" not in title.lower()


def test_title_uniqueness_within_account(test_client, user_a_email, user_b_email):
    """No two chats can have the same title in one account."""
    # User A creates first chat
    res1 = test_client.post(
        "/api/chats",
        headers={"X-User-Email": user_a_email, "Content-Type": "application/json"},
        json={"title": "Casual Greeting"},
    )
    assert res1.status_code == 201
    chat1_title = res1.get_json()["title"]
    assert chat1_title == "Casual Greeting"

    # User A creates second chat with same title -> automatically differentiated
    res2 = test_client.post(
        "/api/chats",
        headers={"X-User-Email": user_a_email, "Content-Type": "application/json"},
        json={"title": "Casual Greeting"},
    )
    assert res2.status_code == 201
    chat2_title = res2.get_json()["title"]
    assert chat2_title == "Casual Greeting 2"
    assert chat1_title != chat2_title

    # User A creates third chat with same title -> 'Casual Greeting 3'
    res3 = test_client.post(
        "/api/chats",
        headers={"X-User-Email": user_a_email, "Content-Type": "application/json"},
        json={"title": "Casual Greeting"},
    )
    assert res3.status_code == 201
    chat3_title = res3.get_json()["title"]
    assert chat3_title == "Casual Greeting 3"

    # User B can have 'Casual Greeting' without collision
    res_b = test_client.post(
        "/api/chats",
        headers={"X-User-Email": user_b_email, "Content-Type": "application/json"},
        json={"title": "Casual Greeting"},
    )
    assert res_b.status_code == 201
    assert res_b.get_json()["title"] == "Casual Greeting"


def test_chat_url_link_isolation(test_client, user_a_email, user_b_email):
    """Verify chat slug format and link isolation across accounts."""
    # Create chat for User A
    res_create = test_client.post(
        "/api/chats",
        headers={"X-User-Email": user_a_email, "Content-Type": "application/json"},
        json={"title": "Isolated Financial Report"},
    )
    assert res_create.status_code == 201
    data = res_create.get_json()
    chat_id = data["chat_id"]
    slug = data["slug"]

    # Verify slug format: 6 alphanumeric - 8 alphanumeric
    parts = slug.split("-")
    assert len(parts) == 2, f"Slug must contain 1 hyphen: {slug}"
    assert len(parts[0]) == 6, f"Account tag must be 6 chars: {parts[0]}"
    assert len(parts[1]) == 8, f"Chat tag must be 8 chars: {parts[1]}"
    assert parts[0].isalnum()
    assert parts[1].isalnum()

    # User A loads chat via link slug
    res_load_a = test_client.get(
        f"/api/chats/{slug}",
        headers={"X-User-Email": user_a_email},
    )
    assert res_load_a.status_code == 200
    assert res_load_a.get_json()["chat"]["id"] == chat_id

    # User B tries to load User A's isolated link -> 404 forbidden
    res_load_b = test_client.get(
        f"/api/chats/{slug}",
        headers={"X-User-Email": user_b_email},
    )
    assert res_load_b.status_code == 404
