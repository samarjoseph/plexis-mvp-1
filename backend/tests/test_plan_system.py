"""
Unit tests for Plexis Plan & Subscription System.
Verifies all limits, quotas, priority scheduling, and persistence rules for Free vs Plus.
"""
import uuid
import pytest
import pandas as pd
from unittest.mock import MagicMock, patch

from core.plans import (
    PLAN_FREE,
    PLAN_PLUS,
    PLAN_LIMITS,
    get_plan_limits,
    normalize_plan,
    check_daily_transformation_quota,
    check_chat_creation_quota,
    check_chat_message_quota,
)
from providers.domain.contracts import AIRequest
from providers.priority_scheduler import PriorityRequestScheduler


class TestPlanDefinitions:
    def test_plan_limits_structure(self):
        free = get_plan_limits("free")
        plus = get_plan_limits("plus")

        # Free tier constraints
        assert free["max_chats"] == 10
        assert free["max_messages_per_chat"] == 50
        assert free["max_tokens_per_chat"] == 7000
        assert free["max_messages_total"] == 500
        assert free["max_daily_transformations"] == 4
        assert free["max_spreadsheet_rows"] == 15000
        assert free["max_dataset_rows"] == 40000
        assert free["max_upload_size_mb"] == 35
        assert free["transformation_persistence"] is False

        # Plus tier constraints
        assert plus["max_chats"] is None
        assert plus["max_messages_per_chat"] is None
        assert plus["max_tokens_per_chat"] is None
        assert plus["max_messages_total"] is None
        assert plus["max_daily_transformations"] is None
        assert plus["max_spreadsheet_rows"] is None
        assert plus["max_dataset_rows"] is None
        assert plus["max_upload_size_mb"] == 50
        assert plus["transformation_persistence"] is True

    def test_normalize_plan(self):
        assert normalize_plan(None) == PLAN_FREE
        assert normalize_plan("") == PLAN_FREE
        assert normalize_plan("free") == PLAN_FREE
        assert normalize_plan("FREE") == PLAN_FREE
        assert normalize_plan("plus") == PLAN_PLUS
        assert normalize_plan("PLUS") == PLAN_PLUS
        assert normalize_plan("unknown") == PLAN_FREE


class TestQuotas:
    def test_daily_transformation_quota_free_under_limit(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = []
        mock_db.query.return_value.filter.return_value.scalar.return_value = 2

        allowed, used, limit = check_daily_transformation_quota(mock_db, uuid.uuid4(), "free")
        assert allowed is True
        assert used == 2
        assert limit == 4

    def test_daily_transformation_quota_free_at_limit(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = []
        mock_db.query.return_value.filter.return_value.scalar.return_value = 4

        allowed, used, limit = check_daily_transformation_quota(mock_db, uuid.uuid4(), "free")
        assert allowed is False
        assert used == 4
        assert limit == 4

    def test_daily_transformation_quota_plus_unlimited(self):
        mock_db = MagicMock()
        allowed, used, limit = check_daily_transformation_quota(mock_db, uuid.uuid4(), "plus")
        assert allowed is True
        assert limit is None

    def test_chat_creation_quota_free(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.scalar.return_value = 9
        allowed, count, limit = check_chat_creation_quota(mock_db, uuid.uuid4(), "free")
        assert allowed is True
        assert count == 9
        assert limit == 10

        mock_db.query.return_value.filter.return_value.scalar.return_value = 10
        allowed, count, limit = check_chat_creation_quota(mock_db, uuid.uuid4(), "free")
        assert allowed is False
        assert count == 10
        assert limit == 10

    def test_chat_creation_quota_plus(self):
        mock_db = MagicMock()
        allowed, count, limit = check_chat_creation_quota(mock_db, uuid.uuid4(), "plus")
        assert allowed is True
        assert limit is None


class TestPriorityScheduler:
    def test_priority_scores(self):
        sched = PriorityRequestScheduler(max_concurrent=2)

        free_req = AIRequest(task="test", user_plan="free", priority="NORMAL")
        plus_req = AIRequest(task="test", user_plan="plus", priority="NORMAL")
        critical_req = AIRequest(task="test", user_plan="free", priority="CRITICAL")

        assert sched._get_priority_score(plus_req) == 0
        assert sched._get_priority_score(critical_req) == 0
        assert sched._get_priority_score(free_req) == 10

    def test_plus_request_preempts_free_queue(self):
        sched = PriorityRequestScheduler(max_concurrent=1)

        # Acquire 1 slot so scheduler is saturated
        holder = AIRequest(task="test", user_plan="free")
        sched.acquire(holder)

        dispatched_order = []
        import threading

        def run_request(name, plan):
            req = AIRequest(task="test", user_plan=plan)
            sched.acquire(req)
            dispatched_order.append(name)
            sched.release()

        # Queue 3 Free requests
        t_free1 = threading.Thread(target=run_request, args=("free1", "free"))
        t_free2 = threading.Thread(target=run_request, args=("free2", "free"))
        t_free3 = threading.Thread(target=run_request, args=("free3", "free"))

        t_free1.start()
        t_free2.start()
        t_free3.start()
        import time
        time.sleep(0.05)  # Ensure all 3 free threads are waiting in heap

        # Queue 1 Plus request
        t_plus = threading.Thread(target=run_request, args=("plus_vip", "plus"))
        t_plus.start()
        time.sleep(0.05)

        # Release the slot — Plus should be dispatched FIRST
        sched.release()

        t_plus.join(timeout=2.0)
        t_free1.join(timeout=2.0)
        t_free2.join(timeout=2.0)
        t_free3.join(timeout=2.0)

        assert len(dispatched_order) >= 1
        assert dispatched_order[0] == "plus_vip"
