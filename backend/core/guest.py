"""
Plexis Guest Mode Architecture.

Provides:
  - Cryptographic stateless guest token signing & verification (HMAC-SHA256).
  - Privacy-conscious, anonymized guest identifier derivation.
  - Server-side calendar-month transformation quota enforcement (max 2/month).
  - Isolated working copy provisioning for the preloaded sample CSV.
  - Guest permissions and boundary validation.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import pandas as pd

from config import Config
from datasets.column_profiler import column_profiler
from datasets.profiler import dataset_profiler
from datasets.registry import DatasetEntry, dataset_registry
from datasets.schema_profiler import schema_profiler

logger = logging.getLogger(__name__)

GUEST_MAX_MONTHLY_TRANSFORMATIONS = 2
GUEST_SESSION_DURATION_SECONDS = 4 * 3600  # 4 hours
GUEST_SAMPLE_FILENAME = "sample_customers.csv"

# In-memory quota fallback for tests and stateless operation
_in_memory_guest_quotas: Dict[str, Dict[str, int]] = {}
_cached_sample_df: Optional[pd.DataFrame] = None


def get_current_month_key() -> str:
    """Return calendar month key in format 'YYYY-MM' (UTC)."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def compute_guest_id(ip: str, user_agent: str, client_seed: Optional[str] = None) -> str:
    """
    Derive an anonymized, privacy-preserving guest identifier.
    Salted with the app SECRET_KEY so it cannot be mapped back to user IP or device info.
    """
    secret = getattr(Config, "SECRET_KEY", "plexis-guest-salt-key-2026")
    raw_ip = (ip or "").strip()
    # Mask the last octet of IPv4 or last segments of IPv6 for user privacy
    if "." in raw_ip:
        ip_parts = raw_ip.split(".")
        masked_ip = ".".join(ip_parts[:3] + ["0"])
    else:
        masked_ip = raw_ip[:16] if raw_ip else "unknown_ip"

    ua = (user_agent or "unknown_ua").strip()[:100]
    seed = (client_seed or "").strip()[:64]
    month = get_current_month_key()

    source = f"guest:{masked_ip}:{ua}:{seed}:{month}"
    return hmac.new(secret.encode("utf-8"), source.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def create_guest_token(session_id: str, guest_id: str, duration: int = GUEST_SESSION_DURATION_SECONDS) -> Tuple[str, int]:
    """
    Create a tamper-proof cryptographically signed guest session token.
    Format: gst.{session_id}.{guest_id}.{expires_at}.{signature}
    """
    secret = getattr(Config, "SECRET_KEY", "plexis-guest-salt-key-2026")
    expires_at = int(time.time()) + duration
    payload = f"{session_id}:{guest_id}:{expires_at}"
    sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    token = f"gst.{session_id}.{guest_id}.{expires_at}.{sig}"
    return token, expires_at


def verify_guest_token(token: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Verify signature and expiry of a guest token.
    Returns session dict if valid, None if invalid or expired.
    """
    if not token or not isinstance(token, str):
        return None

    cleaned = token.strip()
    if cleaned.lower().startswith("bearer "):
        cleaned = cleaned[7:].strip()

    parts = cleaned.split(".")
    if len(parts) != 5 or parts[0] != "gst":
        return None

    _, session_id, guest_id, expires_at_str, signature = parts
    try:
        expires_at = int(expires_at_str)
    except ValueError:
        return None

    if expires_at < int(time.time()):
        logger.warning("[GUEST] Token expired for session=%s", session_id)
        return None

    secret = getattr(Config, "SECRET_KEY", "plexis-guest-salt-key-2026")
    payload = f"{session_id}:{guest_id}:{expires_at}"
    expected_sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]

    if not hmac.compare_digest(signature, expected_sig):
        logger.warning("[GUEST] Invalid signature for session=%s", session_id)
        return None

    return {
        "session_id": session_id,
        "guest_id": guest_id,
        "expires_at": expires_at,
        "dataset_id": f"guest_ds_{session_id}",
    }


# ---------------------------------------------------------------------------
# Server-side Guest Quota Enforcement
# ---------------------------------------------------------------------------

def _ensure_guest_quotas_table(db) -> None:
    """Ensure guest_quotas table exists in PostgreSQL."""
    if db is None:
        return
    try:
        from sqlalchemy import text
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS guest_quotas (
                guest_id VARCHAR(64) NOT NULL,
                month_key VARCHAR(7) NOT NULL,
                transformations_count INTEGER NOT NULL DEFAULT 0,
                last_used_at TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
                PRIMARY KEY (guest_id, month_key)
            );
        """))
        db.commit()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.debug("[GUEST] Table check non-fatal error: %s", e)


def check_guest_transformation_quota(db, guest_id: str) -> Tuple[bool, int, int]:
    """
    Check remaining transformation quota for the guest identifier in the current calendar month.
    Returns: (allowed: bool, used_this_month: int, limit: int)
    """
    month_key = get_current_month_key()
    limit = GUEST_MAX_MONTHLY_TRANSFORMATIONS

    if not guest_id:
        return False, limit, limit

    # Check PostgreSQL first if available
    if db is not None:
        try:
            from sqlalchemy import text
            _ensure_guest_quotas_table(db)
            row = db.execute(
                text("SELECT transformations_count FROM guest_quotas WHERE guest_id = :gid AND month_key = :mkey"),
                {"gid": guest_id, "mkey": month_key}
            ).fetchone()
            if row is not None:
                used = int(row[0] or 0)
                return (used < limit), used, limit
        except Exception as e:
            logger.warning("[GUEST] DB quota read failed, checking in-memory fallback: %s", e)

    # In-memory fallback
    guest_record = _in_memory_guest_quotas.get(guest_id, {})
    used = guest_record.get(month_key, 0)
    return (used < limit), used, limit


def record_guest_transformation(db, guest_id: str) -> int:
    """
    Atomically increment consumed transformations for the guest in the current calendar month.
    Called ONLY upon successfully committed transformation execution.
    Returns: new used count
    """
    month_key = get_current_month_key()

    # Always update in-memory cache
    if guest_id not in _in_memory_guest_quotas:
        _in_memory_guest_quotas[guest_id] = {}
    _in_memory_guest_quotas[guest_id][month_key] = _in_memory_guest_quotas[guest_id].get(month_key, 0) + 1
    new_count = _in_memory_guest_quotas[guest_id][month_key]

    if db is not None:
        try:
            from sqlalchemy import text
            _ensure_guest_quotas_table(db)
            db.execute(text("""
                INSERT INTO guest_quotas (guest_id, month_key, transformations_count, last_used_at, updated_at)
                VALUES (:gid, :mkey, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (guest_id, month_key)
                DO UPDATE SET
                    transformations_count = guest_quotas.transformations_count + 1,
                    last_used_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP;
            """), {"gid": guest_id, "mkey": month_key})
            db.commit()

            # Retrieve exact DB count
            row = db.execute(
                text("SELECT transformations_count FROM guest_quotas WHERE guest_id = :gid AND month_key = :mkey"),
                {"gid": guest_id, "mkey": month_key}
            ).fetchone()
            if row:
                new_count = int(row[0] or 0)
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            logger.error("[GUEST] Failed to record quota in DB: %s", e)

    logger.info("[GUEST] Recorded transformation for guest=%s month=%s count=%d", guest_id, month_key, new_count)
    return new_count


# ---------------------------------------------------------------------------
# Preloaded Sample Dataset Management
# ---------------------------------------------------------------------------

def _load_sample_csv() -> pd.DataFrame:
    """Load the immutable sample customer dataset from disk."""
    global _cached_sample_df
    if _cached_sample_df is not None:
        return _cached_sample_df.copy(deep=True)

    base_dir = os.path.dirname(os.path.dirname(__file__))
    candidates = [
        os.path.join(base_dir, "data", "sample_customers.csv"),
        os.path.join(base_dir, "storage", "uploads", "sample-100-rows.csv"),
    ]

    for path in candidates:
        if os.path.exists(path):
            try:
                df = pd.read_csv(path)
                _cached_sample_df = df
                logger.info("[GUEST] Loaded sample dataset from: %s (%d rows)", path, len(df))
                return df.copy(deep=True)
            except Exception as e:
                logger.error("[GUEST] Failed reading sample CSV from %s: %s", path, e)

    # Fallback inline dataset if no CSV file exists
    fallback_data = {
        "id": list(range(1, 21)),
        "name": ["Ed Grady", "Sheri Dickinson", "Emilio Lindgren", "Hosea Trantow", "Clifton Bayer",
                 "Luna Bins", "Wilson Zboncak", "Sim Ebert", "Dr. Judson Windler", "Madisen Hintz",
                 "Lyla Schmidt", "Dr. Micheal Legros", "Winfield Friesen", "Kylie Jerde", "Daren Cole",
                 "Jacynthe Rolfson", "Adalberto Corkery", "Mariam Runolfsson", "Delfina Douglas", "Caitlyn Sawayn"],
        "city": ["South Tedbury", "Midland", "Pittsburg", "South Valley", "Port Williehaven",
                 "Muncie", "Mount Pleasant", "West Hyman", "South Kaitlin", "Des Moines",
                 "North Richland Hills", "Baldwin Park", "Margarettborough", "North Blairstad", "East Jordon",
                 "Lake Katelynn", "Port Eldred", "New Alex", "Lake Telly", "East Carmelo"],
        "age": [48, 37, 65, 29, 60, 61, 22, 51, 40, 42, 65, 55, 31, 49, 34, 52, 28, 45, 38, 62],
        "spend": [1240.50, 890.20, 2300.00, 450.00, 1780.40, 3100.80, 520.10, 1420.00, 980.50, 2150.75,
                  1670.30, 2890.00, 740.25, 1120.60, 1950.00, 680.40, 1340.90, 2210.00, 890.50, 3450.00],
    }
    df = pd.DataFrame(fallback_data)
    _cached_sample_df = df
    return df.copy(deep=True)


def provision_guest_dataset(session_id: str) -> DatasetEntry:
    """
    Provision an isolated working copy of the sample dataset for a guest session.
    Preserves the original source file completely intact.
    """
    dataset_id = f"guest_ds_{session_id}"

    # Return existing entry if already provisioned in registry
    existing = dataset_registry.get(dataset_id)
    if existing and existing.dataframe is not None:
        return existing

    working_df = _load_sample_csv()

    profile = dataset_profiler.profile(working_df)
    schema_prof = schema_profiler.profile(working_df)
    col_prof = {col: column_profiler.profile(working_df, col) for col in working_df.columns}

    entry = DatasetEntry(
        dataset_id=dataset_id,
        filename=GUEST_SAMPLE_FILENAME,
        file_path="sample_customers.csv",
        upload_timestamp=datetime.now(timezone.utc).isoformat(),
        row_count=len(working_df),
        column_count=len(working_df.columns),
        file_fingerprint=hashlib.md5(working_df.to_csv().encode()).hexdigest(),
        schema_fingerprint=hashlib.md5(",".join(working_df.columns).encode()).hexdigest(),
        dataframe=working_df,
        profile=profile,
        schema_profile=schema_prof,
        column_profiles=col_prof,
    )

    dataset_registry.register(entry)
    logger.info("[GUEST] Provisioned isolated dataset %s for session=%s", dataset_id, session_id)
    return entry


def is_guest_dataset(dataset_id: Optional[str]) -> bool:
    """Check if the dataset ID is a guest session isolated dataset."""
    return bool(dataset_id and str(dataset_id).startswith("guest_ds_"))
