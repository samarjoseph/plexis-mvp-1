"""
Optional Live Integration Test for Backblaze B2 S3-Compatible Cloud Storage.

This test is SEPARATE from the mock unit test suite.
It runs only when live Backblaze B2 credentials are provided in the environment:
    - B2_APPLICATION_KEY_ID
    - B2_APPLICATION_KEY
    - B2_BUCKET_NAME
    - B2_S3_ENDPOINT_URL

If credentials are absent, the test cleanly skips with an explicit message.
When run, it uses a dedicated prefix '_plexis_live_test/' and safely cleans up
the single test object without touching any other objects in the bucket.
"""
import os
import uuid
import pytest
from dotenv import load_dotenv

# Ensure environment is loaded from .env if present
load_dotenv(".env", override=False)

from storage.contracts import StorageAccountConfig
from storage.b2_client import B2Client


def _get_live_config():
    key_id = os.getenv("B2_APPLICATION_KEY_ID", "").strip()
    key_secret = os.getenv("B2_APPLICATION_KEY", "").strip()
    bucket = os.getenv("B2_BUCKET_NAME", "").strip()
    endpoint = os.getenv("B2_S3_ENDPOINT_URL", "").strip()
    region = os.getenv("B2_REGION", "").strip()

    if not (key_id and key_secret and bucket):
        return None

    return StorageAccountConfig(
        account_id=key_id,
        access_key_id=key_id,
        secret_access_key=key_secret,
        bucket_name=bucket,
        endpoint_url=endpoint,
        region_name=region or None,
        account_alias="live_test",
        max_storage_bytes=10 * 1024 * 1024 * 1024,
    )


def test_live_backblaze_b2_lifecycle():
    """Verify live Backblaze B2 upload, download, metadata, and cleanup against live bucket."""
    cfg = _get_live_config()
    if cfg is None:
        pytest.skip(
            "Live Backblaze B2 credentials (B2_APPLICATION_KEY_ID, B2_APPLICATION_KEY, B2_BUCKET_NAME) "
            "are not configured. Skipping live integration test."
        )

    # Initialize live client
    client = B2Client(cfg)
    assert client.is_cloud is True, "Client did not initialize a real S3 boto3 client"

    # Dedicated prefix for test isolation
    test_id = uuid.uuid4().hex
    test_key = f"_plexis_live_test/{test_id}/probe.txt"
    test_payload = f"Plexis Live B2 Integration Test - Session {test_id}\n".encode("utf-8")

    try:
        # 1. Upload small test object
        put_res = client.put_object(
            key=test_key,
            data=test_payload,
            content_type="text/plain",
            metadata={"test-session": test_id},
            check_quota=False,
        )
        assert put_res.success is True
        assert put_res.size_bytes == len(test_payload)
        assert put_res.key == test_key

        # 2. Check existence
        assert client.object_exists(test_key) is True

        # 3. Download object and verify contents
        downloaded = client.get_object(test_key)
        assert downloaded == test_payload, "Downloaded content did not match uploaded payload"

        # 4. Verify object metadata
        meta = client.head_object(test_key)
        assert meta is not None
        assert meta.size_bytes == len(test_payload)
        assert meta.content_type == "text/plain"

        # 5. Verify presigned URL generation
        presigned = client.generate_presigned_url(test_key, client_method="get_object", expires_in=300)
        assert presigned.startswith("http")
        assert "s3." in presigned or "backblazeb2.com" in presigned

    finally:
        # 6. Delete test object and verify cleanup succeeded
        deleted = client.delete_object(test_key)
        assert deleted is True
        assert client.object_exists(test_key) is False, "Test object was not deleted after test completion"
