"""
Dataset service — high-level dataset operations bridging PostgreSQL and in-memory caches.

Handles:
- Persist dataset (deduplicate by user-scoped hash, store BYTEA)
- Restore dataset from PostgreSQL to in-memory DatasetRegistry
- Get dataset with DataFrame (cache-first, DB fallback)
- DKO pipeline integration
"""
import hashlib
import io
import logging
from typing import Optional, Tuple
from uuid import UUID

from sqlalchemy.orm import Session

from db.models.dataset import Dataset
from db.repositories.dataset_repository import dataset_repository

logger = logging.getLogger(__name__)


class DatasetService:
    """
    High-level dataset operations.

    L1 Cache: DatasetRegistry (in-memory)
    L2 Source of Truth: PostgreSQL datasets + dataset_files tables

    If all processes restart and L1 is empty, restore from PostgreSQL.
    """

    def persist_dataset(
        self,
        db: Session,
        user_id: UUID,
        file_obj,
        filename: str,
        mime_type: str = "text/csv",
    ) -> Dataset:
        """
        Persist a dataset for a user with R2 storage and PostgreSQL metadata.
        """
        import uuid
        file_obj.seek(0)
        file_bytes = file_obj.read()

        file_size = len(file_bytes)
        file_hash = hashlib.sha256(file_bytes).hexdigest()
        logger.debug("[DATASET_SVC] Hash computed: %s for %s (%d bytes)", file_hash[:12] + "...", filename, file_size)

        # ── Check per-upload limit (MAX_UPLOAD_SIZE_MB) ──
        from config import Config
        max_upload_bytes = getattr(Config, "MAX_UPLOAD_SIZE_MB", 100) * 1024 * 1024
        if file_size > max_upload_bytes:
            raise ValueError(
                f"File size exceeds maximum allowed upload limit of {Config.MAX_UPLOAD_SIZE_MB} MB."
            )

        # ── User-scoped deduplication check (reuse existing if identical file hash) ──
        existing = dataset_repository.get_by_hash(db, file_hash, user_id)
        if existing:
            logger.info(
                "[DATASET_SVC] Found existing dataset id=%s for user=%s hash=%s — reusing",
                existing.id, user_id, file_hash[:12]
            )
            if filename and existing.original_filename != filename:
                existing.original_filename = filename
                db.flush()
            return existing

        dataset_id = uuid.uuid4()
        storage_account = "b2"
        storage_key = None

        # ── Pre-upload quota check & write to Backblaze B2 ──
        from storage.manager import storage_manager
        from storage.contracts import StorageQuotaExceededError
        try:
            storage_key = storage_manager.build_original_key(
                user_id=user_id,
                dataset_id=dataset_id,
                filename=filename,
            )
            storage_manager.put_object(
                key=storage_key,
                data=file_bytes,
                account_identifier=storage_account,
                content_type=mime_type,
                check_quota=True,
            )
            logger.info("[DATASET_SVC] Uploaded original file to Backblaze B2 key=%s", storage_key)
        except StorageQuotaExceededError as qe:
            logger.error("[DATASET_SVC] Upload blocked by storage quota limit: %s", qe)
            raise ValueError(str(qe))
        except Exception as e:
            logger.warning("[DATASET_SVC] Backblaze B2 upload failed (non-fatal, falling back to DB BYTEA): %s", e)
            storage_key = None

        try:
            # Create new dataset record + BYTEA (for legacy fallback)
            dataset = dataset_repository.create_dataset(
                db,
                user_id=user_id,
                original_filename=filename,
                file_bytes=file_bytes,
                file_hash=file_hash,
                mime_type=mime_type,
                dataset_id=dataset_id,
                storage_account=storage_account,
                storage_key=storage_key,
            )
            return dataset
        except Exception as db_err:
            db.rollback()
            # If concurrent upload happened, check if dataset was committed
            existing = dataset_repository.get_by_hash(db, file_hash, user_id)
            if existing:
                logger.info("[DATASET_SVC] Recovered existing dataset id=%s after conflict", existing.id)
                return existing
            if storage_key:
                try:
                    storage_manager.delete_object(storage_key, storage_account)
                except Exception:
                    pass
            raise db_err

    def restore_dataset_df(
        self,
        db: Session,
        dataset_id: UUID,
        user_id: Optional[UUID] = None,
    ):
        """
        Restore a DataFrame from Backblaze B2 / PostgreSQL storage.
        Re-registers in DatasetRegistry L1 cache.
        """
        import pandas as pd

        if user_id is not None:
            dataset = dataset_repository.get_by_id(db, dataset_id, user_id)
        else:
            dataset = dataset_repository.get_by_id_unsafe(db, dataset_id)
            
        if not dataset:
            logger.warning("[DATASET_SVC] Dataset %s not found for user %s", dataset_id, user_id)
            return None, None

        # 1. Try spreadsheet history manager first if revisions / snapshots exist
        try:
            from spreadsheet.persistence import spreadsheet_history_manager
            restored_ds, restored_df = spreadsheet_history_manager.restore_dataset_state(db, dataset_id, user_id)
            if restored_df is not None:
                return restored_ds or dataset, restored_df
        except Exception as e:
            logger.debug("[DATASET_SVC] spreadsheet_history_manager restore skipped: %s", e)

        # 2. Check latest version from transformation_repository
        from db.repositories.transformation_repository import transformation_repository
        latest = transformation_repository.get_latest_version(db, dataset_id)
        content_dataset_id = latest.version_dataset_id if latest else dataset_id
        if latest:
            if user_id is not None:
                content_dataset = dataset_repository.get_by_id(db, content_dataset_id, user_id)
            else:
                content_dataset = dataset_repository.get_by_id_unsafe(db, content_dataset_id)
        else:
            content_dataset = dataset

        raw_bytes = None
        # 3. Check if content_dataset has R2 storage_key
        if content_dataset and getattr(content_dataset, 'storage_key', None):
            try:
                from storage.manager import storage_manager
                acc = getattr(content_dataset, 'storage_account', 'primary') or 'primary'
                raw_bytes = storage_manager.get_object(content_dataset.storage_key, acc)
            except Exception as e:
                logger.warning("[DATASET_SVC] Failed fetching object storage key %s: %s", content_dataset.storage_key, e)
                raw_bytes = None

        # 4. Fallback: PostgreSQL BYTEA in dataset_files
        if raw_bytes is None:
            raw_bytes = dataset_repository.get_file_bytes(db, content_dataset_id, user_id)

        if raw_bytes is None:
            logger.error("[DATASET_SVC] No file bytes for dataset %s", dataset_id)
            return dataset, None

        try:
            filename = content_dataset.original_filename if content_dataset else dataset.original_filename
            df = self._bytes_to_dataframe(raw_bytes, filename)
        except Exception as e:
            logger.error("[DATASET_SVC] Failed to reconstruct DataFrame for %s: %s", dataset_id, e)
            return dataset, None

        # Re-register in in-memory L1 cache
        self._register_in_cache(dataset, df)

        logger.info(
            "[DATASET_SVC] Restored dataset %s from DB version=%s (%d rows × %d cols)",
            dataset_id, latest.version_number if latest else 0, len(df), len(df.columns),
        )
        return dataset, df

    def get_dataset_with_df(
        self,
        db: Session,
        dataset_id: UUID,
        user_id: Optional[UUID] = None,
    ) -> Tuple[Optional[Dataset], object]:
        """
        Get a Dataset and its DataFrame.

        Check L1 cache (DatasetRegistry) first.
        On cache miss, restore from PostgreSQL and re-populate cache.

        Returns:
            (Dataset, DataFrame) or (Dataset, None) or (None, None)
        """
        # L1 cache check
        cached = self._get_from_cache(dataset_id)
        if cached is not None:
            dataset = dataset_repository.get_by_id(db, dataset_id, user_id)
            if dataset:
                logger.debug("[DATASET_SVC] L1 cache hit for dataset %s", dataset_id)
                return dataset, cached

        # L2 PostgreSQL restore
        logger.info("[DATASET_SVC] L1 cache miss for dataset %s — restoring from DB", dataset_id)
        return self.restore_dataset_df(db, dataset_id, user_id)

    def update_dko(
        self,
        db: Session,
        dataset_id: UUID,
        user_id: UUID,
        dko,
    ) -> None:
        """
        Serialize and persist a DKO to PostgreSQL JSONB.
        Called after the DKO analysis pipeline completes.
        """
        from db.dko_serializer import dko_serializer
        try:
            dko_dict = dko_serializer.serialize(dko)
            dataset_repository.update_dko(db, dataset_id, user_id, dko_dict)
            logger.info("[DATASET_SVC] DKO persisted for dataset %s", dataset_id)
        except Exception as e:
            logger.error("[DATASET_SVC] Failed to persist DKO for %s: %s", dataset_id, e)

    def update_metadata(
        self,
        db: Session,
        dataset_id: UUID,
        user_id: UUID,
        schema_json: Optional[dict] = None,
        row_count: Optional[int] = None,
        column_count: Optional[int] = None,
        quality_score: Optional[float] = None,
    ) -> None:
        """Update dataset metadata after analysis."""
        dataset_repository.update_schema_profile(
            db, dataset_id, user_id,
            schema_json=schema_json,
            row_count=row_count,
            column_count=column_count,
            quality_score=quality_score,
        )

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _bytes_to_dataframe(self, raw_bytes: bytes, filename: str):
        """Convert raw file bytes to a pandas DataFrame."""
        import pandas as pd

        if filename.lower().endswith('.plexis.json'):
            from datasets.version_codec import deserialize_dataframe
            return deserialize_dataframe(raw_bytes)

        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "csv"
        buf = io.BytesIO(raw_bytes)

        if ext == "csv":
            return pd.read_csv(buf)
        elif ext in ("xlsx", "xls"):
            return pd.read_excel(buf)
        elif ext == "json":
            return pd.read_json(buf)
        elif ext == "parquet":
            return pd.read_parquet(buf)
        else:
            # Attempt CSV fallback
            logger.warning("[DATASET_SVC] Unknown extension '%s' — trying CSV", ext)
            buf.seek(0)
            return pd.read_csv(buf)

    def _get_from_cache(self, dataset_id: UUID):
        """Try to get DataFrame from DatasetRegistry L1 cache."""
        try:
            from datasets.registry import dataset_registry
            entry = dataset_registry.get(str(dataset_id))
            if entry and hasattr(entry, "dataframe") and entry.dataframe is not None:
                return entry.dataframe
        except Exception:
            pass
        return None

    def _register_in_cache(self, dataset: Dataset, df) -> None:
        """Re-register a restored Dataset + DataFrame into the L1 cache."""
        try:
            from datasets.registry import dataset_registry, DatasetEntry
            from datetime import datetime, timezone
            # Deserialize DKO from PostgreSQL JSONB if present
            dko = None
            if hasattr(dataset, 'dko_json') and dataset.dko_json:
                try:
                    from db.dko_serializer import dko_serializer
                    dko = dko_serializer.deserialize(dataset.dko_json)
                except Exception as dko_err:
                    logger.debug("[DATASET_SVC] DKO deserialization failed: %s", dko_err)

            schema_profile = []
            column_profiles = {}
            if dko and hasattr(dko, 'columns') and dko.columns:
                schema_profile = [c.to_dict() for c in dko.columns.values()]
                for k, v in dko.columns.items():
                    if hasattr(v, 'numeric_stats') and v.numeric_stats:
                        column_profiles[k] = v.numeric_stats.to_dict()
                    elif hasattr(v, 'categorical_stats') and v.categorical_stats:
                        column_profiles[k] = v.categorical_stats.to_dict()
                    elif hasattr(v, 'datetime_stats') and v.datetime_stats:
                        column_profiles[k] = v.datetime_stats.to_dict()
                    else:
                        column_profiles[k] = {}
            elif df is not None:
                schema_profile = [{'name': c, 'dtype': str(df[c].dtype)} for c in df.columns]

            profile = dataset.profile_json or (dko.to_dict() if dko and hasattr(dko, 'to_dict') else {})

            entry = DatasetEntry(
                dataset_id=str(dataset.id),
                filename=dataset.original_filename,
                file_path="",  # file is in DB BYTEA, not local path
                upload_timestamp=dataset.created_at.isoformat() if dataset.created_at else datetime.now(timezone.utc).isoformat(),
                row_count=len(df) if df is not None else (dataset.row_count or 0),
                column_count=len(df.columns) if df is not None else (dataset.column_count or 0),
                file_fingerprint=dataset.file_hash or "",
                schema_fingerprint=dataset.file_hash or "",
                dataframe=df,
                profile=profile,
                schema_profile=schema_profile,
                column_profiles=column_profiles,
                ontology=None,
                dko=dko,
            )
            dataset_registry.register(entry)
            logger.debug("[DATASET_SVC] Re-registered dataset %s in L1 cache", dataset.id)
        except Exception as e:
            logger.warning("[DATASET_SVC] Could not register in L1 cache: %s", e)



dataset_service = DatasetService()
