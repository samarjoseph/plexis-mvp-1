"""
Plexis Object Storage Package.
"""
from storage.contracts import (
    StorageAccountConfig,
    StorageObjectMetadata,
    StorageResult,
    StorageUsage,
    StorageError,
    StorageObjectNotFoundError,
    StorageAccountNotConfiguredError,
    StoragePermissionError,
    StorageTimeoutError,
    StorageQuotaExceededError,
    StorageConfigurationError,
)
from storage.b2_client import B2Client
from storage.manager import storage_manager, StorageManager

__all__ = [
    "storage_manager",
    "StorageManager",
    "B2Client",
    "StorageAccountConfig",
    "StorageObjectMetadata",
    "StorageResult",
    "StorageUsage",
    "StorageError",
    "StorageObjectNotFoundError",
    "StorageAccountNotConfiguredError",
    "StoragePermissionError",
    "StorageTimeoutError",
    "StorageQuotaExceededError",
    "StorageConfigurationError",
]
