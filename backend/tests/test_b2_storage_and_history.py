"""
Comprehensive test suite for Backblaze B2 Persistent Storage and Spreadsheet Action History.

Tests:
1. StorageManager, B2Client, and Account Routing (Primary vs Secondary, Strict Isolation).
2. Endpoint and Region Resolution for Backblaze B2 S3 API.
3. Key generation and filename sanitization (preventing directory traversal).
4. Object Storage CRUD (Put, Get, Stream, Head, Delete, Presigned URLs).
5. Storage Quotas & Zero-Budget MVP Safeguards (Usage calculation, Pre-upload Quota Enforcement).
6. Single Cell Edits, Batch Edits, and Optimistic Concurrency Control (OCC 409).
7. Undo, Redo, and Branch Invalidation.
8. Periodic Snapshot Compaction to Backblaze B2.
9. Cold-Start Rehydration (Restoring dataset from B2 snapshots + replay).
10. Legacy Dataset Fallback (PostgreSQL BYTEA compatibility).
11. Flask API Endpoints (/api/spreadsheet/edit, /undo, /redo, /history, /api/transform/redo).
12. R2-to-B2 Safe Migration Utility (dry run, copy verification, idempotent execution).
"""
import io
import os
import uuid
import pytest
import pandas as pd
from unittest.mock import patch, MagicMock
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID

# SQLite type overrides for unit testing
@compiles(JSONB, 'sqlite')
def compile_jsonb_sqlite(type_, compiler, **kw):
    return 'JSON'

@compiles(PG_UUID, 'sqlite')
def compile_uuid_sqlite(type_, compiler, **kw):
    return 'TEXT'

from db.base import Base
from db.models import User, Dataset, DatasetFile, SpreadsheetAction, TransformationPlan, DatasetVersion
from config import Config
from storage.contracts import (
    StorageAccountConfig,
    StorageObjectNotFoundError,
    StorageAccountNotConfiguredError,
    StorageQuotaExceededError,
)
from storage.b2_client import B2Client
from storage.manager import StorageManager, storage_manager
from storage.migration_r2_to_b2 import R2ToB2Migrator
from spreadsheet.persistence import (
    SpreadsheetHistoryManager,
    ConcurrencyConflictError,
)
from datasets.registry import dataset_registry, DatasetEntry
from datasets.version_codec import serialize_dataframe, deserialize_dataframe


@pytest.fixture
def test_db():
    """Create an isolated in-memory SQLite database session for testing."""
    engine = create_engine('sqlite:///:memory:', echo=False)
    target_tables = [
        User.__table__,
        Dataset.__table__,
        DatasetFile.__table__,
        SpreadsheetAction.__table__,
        TransformationPlan.__table__,
        DatasetVersion.__table__,
    ]
    Base.metadata.create_all(engine, tables=target_tables)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def mock_storage():
    """Ensure storage manager uses a clean temporary local store for tests."""
    mgr = StorageManager()
    return mgr


# ─── PART 1: STORAGE ABSTRACTION, REGION RESOLUTION & ROUTING ────────────────

def test_b2_endpoint_and_region_resolution():
    """Test Backblaze B2 S3 endpoint formatting and region extraction."""
    # 1. Given endpoint URL, extract region
    cfg1 = StorageAccountConfig(
        account_id="005abc",
        access_key_id="005abc",
        secret_access_key="K005secret",
        bucket_name="my-bucket",
        endpoint_url="https://s3.us-west-004.backblazeb2.com",
    )
    assert cfg1.get_region_name() == "us-west-004"
    assert cfg1.get_endpoint_url() == "https://s3.us-west-004.backblazeb2.com"

    # 2. Given region name, format endpoint URL
    cfg2 = StorageAccountConfig(
        account_id="005xyz",
        access_key_id="005xyz",
        secret_access_key="K005secret",
        bucket_name="my-bucket",
        region_name="eu-central-003",
    )
    assert cfg2.get_region_name() == "eu-central-003"
    assert cfg2.get_endpoint_url() == "https://s3.eu-central-003.backblazeb2.com"


def test_storage_manager_routing_and_account_isolation(mock_storage):
    """Test strict account routing and unknown account rejection."""
    assert mock_storage.is_account_configured("primary")
    assert mock_storage.is_account_configured("b2")
    assert mock_storage.is_account_configured("account_a")

    client_primary = mock_storage.get_client("primary")
    client_b2 = mock_storage.get_client("b2")
    assert client_primary is client_b2

    # Requesting unconfigured account strictly raises StorageAccountNotConfiguredError
    with pytest.raises(StorageAccountNotConfiguredError):
        mock_storage.get_client("non_existent_account_xyz")


def test_server_controlled_key_builders(mock_storage):
    """Test safe server-controlled key generation and path traversal sanitization."""
    user_id = str(uuid.uuid4())
    dataset_id = str(uuid.uuid4())

    # Traversal characters must be sanitized
    dirty_filename = "../../evil/path/../financial_report.csv"
    orig_key = mock_storage.build_original_key(user_id, dataset_id, dirty_filename)
    assert ".." not in orig_key
    assert orig_key.startswith(f"users/{user_id}/datasets/{dataset_id}/original/")
    assert orig_key.endswith("financial_report.csv")

    # Snapshot key format
    snap_key = mock_storage.build_snapshot_key(user_id, dataset_id, 10, "plexis.json")
    assert snap_key == f"users/{user_id}/datasets/{dataset_id}/snapshots/rev_000010.plexis.json"

    # History segment key format
    seg_key = mock_storage.build_history_segment_key(user_id, dataset_id, "seg_001")
    assert seg_key == f"users/{user_id}/datasets/{dataset_id}/history/segment_seg_001.jsonl"


def test_b2_crud_and_presigned_urls(mock_storage):
    """Test object put, get, head, exists, stream, presigned URL, and delete operations."""
    test_key = f"test/{uuid.uuid4().hex}/sample.txt"
    test_data = b"Hello Backblaze B2 and Plexis!"

    # Put
    res = mock_storage.put_object(test_key, test_data, content_type="text/plain")
    assert res.success is True
    assert res.size_bytes == len(test_data)
    assert res.key == test_key

    # Exists
    assert mock_storage.object_exists(test_key) is True

    # Head
    meta = mock_storage.head_object(test_key)
    assert meta is not None
    assert meta.size_bytes == len(test_data)
    assert meta.content_type == "text/plain"

    # Get
    downloaded = mock_storage.get_object(test_key)
    assert downloaded == test_data

    # Stream (with context manager closing handle)
    with mock_storage.get_object_stream(test_key) as stream:
        assert stream.read() == test_data

    # Presigned URL
    url = mock_storage.generate_presigned_url(test_key, client_method="get_object", expires_in=1800)
    assert url is not None
    assert test_key in url or "sample.txt" in url

    # Delete
    deleted = mock_storage.delete_object(test_key)
    assert deleted is True
    assert mock_storage.object_exists(test_key) is False

    # Get non-existent raises StorageObjectNotFoundError
    with pytest.raises(StorageObjectNotFoundError):
        mock_storage.get_object(test_key)


# ─── PART 2: STORAGE QUOTA & ZERO-BUDGET SAFEGUARDS ──────────────────────────

def test_storage_quota_calculation_and_enforcement():
    """Test that exceeding configured storage quota blocks new uploads with StorageQuotaExceededError."""
    # Create client with a small 1 KB quota limit and unique isolated alias
    quota_limit_bytes = 1024
    unique_alias = f"test_quota_{uuid.uuid4().hex[:8]}"
    cfg = StorageAccountConfig(
        account_id="",
        access_key_id="",
        secret_access_key="",
        bucket_name="test-quota-bucket",
        account_alias=unique_alias,
        max_storage_bytes=quota_limit_bytes,
    )
    client = B2Client(cfg)
    try:
        # 1. Upload 600 bytes -> Should succeed
        small_data = b"X" * 600
        res1 = client.put_object("small.txt", small_data)
        assert res1.success is True

        usage = client.get_storage_usage()
        assert usage.total_bytes == 600
        assert usage.quota_bytes == quota_limit_bytes
        assert usage.quota_remaining_bytes == 424

        # 2. Upload another 500 bytes -> 600 + 500 = 1100 > 1024 -> Must raise StorageQuotaExceededError
        overflow_data = b"Y" * 500
        with pytest.raises(StorageQuotaExceededError) as exc_info:
            client.put_object("overflow.txt", overflow_data)

        assert exc_info.value.current_bytes == 600
        assert exc_info.value.incoming_bytes == 500
        assert exc_info.value.max_bytes == quota_limit_bytes
    finally:
        if hasattr(client, "_local_dir") and os.path.exists(client._local_dir):
            import shutil
            shutil.rmtree(client._local_dir, ignore_errors=True)


# ─── PART 3: SPREADSHEET ACTION HISTORY & CONCURRENCY TESTS ──────────────────

def _seed_test_dataset(db, row_count=5):
    """Helper to create a user and dataset with initial DataFrame in test DB."""
    user = User(id=uuid.uuid4(), email=f"user_{uuid.uuid4().hex[:6]}@plexis.local", email_normalized=f"user_{uuid.uuid4().hex[:6]}@plexis.local")
    db.add(user)
    db.flush()

    df = pd.DataFrame({
        "carrier": [f"Airline_{i}" for i in range(row_count)],
        "dep_delay": [10 * i for i in range(row_count)],
        "cancelled": [False] * row_count,
    })

    ds = Dataset(
        id=uuid.uuid4(),
        user_id=user.id,
        original_filename="flights.csv",
        storage_account="b2",
        storage_key=None,
        current_revision=0,
        file_hash="dummy_hash",
        row_count=row_count,
        column_count=3,
    )
    db.add(ds)
    db.commit()

    entry = DatasetEntry(
        dataset_id=str(ds.id),
        filename=ds.original_filename,
        file_path="",
        upload_timestamp="2026-10-26T00:00:00Z",
        row_count=len(df),
        column_count=len(df.columns),
        file_fingerprint="dummy_hash",
        schema_fingerprint="dummy_hash",
        dataframe=df,
        profile={},
        schema_profile=[],
        column_profiles={},
        ontology=None,
        dko=None,
    )
    dataset_registry.register(entry)
    return user, ds, df


def test_single_cell_edit_and_optimistic_concurrency(test_db):
    """Test cell edit increments revision and detects concurrent edits with 409 conflict."""
    mgr = SpreadsheetHistoryManager()
    user, ds, df = _seed_test_dataset(test_db)

    # Apply cell edit at row 0, col 'carrier' with base_revision=0
    res = mgr.apply_cell_edit(
        db=test_db,
        dataset_id=ds.id,
        row_idx=0,
        col_name="carrier",
        new_value="SuperAirways",
        base_revision=0,
        user_id=user.id,
    )

    assert res["success"] is True
    assert res["base_revision"] == 0
    assert res["resulting_revision"] == 1
    assert res["old_value"] == "Airline_0"
    assert res["value"] == "SuperAirways"

    cached_df = dataset_registry.get_by_id(str(ds.id)).dataframe
    assert cached_df.at[0, "carrier"] == "SuperAirways"

    test_db.refresh(ds)
    assert ds.current_revision == 1

    # Concurrent / Stale edit attempt (submitting with base_revision=0 instead of 1)
    with pytest.raises(ConcurrencyConflictError) as exc_info:
        mgr.apply_cell_edit(
            db=test_db,
            dataset_id=ds.id,
            row_idx=1,
            col_name="dep_delay",
            new_value=999,
            base_revision=0,  # Stale!
            user_id=user.id,
        )
    assert exc_info.value.expected_revision == 0
    assert exc_info.value.actual_revision == 1


def test_batch_cell_edit(test_db):
    """Test applying multiple edits atomically in one transaction."""
    mgr = SpreadsheetHistoryManager()
    user, ds, df = _seed_test_dataset(test_db)

    edits = [
        {"row": 0, "col": "dep_delay", "value": 15},
        {"row": 1, "col": "dep_delay", "value": 25},
        {"row": 2, "col": "carrier", "value": "SkyLine"},
    ]

    res = mgr.apply_batch_edit(
        db=test_db,
        dataset_id=ds.id,
        edits=edits,
        base_revision=0,
        user_id=user.id,
    )

    assert res["success"] is True
    assert res["resulting_revision"] == 1
    assert res["edit_count"] == 3

    cached_df = dataset_registry.get_by_id(str(ds.id)).dataframe
    assert cached_df.at[0, "dep_delay"] == 15
    assert cached_df.at[1, "dep_delay"] == 25
    assert cached_df.at[2, "carrier"] == "SkyLine"


def test_undo_and_redo_workflow(test_db):
    """Test undo reverses the DataFrame modification and redo reapplies it."""
    mgr = SpreadsheetHistoryManager()
    user, ds, df = _seed_test_dataset(test_db)

    # 1. Edit rev 0 -> 1: row 0 carrier = 'Alpha'
    mgr.apply_cell_edit(test_db, ds.id, 0, "carrier", "Alpha", base_revision=0, user_id=user.id)
    # 2. Edit rev 1 -> 2: row 0 carrier = 'Beta'
    mgr.apply_cell_edit(test_db, ds.id, 0, "carrier", "Beta", base_revision=1, user_id=user.id)

    cached_df = dataset_registry.get_by_id(str(ds.id)).dataframe
    assert cached_df.at[0, "carrier"] == "Beta"

    # 3. Undo: rev 2 -> 1
    undo_res = mgr.undo(test_db, ds.id, user.id)
    assert undo_res["success"] is True
    assert undo_res["current_revision"] == 1
    assert cached_df.at[0, "carrier"] == "Alpha"

    # 4. Undo: rev 1 -> 0
    undo_res2 = mgr.undo(test_db, ds.id, user.id)
    assert undo_res2["success"] is True
    assert undo_res2["current_revision"] == 0
    assert cached_df.at[0, "carrier"] == "Airline_0"

    # 5. Redo: rev 0 -> 1
    redo_res = mgr.redo(test_db, ds.id, user.id)
    assert redo_res["success"] is True
    assert redo_res["current_revision"] == 1
    assert cached_df.at[0, "carrier"] == "Alpha"

    # 6. Redo: rev 1 -> 2
    redo_res2 = mgr.redo(test_db, ds.id, user.id)
    assert redo_res2["success"] is True
    assert redo_res2["current_revision"] == 2
    assert cached_df.at[0, "carrier"] == "Beta"


def test_branch_invalidation_after_undo(test_db):
    """Applying a new edit after undo invalidates previous undone actions."""
    mgr = SpreadsheetHistoryManager()
    user, ds, df = _seed_test_dataset(test_db)

    mgr.apply_cell_edit(test_db, ds.id, 0, "carrier", "FirstEdit", base_revision=0, user_id=user.id)
    mgr.apply_cell_edit(test_db, ds.id, 0, "carrier", "SecondEdit", base_revision=1, user_id=user.id)

    # Undo back to rev 1
    mgr.undo(test_db, ds.id, user.id)
    history = mgr.get_history(test_db, ds.id, user.id)
    assert history["can_redo"] is True

    # Branch: Apply a new edit on top of rev 1
    mgr.apply_cell_edit(test_db, ds.id, 0, "carrier", "NewBranchEdit", base_revision=1, user_id=user.id)

    # The old SecondEdit must be invalidated; can_redo is now False
    history_after = mgr.get_history(test_db, ds.id, user.id)
    assert history_after["current_revision"] == 2
    assert history_after["can_redo"] is False


def test_snapshot_compaction_and_cold_start_recovery(test_db):
    """
    Test that periodic snapshots are stored in Backblaze B2, and cold-start rehydration
    rebuilds the DataFrame accurately after clearing all memory caches.
    """
    mgr = SpreadsheetHistoryManager()
    user, ds, df = _seed_test_dataset(test_db, row_count=3)

    # Configure snapshot interval to every 2 actions for fast test
    with patch.object(Config, "SNAPSHOT_INTERVAL_ACTIONS", 2):
        mgr.apply_cell_edit(test_db, ds.id, 0, "dep_delay", 100, base_revision=0, user_id=user.id)
        res2 = mgr.apply_cell_edit(test_db, ds.id, 1, "dep_delay", 200, base_revision=1, user_id=user.id)
        assert res2["snapshot_key"] is not None
        mgr.apply_cell_edit(test_db, ds.id, 2, "dep_delay", 300, base_revision=2, user_id=user.id)

    # Verify B2 has the snapshot
    assert storage_manager.object_exists(res2["snapshot_key"], account_identifier="b2") is True

    # ── SIMULATE COMPLETE SERVER CRASH & COLD START ──
    dataset_registry._datasets.clear()
    assert dataset_registry.get_by_id(str(ds.id)) is None

    # Cold-start rehydration
    restored_ds, restored_df = mgr.restore_dataset_state(test_db, ds.id, user.id)
    assert restored_df is not None
    assert len(restored_df) == 3
    assert restored_df.at[0, "dep_delay"] == 100
    assert restored_df.at[1, "dep_delay"] == 200
    assert restored_df.at[2, "dep_delay"] == 300

    # L1 cache must be re-populated
    cached_after = dataset_registry.get_by_id(str(ds.id))
    assert cached_after is not None
    assert cached_after.dataframe.at[2, "dep_delay"] == 300


def test_legacy_dataset_db_bytea_fallback(test_db):
    """Test legacy datasets with NULL storage_key seamlessly fall back to DB BYTEA."""
    import gzip
    mgr = SpreadsheetHistoryManager()
    user, ds, df = _seed_test_dataset(test_db, row_count=2)

    ds.storage_key = None
    test_db.commit()

    csv_bytes = df.to_csv(index=False).encode("utf-8")
    compressed = gzip.compress(csv_bytes)
    db_file = DatasetFile(
        id=uuid.uuid4(),
        dataset_id=ds.id,
        content=compressed,
        compression="gzip",
        checksum="legacy_checksum",
    )
    test_db.add(db_file)
    test_db.commit()

    dataset_registry._datasets.clear()

    restored_ds, restored_df = mgr.restore_dataset_state(test_db, ds.id, user.id)
    assert restored_df is not None
    assert len(restored_df) == 2
    assert list(restored_df.columns) == ["carrier", "dep_delay", "cancelled"]


# ─── PART 4: FLASK API ENDPOINTS TEST ────────────────────────────────────────

def test_spreadsheet_api_endpoints(test_db):
    """Test Flask endpoints /api/spreadsheet/edit, /undo, /redo, and /history."""
    from flask import Flask
    from api.spreadsheet_ops import spreadsheet_bp
    from api.transform import transform_bp

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(spreadsheet_bp)
    app.register_blueprint(transform_bp)

    user, ds, df = _seed_test_dataset(test_db)

    with app.test_client() as client:
        with patch("db.session.get_db", return_value=test_db), patch("api.transform.get_db", return_value=test_db):
            # 1. Edit Cell: Success 200
            resp = client.post("/api/spreadsheet/edit", json={
                "dataset_id": str(ds.id),
                "row": 0,
                "col": "carrier",
                "value": "VirginAir",
                "base_revision": 0,
            })
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["success"] is True
            assert data["resulting_revision"] == 1

            # 2. Edit Cell: Concurrency conflict 409
            resp_conflict = client.post("/api/spreadsheet/edit", json={
                "dataset_id": str(ds.id),
                "row": 0,
                "col": "carrier",
                "value": "LateConflictAir",
                "base_revision": 0,  # Stale!
            })
            assert resp_conflict.status_code == 409
            conflict_data = resp_conflict.get_json()
            assert conflict_data["conflict"] is True
            assert conflict_data["expected_revision"] == 0
            assert conflict_data["actual_revision"] == 1

            # 3. History endpoint
            hist_resp = client.get(f"/api/spreadsheet/history/{ds.id}")
            assert hist_resp.status_code == 200
            hist = hist_resp.get_json()
            assert hist["current_revision"] == 1
            assert hist["can_undo"] is True
            assert hist["can_redo"] is False

            # 4. Undo
            undo_resp = client.post("/api/spreadsheet/undo", json={"dataset_id": str(ds.id)})
            assert undo_resp.status_code == 200
            undo_data = undo_resp.get_json()
            assert undo_data["success"] is True

            # 5. Redo via transform_bp (/api/transform/redo)
            redo_resp = client.post("/api/transform/redo", json={"dataset_id": str(ds.id)})
            assert redo_resp.status_code == 200
            redo_data = redo_resp.get_json()
            assert redo_data["success"] is True
            assert redo_data["current_revision"] == 1


# ─── PART 5: MIGRATION UTILITY VERIFICATION ──────────────────────────────────

def test_r2_to_b2_migration_dry_run_and_execution(test_db):
    """Test the R2ToB2Migrator dry-run and execution verification."""
    user, ds, df = _seed_test_dataset(test_db)

    # Set up simulated legacy R2 storage key
    fake_key = f"users/{user.id}/datasets/{ds.id}/original/data.csv"
    ds.storage_account = "primary"
    ds.storage_key = fake_key
    test_db.commit()

    # Seed the file in local emulated store
    from config import Config
    local_source_path = os.path.join(Config.UPLOAD_FOLDER, "r2_emulated", "primary", fake_key)
    os.makedirs(os.path.dirname(local_source_path), exist_ok=True)
    with open(local_source_path, "wb") as f:
        f.write(b"carrier,dep_delay\nAirline_0,0\n")

    # 1. Dry run
    migrator_dry = R2ToB2Migrator(dry_run=True)
    summary_dry = migrator_dry.run_migration(test_db)
    assert summary_dry["status"] == "dry_run"
    assert summary_dry["pending_count"] == 1

    # In dry run, DB record must NOT be modified
    test_db.refresh(ds)
    assert ds.storage_account == "primary"

    # 2. Real Execution
    migrator_exec = R2ToB2Migrator(dry_run=False)
    summary_exec = migrator_exec.run_migration(test_db)
    assert summary_exec["status"] == "completed"
    assert summary_exec["succeeded"] == 1
    assert summary_exec["failed"] == 0

    # In real run, DB record is updated to 'b2' after verification
    test_db.refresh(ds)
    assert ds.storage_account == "b2"

    # Source file must STILL exist (non-destructive)
    assert os.path.exists(local_source_path) is True
