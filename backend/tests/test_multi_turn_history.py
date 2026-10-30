import uuid
import pytest
from memory.conversation import ConversationMemory
from core.pipeline import request_pipeline
from conversation.context_builder import build_context_block


def test_conversation_memory_client_messages_tier1():
    mem = ConversationMemory()
    session_id = "test-session-1"
    client_msgs = [
        {"role": "user", "content": "modi ji age"},
        {"role": "assistant", "content": "Narendra Modi is 76 years old."},
    ]

    history = mem.get_history(session_id=session_id, client_messages=client_msgs)
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "modi ji age"
    assert history[1]["role"] == "assistant"
    assert history[1]["content"] == "Narendra Modi is 76 years old."


def test_context_builder_includes_conversation_history():
    history = [
        {"role": "user", "content": "modi ji age"},
        {"role": "assistant", "content": "Narendra Modi is 76 years old."},
        {"role": "user", "content": "thnks broo"},
        {"role": "assistant", "content": "Haha, thanks!"},
    ]
    prompt = build_context_block(
        message="what i asked to you before can you tell me",
        intent="conversation",
        conversation_history=history,
    )

    assert "CONVERSATION HISTORY (most recent last):" in prompt
    assert "USER: modi ji age" in prompt
    assert "ASSISTANT: Narendra Modi is 76 years old." in prompt
    assert "USER: thnks broo" in prompt
    assert "USER: what i asked to you before can you tell me" in prompt


def test_pipeline_populates_conversation_history_with_client_messages():
    session_id = f"sess-{uuid.uuid4()}"
    client_msgs = [
        {"role": "user", "content": "modi ji age"},
        {"role": "assistant", "content": "Narendra Modi was born on 17 September 1950."},
    ]

    ctx = request_pipeline.process(
        message="what i asked to you before can you tell me",
        session_id=session_id,
        client_messages=client_msgs,
    )

    assert len(ctx.conversation_history) == 2
    assert ctx.conversation_history[0]["content"] == "modi ji age"
    assert ctx.conversation_history[1]["content"] == "Narendra Modi was born on 17 September 1950."
