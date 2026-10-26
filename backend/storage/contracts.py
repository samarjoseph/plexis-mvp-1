"""
Plexis Storage Abstraction — Contracts, Exceptions, and Data Models.

Supports Backblaze B2 Cloud Storage (S3-compatible) with multi-account routing,
quotas, metadata, and local filesystem emulation for testing and offline development.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, Any, BinaryIO


# ─── Exceptions ──────────────────────────────────────────────────────────────

class StorageError(Exception):
    """Base exception for all storage operations."""
    pass


class StorageObjectNotFoundError(StorageError):
    """Raised when the requested object key is not found in the storage bucket."""
    pass


class StorageAccountNotConfiguredError(StorageError):
    """Raised when attempting an operation on an unconfigured storage account."""
    pass


class StorageConfigurationError(StorageError):
    """Raised when storage configuration is invalid or missing required credentials."""
    pass


class StoragePermissionError(StorageError):
    """Raised on 401/403, key unauthorized, or signature mismatch from storage provider."""
    pass


class StorageTimeoutError(StorageError):
    """Raised when a storage request times out."""
    pass


class StorageQuotaExceededError(StorageError):
    """Raised when an upload would exceed the configured maximum storage quota."""
    def __init__(self, current_bytes: int, incoming_bytes: int, max_bytes: int, message: Optional[str] = None):
        self.current_bytes = current_bytes
        self.incoming_bytes = incoming_bytes
        self.max_bytes = max_bytes
        super().__init__(
            message or (
                f"Storage quota exceeded: Current usage {current_bytes / (1024*1024):.1f} MB + "
                f"incoming {incoming_bytes / (1024*1024):.1f} MB exceeds limit of "
                f"{max_bytes / (1024*1024):.1f} MB."
            )
        )


# ─── Configuration & Metadata Data Models ────────────────────────────────────

@dataclass
class StorageAccountConfig:
    """
    Configuration for an S3-compatible Backblaze B2 account.
    """
    account_id: str                          # applicationKeyId (e.g. 005...)
    access_key_id: str                       # applicationKeyId
    secret_access_key: str                   # applicationKey
    bucket_name: str
    endpoint_url: Optional[str] = None       # e.g. https://s3.us-east-005.backblazeb2.com
    region_name: Optional[str] = None        # e.g. us-east-005
    account_alias: str = "primary"
    provider: str = "b2"
    max_storage_bytes: int = 10 * 1024 * 1024 * 1024  # 10 GB default (B2 free tier)

    @property
    def is_configured(self) -> bool:
        """Return True if credentials and bucket are provided."""
        return bool(self.access_key_id and self.secret_access_key and self.bucket_name)

    def get_region_name(self) -> str:
        """Extract or resolve the B2 region."""
        if self.region_name:
            return self.region_name.strip()
        if self.endpoint_url:
            match = re.search(r"s3\.([a-zA-Z0-9-]+)\.backblazeb2\.com", self.endpoint_url)
            if match:
                return match.group(1)
        return "us-east-005"

    def get_endpoint_url(self) -> str:
        """Resolve endpoint URL: custom override or standard Backblaze B2 regional endpoint."""
        if self.endpoint_url:
            return self.endpoint_url.rstrip("/")
        region = self.get_region_name()
        return f"https://s3.{region}.backblazeb2.com"


@dataclass
class StorageObjectMetadata:
    """Metadata describing a stored object."""
    key: str
    account_alias: str
    size_bytes: int
    content_type: str = "application/octet-stream"
    etag: Optional[str] = None
    last_modified: Optional[datetime] = None
    user_metadata: Dict[str, str] = field(default_factory=dict)


@dataclass
class StorageResult:
    """Result of a storage write operation."""
    key: str
    account_alias: str
    size_bytes: int
    etag: Optional[str] = None
    version_id: Optional[str] = None
    success: bool = True


@dataclass
class StorageUsage:
    """Summary of tracked storage usage against quota."""
    total_bytes: int
    object_count: int
    quota_bytes: int

    @property
    def quota_remaining_bytes(self) -> int:
        return max(0, self.quota_bytes - self.total_bytes)

    @property
    def usage_percent(self) -> float:
        if self.quota_bytes <= 0:
            return 0.0
        return min(100.0, (self.total_bytes / self.quota_bytes) * 100.0)

    @property
    def total_mb(self) -> float:
        return self.total_bytes / (1024 * 1024)

    @property
    def quota_mb(self) -> float:
        return self.quota_bytes / (1024 * 1024)
