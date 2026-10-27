"""
Plexis Spreadsheet History and Persistence Manager.

Durable, reversible spreadsheet state with:
- Optimistic Concurrency Control (OCC) via base_revision checks
- Backblaze B2 snapshot compaction and cold-start rehydration
- Atomic cell edits and batch edits with strict inverse payloads
- Linear and branching history support (invalidating undone actions on new branches)
- In-memory L1 cache (DatasetRegistry) synchronization
- Backward-compatible PostgreSQL BYTEA fallback for legacy datasets
"""
from __future__ import annotations

import io
import uuid
import threading
import logging
from collections import OrderedDict
from typing import Optional, Dict, Any, List, Tuple
from uuid import UUID

import pandas as pd
from sqlalchemy.orm import Session
from sqlalchemy import func

from config import Config
from datasets.version_codec import serialize_dataframe, deserialize_dataframe
from db.models.dataset import Dataset, DatasetFile
from db.models.spreadsheet_action import SpreadsheetAction
from db.repositories.dataset_repository import dataset_repository
from storage.manager import storage_manager

logger = logging.getLogger(__name__)


# ── Process-level L2 DataFrame cache ─────────────────────────────────────────
# Prevents repeated B2 downloads when concurrent threads request the same
# dataset before DatasetRegistry is warmed up. Keyed by dataset_id string.
# Stores (revision, df). Max 4 slots; evicts LRU on overflow.
_L2_CACHE: "OrderedDict[str, tuple]" = OrderedDict()
_L2_LOCK = threading.Lock()
_L2_MAX = 4


def _l2_get(dataset_id: str, revision: int):
    """Return cached DataFrame if revision matches, else None."""
    with _L2_LOCK:
        entry = _L2_CACHE.get(dataset_id)
        if entry is not None and entry[0] == revision:
            _L2_CACHE.move_to_end(dataset_id)
            return entry[1]
    return None


def _l2_put(dataset_id: str, revision: int, df) -> None:
    """Insert/update cache entry, evicting LRU when over capacity."""
    with _L2_LOCK:
        _L2_CACHE[dataset_id] = (revision, df)
        _L2_CACHE.move_to_end(dataset_id)
        while len(_L2_CACHE) > _L2_MAX:
            _L2_CACHE.popitem(last=False)


def _l2_invalidate(dataset_id: str) -> None:
    """Evict a dataset from L2 (call after any mutation)."""
    with _L2_LOCK:
        _L2_CACHE.pop(dataset_id, None)


class ConcurrencyConflictError(Exception):
    """Raised when an edit is attempted against a stale base_revision."""
    def __init__(self, expected_revision: int, actual_revision: int, message: Optional[str] = None):
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__(
            message or (
                f"Concurrency conflict: requested base_revision={expected_revision} "
                f"does not match current dataset revision={actual_revision}."
            )
        )


class SpreadsheetHistoryManager:
    """
    Manages persistent spreadsheet operations, revisions, snapshots, and recovery.
    """

    @staticmethod
    def _coerce_uuid(val: UUID | str | None) -> Optional[UUID]:
        if val is None:
            return None
        if isinstance(val, UUID):
            return val
        try:
            return UUID(str(val))
        except Exception:
            return None

    def get_dataset(self, db: Session, dataset_id: UUID | str, user_id: UUID | str | None = None) -> Optional[Dataset]:
        """Fetch Dataset entity from DB with optional user ownership check."""
        d_id = self._coerce_uuid(dataset_id)
        if not d_id:
            return None
        u_id = self._coerce_uuid(user_id)
        if u_id:
            return dataset_repository.get_by_id(db, d_id, u_id)
        return dataset_repository.get_by_id_unsafe(db, d_id)

    def _get_or_restore_df(self, db: Session, dataset: Dataset) -> pd.DataFrame:
        """Get dataframe from L1 (DatasetRegistry) or L2 (process cache) before hitting B2."""
        ds_id = str(dataset.id)
        current_rev = dataset.current_revision or 0

        # L1: in-memory DatasetRegistry (fastest)
        from datasets.registry import dataset_registry
        entry = dataset_registry.get_by_id(ds_id)
        if entry and entry.dataframe is not None and not (hasattr(entry.dataframe, 'empty') and entry.dataframe.empty):
            return entry.dataframe

        # L2: process-level cache keyed by (dataset_id, revision) — avoids redundant B2 downloads
        cached_df = _l2_get(ds_id, current_rev)
        if cached_df is not None:
            logger.debug("[SpreadsheetHistory] L2 cache hit for dataset %s rev %d", ds_id, current_rev)
            self._sync_registry(dataset, cached_df)
            return cached_df

        logger.info("[SpreadsheetHistory] Cache miss for dataset %s rev %d — restoring from B2", ds_id, current_rev)
        _, df = self.restore_dataset_state(db, dataset.id, dataset.user_id)
        if df is None:
            raise RuntimeError(f"Failed to restore dataframe for dataset {dataset.id}")
        return df

    def _sync_registry(self, dataset: Dataset, df: pd.DataFrame) -> None:
        """Keep in-memory DatasetRegistry updated with current DataFrame and dimensions."""
        from datasets.registry import dataset_registry, DatasetEntry
        from datetime import datetime, timezone

        entry = dataset_registry.get_by_id(str(dataset.id))
        if entry:
            entry.dataframe = df
            entry.row_count = len(df)
            entry.column_count = len(df.columns)
        else:
            entry = DatasetEntry(
                dataset_id=str(dataset.id),
                filename=dataset.original_filename,
                file_path="",
                upload_timestamp=dataset.created_at.isoformat() if dataset.created_at else datetime.now(timezone.utc).isoformat(),
                row_count=len(df),
                column_count=len(df.columns),
                file_fingerprint=dataset.file_hash or "",
                schema_fingerprint=dataset.file_hash or "",
                dataframe=df,
                profile={},
                schema_profile=[],
                column_profiles={},
                ontology=None,
                dko=None,
            )
            dataset_registry.register(entry)

    def _apply_single_cell(self, df: pd.DataFrame, row_idx: int, col_name: str, value: Any) -> Any:
        """Safely apply a cell value to the DataFrame and return the previous value."""
        if row_idx < 0 or row_idx >= len(df):
            raise IndexError(f"Row index {row_idx} is out of bounds (0..{len(df)-1})")
        if col_name not in df.columns:
            raise KeyError(f"Column '{col_name}' does not exist in dataset")

        col_loc = df.columns.get_loc(col_name)
        old_val = df.iat[row_idx, col_loc]

        # Convert NaN / None representations
        if pd.isna(old_val):
            old_val_clean = None
        elif hasattr(old_val, 'item'):
            old_val_clean = old_val.item()
        else:
            old_val_clean = old_val

        # Handle type coercion for numeric columns if value is string or None
        target_val = value
        if value is None or (isinstance(value, str) and value.strip() == ""):
            target_val = None
        else:
            col_dtype = df[col_name].dtype
            if pd.api.types.is_integer_dtype(col_dtype):
                try:
                    target_val = int(value)
                except (ValueError, TypeError):
                    target_val = value
            elif pd.api.types.is_float_dtype(col_dtype):
                try:
                    target_val = float(value)
                except (ValueError, TypeError):
                    target_val = value

        df.iat[row_idx, col_loc] = target_val
        return old_val_clean

    def apply_cell_edit(
        self,
        db: Session,
        dataset_id: UUID | str,
        row_idx: int,
        col_name: str,
        new_value: Any,
        base_revision: int,
        user_id: UUID | str | None = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Apply a single cell edit with optimistic concurrency check.
        """
        dataset = self.get_dataset(db, dataset_id, user_id)
        if not dataset:
            raise ValueError(f"Dataset '{dataset_id}' not found")

        # 1. Optimistic Concurrency Check
        current_rev = dataset.current_revision or 0
        if base_revision != current_rev:
            raise ConcurrencyConflictError(expected_revision=base_revision, actual_revision=current_rev)

        # 2. Get DataFrame and mutate cell
        df = self._get_or_restore_df(db, dataset)
        old_val = self._apply_single_cell(df, row_idx, col_name, new_value)

        # 3. Branch invalidation: delete any undone actions ahead of base_revision
        db.query(SpreadsheetAction).filter(
            SpreadsheetAction.dataset_id == dataset.id,
            SpreadsheetAction.status == "undone",
            SpreadsheetAction.base_revision >= base_revision,
        ).delete(synchronize_session=False)

        # 4. Determine next sequence number
        max_seq = (
            db.query(func.max(SpreadsheetAction.sequence_number))
            .filter(SpreadsheetAction.dataset_id == dataset.id)
            .scalar()
        ) or 0
        next_seq = max_seq + 1
        resulting_rev = current_rev + 1

        # 5. Build action record
        storage_acc = dataset.storage_account or "b2"
        action = SpreadsheetAction(
            id=uuid.uuid4(),
            dataset_id=dataset.id,
            user_id=self._coerce_uuid(user_id) or dataset.user_id,
            session_id=session_id,
            base_revision=base_revision,
            resulting_revision=resulting_rev,
            sequence_number=next_seq,
            action_type="CELL_EDIT",
            operation_payload={"row": row_idx, "col": col_name, "value": new_value},
            inverse_payload={"row": row_idx, "col": col_name, "value": old_val},
            storage_account=storage_acc,
            status="committed",
        )

        # 6. Periodic snapshot compaction
        snapshot_key = None
        if resulting_rev % getattr(Config, "SNAPSHOT_INTERVAL_ACTIONS", 20) == 0:
            try:
                snapshot_bytes = serialize_dataframe(df)
                snap_key = storage_manager.build_snapshot_key(
                    user_id=dataset.user_id,
                    dataset_id=dataset.id,
                    revision=resulting_rev,
                    ext="plexis.json",
                )
                storage_manager.put_object(
                    key=snap_key,
                    data=snapshot_bytes,
                    account_identifier=storage_acc,
                    content_type="application/json",
                )
                action.storage_key = snap_key
                dataset.storage_key = snap_key
                snapshot_key = snap_key
                logger.info(
                    "[SpreadsheetHistory] Created Backblaze B2 snapshot for dataset %s at rev %d (%s)",
                    dataset.id, resulting_rev, snap_key,
                )
            except Exception as e:
                logger.warning("[SpreadsheetHistory] Failed to write Backblaze B2 snapshot (non-fatal): %s", e)

        # 7. Update dataset revision & sync registry
        dataset.current_revision = resulting_rev
        db.add(action)
        db.commit()
        db.refresh(action)

        self._sync_registry(dataset, df)
        _l2_put(str(dataset.id), dataset.current_revision or 0, df)

        return {
            "success": True,
            "action_id": str(action.id),
            "base_revision": base_revision,
            "resulting_revision": resulting_rev,
            "row": row_idx,
            "col": col_name,
            "value": new_value,
            "old_value": old_val,
            "snapshot_key": snapshot_key,
        }

    def apply_batch_edit(
        self,
        db: Session,
        dataset_id: UUID | str,
        edits: List[Dict[str, Any]],
        base_revision: int,
        user_id: UUID | str | None = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Apply a batch of cell edits atomically.
        Each item in edits: {row: int, col: str, value: Any}
        """
        if not edits:
            raise ValueError("edits list cannot be empty")

        dataset = self.get_dataset(db, dataset_id, user_id)
        if not dataset:
            raise ValueError(f"Dataset '{dataset_id}' not found")

        current_rev = dataset.current_revision or 0
        if base_revision != current_rev:
            raise ConcurrencyConflictError(expected_revision=base_revision, actual_revision=current_rev)

        df = self._get_or_restore_df(db, dataset)

        applied_ops = []
        inverse_ops = []

        for item in edits:
            r = item.get("row")
            c = item.get("col")
            v = item.get("value")
            if r is None or c is None:
                continue
            old_v = self._apply_single_cell(df, int(r), str(c), v)
            applied_ops.append({"row": int(r), "col": str(c), "value": v})
            inverse_ops.append({"row": int(r), "col": str(c), "value": old_v})

        # Invalidate undone branches
        db.query(SpreadsheetAction).filter(
            SpreadsheetAction.dataset_id == dataset.id,
            SpreadsheetAction.status == "undone",
            SpreadsheetAction.base_revision >= base_revision,
        ).delete(synchronize_session=False)

        max_seq = (
            db.query(func.max(SpreadsheetAction.sequence_number))
            .filter(SpreadsheetAction.dataset_id == dataset.id)
            .scalar()
        ) or 0
        next_seq = max_seq + 1
        resulting_rev = current_rev + 1

        storage_acc = dataset.storage_account or "b2"
        action = SpreadsheetAction(
            id=uuid.uuid4(),
            dataset_id=dataset.id,
            user_id=self._coerce_uuid(user_id) or dataset.user_id,
            session_id=session_id,
            base_revision=base_revision,
            resulting_revision=resulting_rev,
            sequence_number=next_seq,
            action_type="BATCH_CELL_EDIT",
            operation_payload={"edits": applied_ops},
            inverse_payload={"edits": inverse_ops},
            storage_account=storage_acc,
            status="committed",
        )

        snapshot_key = None
        if resulting_rev % getattr(Config, "SNAPSHOT_INTERVAL_ACTIONS", 20) == 0:
            try:
                snapshot_bytes = serialize_dataframe(df)
                snap_key = storage_manager.build_snapshot_key(
                    user_id=dataset.user_id,
                    dataset_id=dataset.id,
                    revision=resulting_rev,
                    ext="plexis.json",
                )
                storage_manager.put_object(
                    key=snap_key,
                    data=snapshot_bytes,
                    account_identifier=storage_acc,
                    content_type="application/json",
                )
                action.storage_key = snap_key
                dataset.storage_key = snap_key
                snapshot_key = snap_key
            except Exception as e:
                logger.warning("[SpreadsheetHistory] Failed to write Backblaze B2 batch snapshot: %s", e)

        dataset.current_revision = resulting_rev
        db.add(action)
        db.commit()
        db.refresh(action)

        self._sync_registry(dataset, df)
        _l2_put(str(dataset.id), dataset.current_revision or 0, df)

        return {
            "success": True,
            "action_id": str(action.id),
            "base_revision": base_revision,
            "resulting_revision": resulting_rev,
            "edit_count": len(applied_ops),
            "snapshot_key": snapshot_key,
        }

    def record_transformation(
        self,
        db: Session,
        dataset: Dataset,
        operation_label: str,
        new_df: pd.DataFrame,
        user_id: UUID | str | None = None,
        session_id: Optional[str] = None,
    ) -> SpreadsheetAction:
        """
        Record a full transformation as an action with mandatory R2 snapshot.
        """
        base_rev = dataset.current_revision or 0
        resulting_rev = base_rev + 1
        storage_acc = dataset.storage_account or "b2"

        # Invalidate undone branches
        db.query(SpreadsheetAction).filter(
            SpreadsheetAction.dataset_id == dataset.id,
            SpreadsheetAction.status == "undone",
            SpreadsheetAction.base_revision >= base_rev,
        ).delete(synchronize_session=False)

        max_seq = (
            db.query(func.max(SpreadsheetAction.sequence_number))
            .filter(SpreadsheetAction.dataset_id == dataset.id)
            .scalar()
        ) or 0
        next_seq = max_seq + 1

        # Upload snapshot to R2
        snap_key = None
        try:
            snapshot_bytes = serialize_dataframe(new_df)
            snap_key = storage_manager.build_snapshot_key(
                user_id=dataset.user_id,
                dataset_id=dataset.id,
                revision=resulting_rev,
                ext="plexis.json",
            )
            storage_manager.put_object(
                key=snap_key,
                data=snapshot_bytes,
                account_identifier=storage_acc,
                content_type="application/json",
            )
            dataset.storage_key = snap_key
        except Exception as e:
            logger.error("[SpreadsheetHistory] Failed to upload transformation snapshot to Backblaze B2: %s", e)

        action = SpreadsheetAction(
            id=uuid.uuid4(),
            dataset_id=dataset.id,
            user_id=self._coerce_uuid(user_id) or dataset.user_id,
            session_id=session_id,
            base_revision=base_rev,
            resulting_revision=resulting_rev,
            sequence_number=next_seq,
            action_type="TRANSFORMATION",
            operation_payload={"label": operation_label},
            inverse_payload={"previous_storage_key": dataset.storage_key},
            storage_account=storage_acc,
            storage_key=snap_key,
            status="committed",
        )

        dataset.current_revision = resulting_rev
        db.add(action)
        db.commit()
        db.refresh(action)

        self._sync_registry(dataset, new_df)
        return action

    def undo(
        self,
        db: Session,
        dataset_id: UUID | str,
        user_id: UUID | str | None = None,
    ) -> Dict[str, Any]:
        """
        Undo the latest committed action for a dataset.
        Reverts the DataFrame and updates dataset.current_revision.
        """
        dataset = self.get_dataset(db, dataset_id, user_id)
        if not dataset:
            raise ValueError(f"Dataset '{dataset_id}' not found")

        current_rev = dataset.current_revision or 0
        if current_rev <= 0:
            return {"success": False, "error_code": "NO_HISTORY", "error": "Nothing to undo."}

        # Find action that brought dataset to current_rev
        action = (
            db.query(SpreadsheetAction)
            .filter(
                SpreadsheetAction.dataset_id == dataset.id,
                SpreadsheetAction.resulting_revision == current_rev,
                SpreadsheetAction.status == "committed",
            )
            .first()
        )

        if not action:
            # Fallback check by sequence_number
            action = (
                db.query(SpreadsheetAction)
                .filter(
                    SpreadsheetAction.dataset_id == dataset.id,
                    SpreadsheetAction.status == "committed",
                )
                .order_by(SpreadsheetAction.sequence_number.desc())
                .first()
            )

        if not action:
            return {"success": False, "error_code": "NO_HISTORY", "error": "No committed action found to undo."}

        df = self._get_or_restore_df(db, dataset)

        # Apply reversal
        if action.action_type == "CELL_EDIT":
            inv = action.inverse_payload or {}
            self._apply_single_cell(df, inv.get("row", 0), inv.get("col", ""), inv.get("value"))
        elif action.action_type == "BATCH_CELL_EDIT":
            inv = action.inverse_payload or {}
            for item in inv.get("edits", []):
                self._apply_single_cell(df, item.get("row", 0), item.get("col", ""), item.get("value"))
        elif action.action_type == "TRANSFORMATION":
            # For transformations, restore to previous snapshot / baseline
            target_rev = action.base_revision
            prev_action = (
                db.query(SpreadsheetAction)
                .filter(
                    SpreadsheetAction.dataset_id == dataset.id,
                    SpreadsheetAction.resulting_revision <= target_rev,
                    SpreadsheetAction.storage_key.isnot(None),
                )
                .order_by(SpreadsheetAction.resulting_revision.desc())
                .first()
            )
            if prev_action and prev_action.storage_key:
                raw = storage_manager.get_object(prev_action.storage_key, prev_action.storage_account)
                df = deserialize_dataframe(raw)
            else:
                # Restore baseline
                _, restored_df = self.restore_dataset_state(db, dataset.id, dataset.user_id, override_revision=target_rev)
                if restored_df is not None:
                    df = restored_df

        action.status = "undone"
        dataset.current_revision = action.base_revision
        db.commit()

        self._sync_registry(dataset, df)

        return {
            "success": True,
            "message": f"Undone action {action.action_type} (rev {action.resulting_revision} -> {action.base_revision})",
            "current_revision": dataset.current_revision,
            "action_id": str(action.id),
            "restored_rows": len(df),
            "restored_columns": len(df.columns),
        }

    def redo(
        self,
        db: Session,
        dataset_id: UUID | str,
        user_id: UUID | str | None = None,
    ) -> Dict[str, Any]:
        """
        Redo the next undone action for a dataset.
        Re-applies the DataFrame change and advances dataset.current_revision.
        """
        dataset = self.get_dataset(db, dataset_id, user_id)
        if not dataset:
            raise ValueError(f"Dataset '{dataset_id}' not found")

        current_rev = dataset.current_revision or 0

        # Find the next undone action starting from current_rev
        action = (
            db.query(SpreadsheetAction)
            .filter(
                SpreadsheetAction.dataset_id == dataset.id,
                SpreadsheetAction.base_revision == current_rev,
                SpreadsheetAction.status == "undone",
            )
            .order_by(SpreadsheetAction.sequence_number.asc())
            .first()
        )

        if not action:
            return {"success": False, "error_code": "NO_REDO", "error": "Nothing to redo."}

        df = self._get_or_restore_df(db, dataset)

        # Apply forward payload
        if action.action_type == "CELL_EDIT":
            fwd = action.operation_payload or {}
            self._apply_single_cell(df, fwd.get("row", 0), fwd.get("col", ""), fwd.get("value"))
        elif action.action_type == "BATCH_CELL_EDIT":
            fwd = action.operation_payload or {}
            for item in fwd.get("edits", []):
                self._apply_single_cell(df, item.get("row", 0), item.get("col", ""), item.get("value"))
        elif action.action_type == "TRANSFORMATION":
            if action.storage_key:
                raw = storage_manager.get_object(action.storage_key, action.storage_account)
                df = deserialize_dataframe(raw)
            else:
                _, restored_df = self.restore_dataset_state(db, dataset.id, dataset.user_id, override_revision=action.resulting_revision)
                if restored_df is not None:
                    df = restored_df

        action.status = "committed"
        dataset.current_revision = action.resulting_revision
        db.commit()

        self._sync_registry(dataset, df)

        return {
            "success": True,
            "message": f"Redone action {action.action_type} (rev {action.base_revision} -> {action.resulting_revision})",
            "current_revision": dataset.current_revision,
            "action_id": str(action.id),
            "restored_rows": len(df),
            "restored_columns": len(df.columns),
        }

    def restore_dataset_state(
        self,
        db: Session,
        dataset_id: UUID | str,
        user_id: UUID | str | None = None,
        override_revision: Optional[int] = None,
    ) -> Tuple[Optional[Dataset], Optional[pd.DataFrame]]:
        """
        Cold-start rehydration:
        1. Fetch Dataset record.
        2. Identify latest valid snapshot from R2 at or before target revision.
        3. If snapshot exists: deserialize and replay remaining committed actions.
        4. If no snapshot exists: load original file from R2 (or fallback to DB BYTEA)
           and replay all committed actions up to target revision.
        5. Sync restored state into in-memory DatasetRegistry.
        """
        dataset = self.get_dataset(db, dataset_id, user_id)
        if not dataset:
            logger.warning("[SpreadsheetHistory] Cannot restore: dataset %s not found", dataset_id)
            return None, None

        target_rev = override_revision if override_revision is not None else (dataset.current_revision or 0)
        storage_acc = dataset.storage_account or "b2"

        df: Optional[pd.DataFrame] = None
        base_rev_loaded = 0

        # Step 2: Check for a snapshot at or before target_rev
        snapshot_action = (
            db.query(SpreadsheetAction)
            .filter(
                SpreadsheetAction.dataset_id == dataset.id,
                SpreadsheetAction.resulting_revision <= target_rev,
                SpreadsheetAction.storage_key.isnot(None),
                SpreadsheetAction.status == "committed",
            )
            .order_by(SpreadsheetAction.resulting_revision.desc())
            .first()
        )

        if snapshot_action and snapshot_action.storage_key:
            try:
                logger.info(
                    "[SpreadsheetHistory] Loading Backblaze B2 snapshot %s (account=%s) for dataset %s",
                    snapshot_action.storage_key, snapshot_action.storage_account, dataset.id,
                )
                raw_snap = storage_manager.get_object(snapshot_action.storage_key, snapshot_action.storage_account)
                df = deserialize_dataframe(raw_snap)
                base_rev_loaded = snapshot_action.resulting_revision
            except Exception as e:
                logger.warning("[SpreadsheetHistory] Failed reading snapshot %s: %s; trying base", snapshot_action.storage_key, e)
                df = None

        # Step 3: If no snapshot, load original dataset file from R2 or DB BYTEA
        if df is None:
            if dataset.storage_key and storage_manager.is_account_configured(storage_acc):
                try:
                    logger.info("[SpreadsheetHistory] Loading original file from Backblaze B2 key=%s", dataset.storage_key)
                    raw_file = storage_manager.get_object(dataset.storage_key, storage_acc)
                    from db.services.dataset_service import dataset_service
                    df = dataset_service._bytes_to_dataframe(raw_file, dataset.original_filename)
                    base_rev_loaded = 0
                except Exception as e:
                    logger.warning("[SpreadsheetHistory] Backblaze B2 original file fetch failed (%s): %s", dataset.storage_key, e)
                    df = None

            # Legacy fallback: PostgreSQL BYTEA in dataset_files
            if df is None:
                logger.info("[SpreadsheetHistory] Attempting legacy PostgreSQL BYTEA restore for %s", dataset.id)
                raw_bytes = dataset_repository.get_file_bytes(db, dataset.id, user_id=None)
                if raw_bytes is not None:
                    from db.services.dataset_service import dataset_service
                    df = dataset_service._bytes_to_dataframe(raw_bytes, dataset.original_filename)
                    base_rev_loaded = 0

        if df is None:
            logger.error("[SpreadsheetHistory] Completely unable to reconstruct DataFrame for dataset %s", dataset.id)
            return dataset, None

        # Step 4: Replay committed actions between base_rev_loaded and target_rev
        if base_rev_loaded < target_rev:
            subsequent_actions = (
                db.query(SpreadsheetAction)
                .filter(
                    SpreadsheetAction.dataset_id == dataset.id,
                    SpreadsheetAction.resulting_revision > base_rev_loaded,
                    SpreadsheetAction.resulting_revision <= target_rev,
                    SpreadsheetAction.status == "committed",
                )
                .order_by(SpreadsheetAction.sequence_number.asc())
                .all()
            )
            for act in subsequent_actions:
                if act.action_type == "CELL_EDIT":
                    payload = act.operation_payload or {}
                    self._apply_single_cell(df, payload.get("row", 0), payload.get("col", ""), payload.get("value"))
                elif act.action_type == "BATCH_CELL_EDIT":
                    payload = act.operation_payload or {}
                    for item in payload.get("edits", []):
                        self._apply_single_cell(df, item.get("row", 0), item.get("col", ""), item.get("value"))
                elif act.action_type == "TRANSFORMATION":
                    if act.storage_key:
                        raw = storage_manager.get_object(act.storage_key, act.storage_account)
                        df = deserialize_dataframe(raw)

        # Step 5: Sync L1 cache
        self._sync_registry(dataset, df)
        _l2_put(str(dataset.id), dataset.current_revision or 0, df)
        return dataset, df

    def get_history(
        self,
        db: Session,
        dataset_id: UUID | str,
        user_id: UUID | str | None = None,
    ) -> Dict[str, Any]:
        """
        Return the action history, current revision, and undo/redo capabilities.
        """
        dataset = self.get_dataset(db, dataset_id, user_id)
        if not dataset:
            raise ValueError(f"Dataset '{dataset_id}' not found")

        current_rev = dataset.current_revision or 0

        actions = (
            db.query(SpreadsheetAction)
            .filter(SpreadsheetAction.dataset_id == dataset.id)
            .order_by(SpreadsheetAction.sequence_number.asc())
            .all()
        )

        can_undo = any(a.status == "committed" and a.resulting_revision == current_rev for a in actions)
        can_redo = any(a.status == "undone" and a.base_revision == current_rev for a in actions)

        formatted_actions = [
            {
                "id": str(a.id),
                "sequence_number": a.sequence_number,
                "base_revision": a.base_revision,
                "resulting_revision": a.resulting_revision,
                "action_type": a.action_type,
                "status": a.status,
                "payload": a.operation_payload,
                "has_snapshot": bool(a.storage_key),
                "storage_account": a.storage_account,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in actions
        ]

        return {
            "dataset_id": str(dataset.id),
            "current_revision": current_rev,
            "storage_account": dataset.storage_account,
            "storage_key": dataset.storage_key,
            "can_undo": can_undo,
            "can_redo": can_redo,
            "actions_count": len(actions),
            "actions": formatted_actions,
        }


# Global singleton instance
spreadsheet_history_manager = SpreadsheetHistoryManager()
