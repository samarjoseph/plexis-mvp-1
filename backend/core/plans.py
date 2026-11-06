"""
Plexis Subscription Plans and Quota Management.

Defines feature matrices, resource constraints, and quota validators for:
- Free Plan (Standard Community Tier)
- Plus Plan (Unrestricted Pro Tier)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Tuple
from uuid import UUID

logger = logging.getLogger(__name__)

PLAN_FREE = "free"
PLAN_PLUS = "plus"

# Comprehensive feature limits per plan
PLAN_LIMITS: Dict[str, Dict[str, Any]] = {
    PLAN_FREE: {
        "name": "Free",
        "description": "Essential data analysis and exploratory workspace",
        "price_usd": 0,
        "max_chats": 10,
        "max_messages_per_chat": 50,
        "max_tokens_per_chat": 7000,
        "max_messages_total": 500,
        "max_daily_transformations": 4,
        "max_spreadsheet_rows": 15000,
        "max_dataset_rows": 40000,
        "max_upload_size_mb": 35,
        "llm_priority": "NORMAL",
        "transformation_persistence": False,
        "features": [
            "Up to 10 chats per account",
            "Max 50 messages & 7,000 tokens per chat",
            "Max 500 total messages across chats",
            "4 AI transformations per day",
            "Spreadsheet view up to 15,000 rows",
            "Upload files up to 35 MB & 40,000 rows",
            "Session-based transformation workspace",
            "Standard inference queue"
        ]
    },
    PLAN_PLUS: {
        "name": "Plus",
        "description": "Uncapped computing power, unlimited persistence, and priority intelligence",
        "price_usd": 30,
        "max_chats": None,  # Unlimited
        "max_messages_per_chat": None,  # Unlimited
        "max_tokens_per_chat": None,  # Unlimited
        "max_messages_total": None,  # Unlimited
        "max_daily_transformations": None,  # Unlimited
        "max_spreadsheet_rows": None,  # Full dataset rendering
        "max_dataset_rows": None,  # Unlimited rows
        "max_upload_size_mb": 50,
        "llm_priority": "HIGH",
        "transformation_persistence": True,
        "features": [
            "Unlimited chats per account",
            "Unlimited messages & tokens per chat",
            "Unlimited total messages",
            "Unlimited daily transformations",
            "Full dataset spreadsheet rendering (No 15,000 row cap)",
            "Upload files up to 50 MB & unlimited rows",
            "100% Persistent transformations across restarts, sleeps & reloads",
            "Dedicated VIP Priority LLM inference queue (Answered first)"
        ]
    }
}


def normalize_plan(plan: Optional[str]) -> str:
    """Normalize plan string to 'free' or 'plus'."""
    if not plan:
        return PLAN_FREE
    p = str(plan).strip().lower()
    return PLAN_PLUS if p == PLAN_PLUS else PLAN_FREE


def get_plan_limits(plan: Optional[str]) -> Dict[str, Any]:
    """Retrieve the limits dictionary for a given plan."""
    norm = normalize_plan(plan)
    return PLAN_LIMITS[norm]


def check_daily_transformation_quota(
    db,
    user_id: Optional[UUID],
    plan: Optional[str] = PLAN_FREE,
) -> Tuple[bool, int, Optional[int]]:
    """
    Check if the user has remaining transformation quota for today (UTC).

    Returns:
        (allowed: bool, used_today: int, limit: Optional[int])
    """
    norm = normalize_plan(plan)
    limit = PLAN_LIMITS[norm]["max_daily_transformations"]
    if limit is None:
        return True, 0, None  # Unlimited for Plus

    if db is None or user_id is None:
        return True, 0, limit  # Graceful fallback in stateless mode

    try:
        from db.models.transformation import TransformationPlan
        from sqlalchemy import func

        today_utc_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        from db.models.dataset import Dataset
        from sqlalchemy import or_

        user_dataset_ids = [
            row[0] for row in db.query(Dataset.id).filter(Dataset.user_id == user_id).all()
        ]

        conditions = [TransformationPlan.user_id == user_id]
        if user_dataset_ids:
            conditions.append(TransformationPlan.dataset_id.in_(user_dataset_ids))

        used = (
            db.query(func.count(TransformationPlan.id))
            .filter(
                TransformationPlan.status == "applied",
                TransformationPlan.applied_at >= today_utc_start,
                or_(*conditions),
            )
            .scalar()
        ) or 0

        allowed = used < limit
        return allowed, used, limit
    except Exception as e:
        logger.warning("[PLANS] Daily transformation quota check failed: %s", e)
        return True, 0, limit


def check_chat_creation_quota(
    db,
    user_id: Optional[UUID],
    plan: Optional[str] = PLAN_FREE,
) -> Tuple[bool, int, Optional[int]]:
    """
    Check if the user is allowed to create a new chat.

    Free plan: max 10 chats.
    Plus plan: unlimited.

    Returns:
        (allowed: bool, current_count: int, limit: Optional[int])
    """
    norm = normalize_plan(plan)
    limit = PLAN_LIMITS[norm]["max_chats"]
    if limit is None:
        return True, 0, None  # Unlimited for Plus

    if db is None or user_id is None:
        return True, 0, limit

    try:
        from db.models.chat import Chat
        from sqlalchemy import func

        chat_count = (
            db.query(func.count(Chat.id))
            .filter(Chat.user_id == user_id)
            .scalar()
        ) or 0

        allowed = chat_count < limit
        return allowed, chat_count, limit
    except Exception as e:
        logger.warning("[PLANS] Chat creation quota check failed: %s", e)
        return True, 0, limit


def check_chat_message_quota(
    db,
    user_id: Optional[UUID],
    chat_id: Optional[UUID | str],
    plan: Optional[str] = PLAN_FREE,
    new_message_text: str = "",
) -> Tuple[bool, Optional[str], Dict[str, Any]]:
    """
    Check messages per chat, tokens per chat, and total messages across all chats.

    Free plan:
    - Messages per chat: 50
    - Tokens per chat: 7,000
    - Maximum messages across all chats: 500

    Returns:
        (allowed: bool, rejection_reason: Optional[str], stats: dict)
    """
    norm = normalize_plan(plan)
    stats: Dict[str, Any] = {
        "plan": norm,
        "chat_messages": 0,
        "chat_tokens": 0,
        "total_messages": 0,
    }

    if norm == PLAN_PLUS:
        return True, None, stats

    if db is None or user_id is None:
        return True, None, stats

    try:
        from db.models.message import Message
        from db.models.chat import Chat
        from sqlalchemy import func

        # 1. Check total messages across all chats for user
        total_msgs = (
            db.query(func.count(Message.id))
            .join(Chat, Chat.id == Message.chat_id)
            .filter(Chat.user_id == user_id)
            .scalar()
        ) or 0
        stats["total_messages"] = total_msgs

        max_total = PLAN_LIMITS[PLAN_FREE]["max_messages_total"]
        if total_msgs >= max_total:
            return (
                False,
                f"Free plan limit reached: {max_total} total messages across all chats. Upgrade to Plus for unlimited messages.",
                stats,
            )

        # 2. Check per-chat limits if chat_id is provided
        if chat_id:
            try:
                c_uuid = UUID(str(chat_id))
            except (ValueError, TypeError):
                c_uuid = None

            if c_uuid:
                chat_msgs = (
                    db.query(Message)
                    .filter(Message.chat_id == c_uuid)
                    .all()
                )
                msg_count = len(chat_msgs)
                stats["chat_messages"] = msg_count

                max_per_chat = PLAN_LIMITS[PLAN_FREE]["max_messages_per_chat"]
                if msg_count >= max_per_chat:
                    return (
                        False,
                        f"Free plan limit reached: {max_per_chat} messages per chat. Upgrade to Plus for unlimited messages.",
                        stats,
                    )

                # Estimate tokens: approx 4 chars per token across chat history + new message
                total_chars = sum(len(m.content or "") for m in chat_msgs) + len(new_message_text)
                est_tokens = total_chars // 4
                stats["chat_tokens"] = est_tokens

                max_tokens = PLAN_LIMITS[PLAN_FREE]["max_tokens_per_chat"]
                if est_tokens >= max_tokens:
                    return (
                        False,
                        f"Free plan limit reached: {max_tokens:,} tokens per chat. Upgrade to Plus for unlimited tokens.",
                        stats,
                    )

        return True, None, stats
    except Exception as e:
        logger.warning("[PLANS] Chat message quota check failed: %s", e)
        return True, None, stats
