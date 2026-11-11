"""
TransformationHistory — in-memory undo history with dataset versioning.

Provides a reliable undo stack that works independently of PostgreSQL.
Every successful mutation records an entry. No-ops, previews, and failures
record nothing.

INVARIANTS:
  1. Every successful mutation creates exactly ONE undo entry.
  2. Failed/no-op/preview transforms create ZERO entries.
  3. Undo restores the immediately previous DataFrame state.
  4. Dataset version increments monotonically per dataset.
  5. Stale plans (created against an older version) are rejected.
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Maximum undo depth per dataset (memory safety)
MAX_UNDO_DEPTH = 10  # Reduced from 50 to prevent Render 512MB OOM


class UndoEntry:
    """
    A single undo-history record for one successful transformation.
    Memory-optimized: uses compact compressed parquet snapshots to prevent Render 512MB RAM exhaustion.
    """
    def __init__(
        self,
        dataset_id: str,
        plan_id: str,
        operation_type: str,
        operation_label: str,
        previous_df: pd.DataFrame,
        previous_version: int,
        result_version: int,
        changed_cells: int = 0,
        affected_columns: Optional[List[str]] = None,
        timestamp: Optional[str] = None,
    ):
        self.dataset_id = dataset_id
        self.plan_id = plan_id
        self.operation_type = operation_type
        self.operation_label = operation_label
        self.previous_version = previous_version
        self.result_version = result_version
        self.changed_cells = changed_cells
        self.affected_columns = affected_columns or []
        self.timestamp = timestamp or datetime.now(timezone.utc).isoformat()
        
        # Cache active DataFrame for immediate undo, compress to Parquet bytes for older stack entries
        self._cached_df = previous_df  # Single reference; .copy() deferred to .previous_df getter
        try:
            import io
            buf = io.BytesIO()
            previous_df.to_parquet(buf, compression='snappy')
            self._parquet_bytes = buf.getvalue()
        except Exception:
            self._parquet_bytes = None

    @property
    def previous_df(self) -> pd.DataFrame:
        if self._cached_df is not None:
            return self._cached_df.copy()
        if self._parquet_bytes is not None:
            import io
            return pd.read_parquet(io.BytesIO(self._parquet_bytes))
        raise RuntimeError("No snapshot available in UndoEntry")

    def evict_ram_cache(self) -> None:
        """Evict the heavy DataFrame from RAM, retaining only compressed Parquet bytes."""
        if self._parquet_bytes is not None:
            self._cached_df = None


class TransformationHistory:
    """
    Thread-safe in-memory undo history with dataset version tracking.

    Each dataset has:
      - A monotonically increasing version counter
      - A stack of UndoEntry records (LIFO)
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._versions: Dict[str, int] = defaultdict(int)       # dataset_id → version
        self._history: Dict[str, List[UndoEntry]] = defaultdict(list)  # dataset_id → stack

    # ── Version tracking ──────────────────────────────────────────────────────

    def get_version(self, dataset_id: str) -> int:
        """Get the current version number for a dataset."""
        with self._lock:
            return self._versions[dataset_id]

    def increment_version(self, dataset_id: str) -> int:
        """Increment and return the new version for a dataset."""
        with self._lock:
            self._versions[dataset_id] += 1
            return self._versions[dataset_id]

    # ── History recording ─────────────────────────────────────────────────────

    def record(
        self,
        dataset_id: str,
        plan_id: str,
        operation_type: str,
        operation_label: str,
        previous_df: pd.DataFrame,
        previous_version: int,
        result_version: int,
        changed_cells: int = 0,
        affected_columns: Optional[List[str]] = None,
    ) -> None:
        """
        Record a successful transformation for undo.

        MUST be called only after successful mutation + version increment.
        """
        entry = UndoEntry(
            dataset_id=dataset_id,
            plan_id=plan_id,
            operation_type=operation_type,
            operation_label=operation_label,
            previous_df=previous_df,  # executor passes original df, UndoEntry copies once
            previous_version=previous_version,
            result_version=result_version,
            changed_cells=changed_cells,
            affected_columns=affected_columns or [],
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        with self._lock:
            stack = self._history[dataset_id]
            stack.append(entry)

            # Memory optimization: evict full DataFrames from RAM for entries older than the latest 2
            for old_entry in stack[:-1]:
                old_entry.evict_ram_cache()

            # Trim old entries to respect MAX_UNDO_DEPTH
            if len(stack) > MAX_UNDO_DEPTH:
                removed = stack[:len(stack) - MAX_UNDO_DEPTH]
                self._history[dataset_id] = stack[-MAX_UNDO_DEPTH:]
                logger.info(
                    "[History] Trimmed %d old undo entries for dataset %s",
                    len(removed), dataset_id,
                )

        logger.info(
            "[History] Recorded undo entry: dataset=%s plan=%s op=%s version=%d→%d",
            dataset_id, plan_id, operation_type, previous_version, result_version,
        )

    # ── Undo ──────────────────────────────────────────────────────────────────

    def has_history(self, dataset_id: str) -> bool:
        """Check whether a dataset has any undo history."""
        with self._lock:
            stack = self._history.get(dataset_id, [])
            return len(stack) > 0

    def peek(self, dataset_id: str) -> Optional[UndoEntry]:
        """Peek at the most recent undo entry without consuming it."""
        with self._lock:
            stack = self._history.get(dataset_id, [])
            return stack[-1] if stack else None

    def pop(self, dataset_id: str) -> Optional[UndoEntry]:
        """
        Pop and return the most recent undo entry.

        Returns None if no history exists.
        Also increments version (undo is itself a dataset mutation).
        """
        with self._lock:
            stack = self._history.get(dataset_id, [])
            if not stack:
                return None
            entry = stack.pop()
            # Undo increments version
            self._versions[dataset_id] += 1
            return entry

    def get_depth(self, dataset_id: str) -> int:
        """Return how many undo entries exist for a dataset."""
        with self._lock:
            return len(self._history.get(dataset_id, []))

    def clear(self, dataset_id: str) -> None:
        """Clear all undo history for a dataset."""
        with self._lock:
            self._history.pop(dataset_id, None)


# Module-level singleton
transformation_history = TransformationHistory()
