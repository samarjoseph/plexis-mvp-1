"""
Plexis Storage Manager — Backblaze B2 Account Router, Quota Enforcer, and Key Builder.

Enforces:
- Explicit routing by storage account identifier ('primary', 'b2', 'secondary', etc.).
- Safe server-controlled object key generation without user-controlled paths.
- Strict isolation between Account A and Account B (no cross-account guesswork).
- Application-level storage quotas to protect zero-budget MVP constraints.
- Pre-upload quota validation.
"""
from __future__ import annotations

import os
import re
import uuid
import logging
from typing import Optional, Dict, Any, BinaryIO

from config import Config
from storage.contracts import (
    StorageAccountConfig,
    StorageObjectMetadata,
    StorageResult,
    StorageUsage,
    StorageError,
    StorageObjectNotFoundError,
    StorageAccountNotConfiguredError,
    StorageQuotaExceededError,
)
from storage.b2_client import B2Client

logger = logging.getLogger(__name__)


class StorageManager:
    """
    Central storage service managing Backblaze B2 storage accounts, quotas, and key conventions.
    """

    PRIMARY_ALIAS = "primary"
    SECONDARY_ALIAS = "secondary"

    def __init__(self):
        self._clients: Dict[str, B2Client] = {}
        self._init_accounts()

    def _init_accounts(self) -> None:
        """Initialize primary and secondary account clients from Config."""
        max_bytes = getattr(Config, "B2_MAX_STORAGE_MB", 10000) * 1024 * 1024

        # Backblaze B2 Account A (Primary)
        primary_cfg = StorageAccountConfig(
            account_id=Config.B2_APPLICATION_KEY_ID,
            access_key_id=Config.B2_APPLICATION_KEY_ID,
            secret_access_key=Config.B2_APPLICATION_KEY,
            bucket_name=Config.B2_BUCKET_NAME,
            endpoint_url=Config.B2_S3_ENDPOINT_URL or None,
            region_name=Config.B2_REGION or None,
            account_alias=self.PRIMARY_ALIAS,
            provider=getattr(Config, "STORAGE_PROVIDER", "b2"),
            max_storage_bytes=max_bytes,
        )
        primary_client = B2Client(primary_cfg)
        self._clients[self.PRIMARY_ALIAS] = primary_client
        self._clients["b2"] = primary_client
        self._clients["a"] = primary_client
        self._clients["account_a"] = primary_client

        # Backblaze B2 Account B (Secondary — Optional)
        if Config.B2_B_APPLICATION_KEY_ID or Config.B2_B_BUCKET_NAME or Config.B2_B_APPLICATION_KEY:
            secondary_cfg = StorageAccountConfig(
                account_id=Config.B2_B_APPLICATION_KEY_ID,
                access_key_id=Config.B2_B_APPLICATION_KEY_ID,
                secret_access_key=Config.B2_B_APPLICATION_KEY,
                bucket_name=Config.B2_B_BUCKET_NAME,
                endpoint_url=Config.B2_B_S3_ENDPOINT_URL or None,
                region_name=Config.B2_B_REGION or None,
                account_alias=self.SECONDARY_ALIAS,
                provider=getattr(Config, "STORAGE_PROVIDER", "b2"),
                max_storage_bytes=max_bytes,
            )
            secondary_client = B2Client(secondary_cfg)
            self._clients[self.SECONDARY_ALIAS] = secondary_client
            self._clients["b2_b"] = secondary_client
            self._clients["b"] = secondary_client
            self._clients["account_b"] = secondary_client
            logger.info("[StorageManager] Initialized dual Backblaze B2 storage accounts (primary and secondary).")
        else:
            logger.info("[StorageManager] Initialized single Backblaze B2 storage account (primary).")

    def get_client(self, account_identifier: Optional[str] = None) -> B2Client:
        """
        Resolve client for the given account identifier.
        Strict: Does NOT silently fallback to another account if the requested account is missing.
        """
        acc_key = (account_identifier or self.PRIMARY_ALIAS).lower().strip()
        client = self._clients.get(acc_key)
        if not client:
            raise StorageAccountNotConfiguredError(
                f"Storage account '{account_identifier}' is not configured on this server."
            )
        return client

    def is_account_configured(self, account_identifier: str) -> bool:
        """Check if an account identifier is registered and has cloud/local client."""
        return (account_identifier or "").lower().strip() in self._clients

    # -------------------------------------------------------------------------
    # Server-Controlled Key Builders
    # -------------------------------------------------------------------------

    @staticmethod
    def sanitize_filename(filename: str) -> str:
        """Sanitize a filename so it cannot traverse directories or break object keys."""
        base = os.path.basename(filename) if hasattr(os, "path") else filename
        clean = re.sub(r"[^a-zA-Z0-9._-]", "_", base)
        return clean[:100] or "dataset.csv"

    def build_original_key(self, user_id: str | uuid.UUID, dataset_id: str | uuid.UUID, filename: str) -> str:
        """Generate safe key for an original uploaded dataset file."""
        safe_fn = self.sanitize_filename(filename)
        return f"users/{user_id}/datasets/{dataset_id}/original/{safe_fn}"

    def build_snapshot_key(self, user_id: str | uuid.UUID, dataset_id: str | uuid.UUID, revision: int, ext: str = "plexis.json") -> str:
        """Generate safe key for a dataset revision snapshot."""
        return f"users/{user_id}/datasets/{dataset_id}/snapshots/rev_{revision:06d}.{ext}"

    def build_history_segment_key(self, user_id: str | uuid.UUID, dataset_id: str | uuid.UUID, segment_id: str) -> str:
        """Generate safe key for a compacted action history segment."""
        clean_seg = re.sub(r"[^a-zA-Z0-9._-]", "_", str(segment_id))
        return f"users/{user_id}/datasets/{dataset_id}/history/segment_{clean_seg}.jsonl"

    # -------------------------------------------------------------------------
    # Routed Storage Operations
    # -------------------------------------------------------------------------

    def put_object(
        self,
        key: str,
        data: bytes | BinaryIO,
        account_identifier: Optional[str] = None,
        content_type: str = "application/octet-stream",
        metadata: Optional[Dict[str, str]] = None,
        check_quota: bool = True,
    ) -> StorageResult:
        """Store an object in the specified storage account."""
        client = self.get_client(account_identifier)
        return client.put_object(
            key=key,
            data=data,
            content_type=content_type,
            metadata=metadata,
            check_quota=check_quota,
        )

    def get_object(self, key: str, account_identifier: Optional[str] = None) -> bytes:
        """Download an object from the specified storage account."""
        client = self.get_client(account_identifier)
        return client.get_object(key=key)

    def get_object_stream(self, key: str, account_identifier: Optional[str] = None) -> BinaryIO:
        """Stream an object from the specified storage account."""
        client = self.get_client(account_identifier)
        return client.get_object_stream(key=key)

    def head_object(self, key: str, account_identifier: Optional[str] = None) -> Optional[StorageObjectMetadata]:
        """Check metadata for an object in the specified storage account."""
        client = self.get_client(account_identifier)
        return client.head_object(key=key)

    def object_exists(self, key: str, account_identifier: Optional[str] = None) -> bool:
        """Check object existence in the specified storage account."""
        client = self.get_client(account_identifier)
        return client.object_exists(key=key)

    def delete_object(self, key: str, account_identifier: Optional[str] = None) -> bool:
        """Delete an object from the specified storage account."""
        client = self.get_client(account_identifier)
        return client.delete_object(key=key)

    def upload_file(
        self,
        key: str,
        source_local_path: str,
        account_identifier: Optional[str] = None,
        content_type: str = "application/octet-stream",
        metadata: Optional[Dict[str, str]] = None,
        check_quota: bool = True,
    ) -> StorageResult:
        """Upload a file from local filesystem to the specified storage account."""
        client = self.get_client(account_identifier)
        return client.upload_file(
            key=key,
            source_local_path=source_local_path,
            content_type=content_type,
            metadata=metadata,
            check_quota=check_quota,
        )

    def download_file(self, key: str, target_local_path: str, account_identifier: Optional[str] = None) -> None:
        """Download an object to a local filesystem destination."""
        client = self.get_client(account_identifier)
        client.download_file(key=key, target_local_path=target_local_path)

    def generate_presigned_url(
        self,
        key: str,
        account_identifier: Optional[str] = None,
        client_method: str = "get_object",
        expires_in: int = 3600,
    ) -> str:
        """Generate a short-lived presigned URL for an object."""
        client = self.get_client(account_identifier)
        return client.generate_presigned_url(key=key, client_method=client_method, expires_in=expires_in)

    def get_storage_usage(self, account_identifier: Optional[str] = None, prefix: Optional[str] = None) -> StorageUsage:
        """Get tracked storage usage and remaining quota for an account."""
        client = self.get_client(account_identifier)
        return client.get_storage_usage(prefix=prefix)

    def check_quota(self, incoming_bytes: int, account_identifier: Optional[str] = None) -> None:
        """Check if adding incoming_bytes would exceed quota."""
        client = self.get_client(account_identifier)
        client.check_quota(incoming_bytes)


# Global singleton instance
storage_manager = StorageManager()
