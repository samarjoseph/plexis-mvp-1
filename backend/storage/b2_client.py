"""
Plexis Backblaze B2 S3-Compatible Storage Client.

Provides uniform object storage operations via boto3 connecting to Backblaze B2 S3 API.
Falls back to local file emulation if credentials are not configured or STORAGE_PROVIDER=local,
enabling development and testing without live cloud infrastructure.
"""
from __future__ import annotations

import io
import os
import shutil
import mimetypes
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, BinaryIO, Generator

from storage.contracts import (
    StorageAccountConfig,
    StorageObjectMetadata,
    StorageResult,
    StorageUsage,
    StorageError,
    StorageObjectNotFoundError,
    StoragePermissionError,
    StorageTimeoutError,
    StorageQuotaExceededError,
    StorageConfigurationError,
)

logger = logging.getLogger(__name__)


class B2Client:
    """
    Client for interacting with Backblaze B2 via its S3-compatible API.
    One instance per configured account.
    """

    def __init__(self, config: StorageAccountConfig):
        self.config = config
        self.account_alias = config.account_alias
        self._s3_client = None
        self._local_fallback_dir = None
        self._cached_usage_bytes = 0
        self._last_usage_check_time = 0.0

        if self.config.is_configured:
            self._init_s3_client()
        elif getattr(self.config, "provider", "b2") == "b2" and not (os.getenv("TESTING") or os.getenv("PYTEST_CURRENT_TEST")):
            raise StorageConfigurationError(
                f"Backblaze B2 is configured as storage provider for account '{self.account_alias}', "
                f"but required configuration (B2_APPLICATION_KEY_ID, B2_APPLICATION_KEY, or B2_BUCKET_NAME) is missing."
            )
        else:
            self._init_local_fallback()

    def _init_s3_client(self) -> None:
        """Initialize boto3 S3 client with Backblaze B2 settings."""
        try:
            import boto3
            import botocore.client
            from botocore.config import Config as BotoConfig

            endpoint_url = self.config.get_endpoint_url()
            region_name = self.config.get_region_name()

            boto_config = BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=10,
                read_timeout=60,
            )

            self._s3_client = boto3.client(
                "s3",
                endpoint_url=endpoint_url,
                aws_access_key_id=self.config.access_key_id,
                aws_secret_access_key=self.config.secret_access_key,
                region_name=region_name,
                config=boto_config,
            )
            logger.info(
                "[B2Client] Initialized Backblaze B2 S3 client for account '%s' (bucket: %s, endpoint: %s, region: %s)",
                self.account_alias,
                self.config.bucket_name,
                endpoint_url,
                region_name,
            )
        except Exception as exc:
            if os.getenv("TESTING") or os.getenv("PYTEST_CURRENT_TEST"):
                logger.warning(
                    "[B2Client] Failed to initialize Backblaze B2 client for '%s' (%s). Falling back to local storage in test mode.",
                    self.account_alias,
                    exc,
                )
                self._init_local_fallback()
            else:
                raise StorageConfigurationError(
                    f"Failed to initialize Backblaze B2 client for account '{self.account_alias}': {str(exc)}"
                ) from exc

    def _init_local_fallback(self) -> None:
        """Initialize local filesystem emulation for development/testing."""
        from config import Config
        base_dir = os.path.join(Config.UPLOAD_FOLDER, "b2_emulated", self.account_alias)
        os.makedirs(base_dir, exist_ok=True)
        self._local_fallback_dir = base_dir
        self._s3_client = None
        logger.info(
            "[B2Client] Running in local filesystem fallback mode for account '%s' at '%s'",
            self.account_alias,
            self._local_fallback_dir,
        )

    @property
    def is_cloud(self) -> bool:
        """Return True if connected to real Backblaze B2 cloud storage."""
        return self._s3_client is not None

    def _handle_client_error(self, exc: Exception, key: str) -> None:
        """Map botocore ClientError to typed StorageError."""
        err_code = ""
        msg = str(exc)
        if hasattr(exc, "response") and isinstance(exc.response, dict):
            err_code = exc.response.get("Error", {}).get("Code", "")
            msg = exc.response.get("Error", {}).get("Message", msg)

        if err_code in ("NoSuchKey", "404", "NotFound"):
            raise StorageObjectNotFoundError(f"Object '{key}' not found in Backblaze B2 bucket '{self.config.bucket_name}'") from exc
        if err_code in ("AccessDenied", "403", "InvalidAccessKeyId", "SignatureDoesNotMatch", "Unauthorized"):
            raise StoragePermissionError(f"Permission denied for object '{key}' on Backblaze B2 account '{self.account_alias}': {msg}") from exc
        if "timed out" in msg.lower() or "timeout" in msg.lower():
            raise StorageTimeoutError(f"Timeout accessing Backblaze B2 object '{key}': {msg}") from exc

        raise StorageError(f"Backblaze B2 operation failed on account '{self.account_alias}' for key '{key}': {msg}") from exc

    # -------------------------------------------------------------------------
    # Quota and Usage Safeguards
    # -------------------------------------------------------------------------

    def get_storage_usage(self, prefix: Optional[str] = None) -> StorageUsage:
        """Calculate total stored bytes and object count in the bucket or prefix."""
        total_bytes = 0
        object_count = 0

        if self._s3_client:
            try:
                paginator = self._s3_client.get_paginator("list_objects_v2")
                params: Dict[str, Any] = {"Bucket": self.config.bucket_name}
                if prefix:
                    params["Prefix"] = prefix.lstrip("/")
                for page in paginator.paginate(**params):
                    for item in page.get("Contents", []):
                        total_bytes += item.get("Size", 0)
                        object_count += 1
            except Exception as e:
                logger.warning("[B2Client] Failed calculating cloud usage: %s", e)
        elif self._local_fallback_dir and os.path.exists(self._local_fallback_dir):
            for root, _, files in os.walk(self._local_fallback_dir):
                for f in files:
                    try:
                        fp = os.path.join(root, f)
                        total_bytes += os.path.getsize(fp)
                        object_count += 1
                    except OSError:
                        pass

        return StorageUsage(
            total_bytes=total_bytes,
            object_count=object_count,
            quota_bytes=self.config.max_storage_bytes,
        )

    def check_quota(self, incoming_bytes: int) -> None:
        """Validate whether incoming upload fits within the configured maximum quota."""
        if self.config.max_storage_bytes <= 0:
            return  # Unlimited

        usage = self.get_storage_usage()
        if (usage.total_bytes + incoming_bytes) > self.config.max_storage_bytes:
            raise StorageQuotaExceededError(
                current_bytes=usage.total_bytes,
                incoming_bytes=incoming_bytes,
                max_bytes=self.config.max_storage_bytes,
            )

    # -------------------------------------------------------------------------
    # Core Operations
    # -------------------------------------------------------------------------

    def put_object(
        self,
        key: str,
        data: bytes | BinaryIO,
        content_type: str = "application/octet-stream",
        metadata: Optional[Dict[str, str]] = None,
        check_quota: bool = True,
    ) -> StorageResult:
        """Upload raw bytes or file-like object to Backblaze B2."""
        safe_key = key.lstrip("/")
        meta_dict = metadata or {}

        # Calculate payload size
        size = 0
        if isinstance(data, (bytes, bytearray)):
            size = len(data)
        elif hasattr(data, "seek") and hasattr(data, "tell"):
            data.seek(0, os.SEEK_END)
            size = data.tell()
            data.seek(0)

        # Enforce quota safeguard before writing
        if check_quota and size > 0:
            self.check_quota(size)

        if self._s3_client:
            try:
                body = data
                resp = self._s3_client.put_object(
                    Bucket=self.config.bucket_name,
                    Key=safe_key,
                    Body=body,
                    ContentType=content_type,
                    Metadata=meta_dict,
                )
                return StorageResult(
                    key=safe_key,
                    account_alias=self.account_alias,
                    size_bytes=size,
                    etag=resp.get("ETag", "").strip('"'),
                    version_id=resp.get("VersionId"),
                    success=True,
                )
            except Exception as e:
                self._handle_client_error(e, safe_key)

        # Local Fallback
        local_path = os.path.join(self._local_fallback_dir, safe_key)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        if isinstance(data, (bytes, bytearray)):
            with open(local_path, "wb") as f:
                f.write(data)
            size = len(data)
        else:
            with open(local_path, "wb") as f:
                shutil.copyfileobj(data, f)
            size = os.path.getsize(local_path)

        return StorageResult(
            key=safe_key,
            account_alias=self.account_alias,
            size_bytes=size,
            etag="local-mock-etag",
            success=True,
        )

    def get_object(self, key: str) -> bytes:
        """Download complete object content as bytes."""
        safe_key = key.lstrip("/")
        if self._s3_client:
            try:
                resp = self._s3_client.get_object(Bucket=self.config.bucket_name, Key=safe_key)
                return resp["Body"].read()
            except Exception as e:
                self._handle_client_error(e, safe_key)

        local_path = os.path.join(self._local_fallback_dir, safe_key)
        if not os.path.exists(local_path):
            raise StorageObjectNotFoundError(f"Object '{safe_key}' not found in local store.")
        with open(local_path, "rb") as f:
            return f.read()

    def get_object_stream(self, key: str) -> BinaryIO:
        """Get a readable binary stream for the object."""
        safe_key = key.lstrip("/")
        if self._s3_client:
            try:
                resp = self._s3_client.get_object(Bucket=self.config.bucket_name, Key=safe_key)
                return resp["Body"]
            except Exception as e:
                self._handle_client_error(e, safe_key)

        local_path = os.path.join(self._local_fallback_dir, safe_key)
        if not os.path.exists(local_path):
            raise StorageObjectNotFoundError(f"Object '{safe_key}' not found in local store.")
        return open(local_path, "rb")

    def head_object(self, key: str) -> Optional[StorageObjectMetadata]:
        """Fetch metadata for an object without downloading body."""
        safe_key = key.lstrip("/")
        if self._s3_client:
            try:
                resp = self._s3_client.head_object(Bucket=self.config.bucket_name, Key=safe_key)
                return StorageObjectMetadata(
                    key=safe_key,
                    account_alias=self.account_alias,
                    size_bytes=resp.get("ContentLength", 0),
                    content_type=resp.get("ContentType", "application/octet-stream"),
                    etag=resp.get("ETag", "").strip('"'),
                    last_modified=resp.get("LastModified"),
                    user_metadata=resp.get("Metadata", {}),
                )
            except Exception as e:
                err_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
                if err_code in ("404", "NoSuchKey", "NotFound"):
                    return None
                self._handle_client_error(e, safe_key)

        local_path = os.path.join(self._local_fallback_dir, safe_key)
        if not os.path.exists(local_path):
            return None
        stat = os.stat(local_path)
        guessed_type, _ = mimetypes.guess_type(local_path)
        return StorageObjectMetadata(
            key=safe_key,
            account_alias=self.account_alias,
            size_bytes=stat.st_size,
            content_type=guessed_type or "application/octet-stream",
            last_modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        )

    def object_exists(self, key: str) -> bool:
        """Check if an object exists in this account's bucket."""
        return self.head_object(key) is not None

    def delete_object(self, key: str) -> bool:
        """Delete an object from this account's bucket."""
        safe_key = key.lstrip("/")
        if self._s3_client:
            try:
                self._s3_client.delete_object(Bucket=self.config.bucket_name, Key=safe_key)
                return True
            except Exception as e:
                self._handle_client_error(e, safe_key)

        local_path = os.path.join(self._local_fallback_dir, safe_key)
        if os.path.exists(local_path):
            try:
                os.remove(local_path)
                return True
            except OSError as err:
                logger.error("[B2Client] Error deleting local file %s: %s", local_path, err)
                return False
        return True

    def download_file(self, key: str, target_local_path: str) -> None:
        """Download object directly to a local file path."""
        safe_key = key.lstrip("/")
        os.makedirs(os.path.dirname(os.path.abspath(target_local_path)), exist_ok=True)
        if self._s3_client:
            try:
                self._s3_client.download_file(self.config.bucket_name, safe_key, target_local_path)
                return
            except Exception as e:
                self._handle_client_error(e, safe_key)

        local_path = os.path.join(self._local_fallback_dir, safe_key)
        if not os.path.exists(local_path):
            raise StorageObjectNotFoundError(f"Object '{safe_key}' not found in local store.")
        shutil.copyfile(local_path, target_local_path)

    def upload_file(
        self,
        key: str,
        source_local_path: str,
        content_type: str = "application/octet-stream",
        metadata: Optional[Dict[str, str]] = None,
        check_quota: bool = True,
    ) -> StorageResult:
        """Upload a local file path directly to Backblaze B2."""
        safe_key = key.lstrip("/")
        if not os.path.exists(source_local_path):
            raise StorageError(f"Local source file '{source_local_path}' does not exist.")

        size = os.path.getsize(source_local_path)
        if check_quota and size > 0:
            self.check_quota(size)

        if self._s3_client:
            try:
                extra_args = {"ContentType": content_type}
                if metadata:
                    extra_args["Metadata"] = metadata
                self._s3_client.upload_file(
                    Filename=source_local_path,
                    Bucket=self.config.bucket_name,
                    Key=safe_key,
                    ExtraArgs=extra_args,
                )
                return StorageResult(
                    key=safe_key,
                    account_alias=self.account_alias,
                    size_bytes=size,
                    success=True,
                )
            except Exception as e:
                self._handle_client_error(e, safe_key)

        # Local Fallback
        target_path = os.path.join(self._local_fallback_dir, safe_key)
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        shutil.copyfile(source_local_path, target_path)
        return StorageResult(
            key=safe_key,
            account_alias=self.account_alias,
            size_bytes=size,
            etag="local-mock-etag",
            success=True,
        )

    def generate_presigned_url(
        self,
        key: str,
        client_method: str = "get_object",
        expires_in: int = 3600,
    ) -> str:
        """Generate a short-lived presigned URL for Backblaze B2 object access."""
        safe_key = key.lstrip("/")
        if self._s3_client:
            try:
                return self._s3_client.generate_presigned_url(
                    ClientMethod=client_method,
                    Params={"Bucket": self.config.bucket_name, "Key": safe_key},
                    ExpiresIn=expires_in,
                )
            except Exception as e:
                self._handle_client_error(e, safe_key)

        # Local simulation presigned URL
        return f"http://localhost:5000/api/storage/local/{self.account_alias}/{safe_key}?expires={expires_in}"
