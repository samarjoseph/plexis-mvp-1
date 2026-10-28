"""Dataset registry management."""
import logging
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
import pandas as pd

logger = logging.getLogger(__name__)

@dataclass
class DatasetEntry:
    dataset_id: str
    filename: str
    file_path: str
    upload_timestamp: str
    row_count: int
    column_count: int
    file_fingerprint: str
    schema_fingerprint: str
    dataframe: pd.DataFrame
    profile: Dict[str, Any]
    schema_profile: List[Dict[str, Any]]
    column_profiles: Dict[str, Dict[str, Any]]
    ontology: Optional[Any] = None
    dko: Optional[Any] = None  # DatasetKnowledgeObject from Dataset Intelligence Engine

class DatasetRegistry:
    def __init__(self):
        self._datasets: Dict[str, DatasetEntry] = {}
        self._active_dataset_id: Optional[str] = None
        self._restoring: set = set()
        
    def register(self, entry: DatasetEntry) -> None:
        self._datasets[entry.dataset_id] = entry
        self._active_dataset_id = entry.dataset_id
        logger.info(f"Registered dataset {entry.dataset_id} ({entry.filename})")

    def rekey(self, old_dataset_id: str, new_dataset_id: str) -> Optional[DatasetEntry]:
        """Replace a temporary upload ID with the durable PostgreSQL dataset ID."""
        old_dataset_id = str(old_dataset_id)
        new_dataset_id = str(new_dataset_id)
        if old_dataset_id == new_dataset_id:
            return self._datasets.get(old_dataset_id)

        entry = self._datasets.pop(old_dataset_id, None)
        if entry is None:
            return None

        entry.dataset_id = new_dataset_id
        self._datasets[new_dataset_id] = entry
        if self._active_dataset_id == old_dataset_id:
            self._active_dataset_id = new_dataset_id
        logger.info("Re-keyed dataset %s -> %s", old_dataset_id, new_dataset_id)
        return entry
     
    def get(self, dataset_id: str) -> Optional[DatasetEntry]:
        entry = self._datasets.get(dataset_id)
        if entry is not None:
            return entry

        if dataset_id in self._restoring:
            return None

        self._restoring.add(dataset_id)
        # L2 Auto-restore on cache miss
        try:
            from flask import has_request_context, request
            import uuid
            
            user_id = None
            if has_request_context():
                try:
                    from db.identity import identity_resolver
                    current_user = identity_resolver.resolve(request)
                    if current_user:
                        user_id = current_user.user_id
                except Exception:
                    pass
            
            from db.session import get_db
            from db.services.dataset_service import dataset_service
            
            try:
                ds_uuid = uuid.UUID(str(dataset_id))
            except (ValueError, TypeError):
                return None

            db = get_db()
            if db:
                dataset, df = dataset_service.restore_dataset_df(db, ds_uuid, user_id)
                if dataset is not None and df is not None:
                    return self._datasets.get(dataset_id)
        except Exception as e:
            logger.warning('Auto-restore failed for dataset %s: %s', dataset_id, e)
        finally:
            self._restoring.discard(dataset_id)
            
        return None

    def get_by_id(self, dataset_id: str) -> Optional[DatasetEntry]:
        return self.get(dataset_id)
        
    def get_by_filename(self, filename: str) -> Optional[DatasetEntry]:
        matches = [d for d in self._datasets.values() if d.filename == filename]
        if not matches:
            return None
        matches.sort(key=lambda x: x.upload_timestamp, reverse=True)
        return matches[0]
        
    def get_active_dataset(self) -> Optional[DatasetEntry]:
        if self._active_dataset_id:
            return self._datasets.get(self._active_dataset_id)
        return None
        
    def all_datasets(self) -> List[DatasetEntry]:
        return list(self._datasets.values())

dataset_registry = DatasetRegistry()
