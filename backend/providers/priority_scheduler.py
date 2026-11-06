"""
Priority Request Scheduler for LLM Inference.

Enforces strict SLA and tier priority:
- Plus plan users get high-priority VIP scheduling (Priority 0).
- Free plan users get standard scheduling (Priority 10).
- When multiple concurrent inference requests arrive, Plus plan requests
  are scheduled and dispatched first ahead of any waiting Free plan requests.
"""
from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

from providers.domain.contracts import AIRequest

logger = logging.getLogger(__name__)


class PriorityRequestScheduler:
    """
    Coordinates inference requests by subscriber tier.
    Uses priority-weighted queuing to guarantee that Plus subscribers
    are served ahead of Free tier users under concurrent load.
    """

    def __init__(self, max_concurrent: int = 8):
        self.max_concurrent = max_concurrent
        self.active_count = 0
        self._lock = threading.Lock()
        self._counter = itertools.count()  # Tie-breaker for heap
        self._waiting_heap: list = []  # Elements: (priority_score, sequence_id, condition)
        self._stats = {
            "total_plus_requests": 0,
            "total_free_requests": 0,
            "plus_preemptions": 0,
        }

    def _get_priority_score(self, request: AIRequest) -> int:
        """
        Lower number = higher priority.
        Plus plan / CRITICAL / HIGH = 0.
        Free plan / NORMAL = 10.
        LOW = 20.
        """
        plan = getattr(request, "user_plan", "free") or "free"
        req_prio = getattr(request, "priority", "NORMAL") or "NORMAL"

        if str(plan).strip().lower() == "plus" or req_prio in ("HIGH", "CRITICAL"):
            return 0
        if req_prio == "LOW":
            return 20
        return 10

    def acquire(self, request: AIRequest) -> None:
        """
        Block until an execution slot is available, with priority queueing.
        Plus requests cut ahead of waiting Free requests.
        """
        score = self._get_priority_score(request)
        is_plus = (score == 0)

        with self._lock:
            if is_plus:
                self._stats["total_plus_requests"] += 1
            else:
                self._stats["total_free_requests"] += 1

            # If there is immediate capacity and nobody higher priority waiting:
            if self.active_count < self.max_concurrent and not self._waiting_heap:
                self.active_count += 1
                return

            # Need to wait in priority order
            cond = threading.Condition(self._lock)
            seq = next(self._counter)
            heapq.heappush(self._waiting_heap, (score, seq, cond))

            waiting_free = sum(1 for item in self._waiting_heap if item[0] > 0)
            if is_plus and waiting_free > 0:
                self._stats["plus_preemptions"] += 1
                logger.info(
                    "[PriorityScheduler] Plus VIP request queued ahead of %d waiting Free requests (seq=%d)",
                    waiting_free, seq
                )

            while True:
                # Check if this thread's condition is at the top of the heap and capacity exists
                if self.active_count < self.max_concurrent and self._waiting_heap and self._waiting_heap[0][2] is cond:
                    heapq.heappop(self._waiting_heap)
                    self.active_count += 1
                    logger.debug("[PriorityScheduler] Request granted execution slot (score=%d seq=%d)", score, seq)
                    return
                cond.wait()

    def release(self) -> None:
        """Release execution slot and wake up the highest-priority waiting request."""
        with self._lock:
            self.active_count = max(0, self.active_count - 1)
            if self._waiting_heap:
                # Wake up the condition at the top of the priority heap
                top_item = self._waiting_heap[0]
                top_cond = top_item[2]
                top_cond.notify()

    def schedule(self, request: AIRequest, execute_fn: Callable[[], Any]) -> Any:
        """Context-managed execution wrapper."""
        self.acquire(request)
        try:
            return execute_fn()
        finally:
            self.release()

    def get_stats(self) -> Dict[str, Any]:
        """Return scheduler telemetry."""
        with self._lock:
            return {
                **self._stats,
                "active_executions": self.active_count,
                "waiting_queue_depth": len(self._waiting_heap),
            }


# Singleton instance
priority_request_scheduler = PriorityRequestScheduler(max_concurrent=6)
