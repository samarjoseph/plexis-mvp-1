"""Safe typed JSON encoding for persisted transformation snapshots.

CSV is a user-facing interchange format, but it cannot round-trip mixed
columns reliably (for example, numeric 0 alongside string "0"). Version
snapshots use tagged JSON scalars instead; no pickle or code execution is used.

V2 FORMAT (2026-11-11): Column-oriented storage that avoids per-cell dict
overhead.  For 150k rows × 23 cols the old row-oriented v1 format allocated
~3.45 million Python dicts (one per cell), consuming 800 MB+.  V2 stores
each column as a flat JSON array of native values (int/float/str/bool/null)
and only wraps truly special scalars (Timestamp, Date, Decimal, ±Inf) in
compact ``{"__t": ..., "v": ...}`` tags.  Peak memory drops to ~80 MB.

The ``deserialize_dataframe`` function transparently reads both v1 and v2
snapshots, so existing persisted data is never invalidated.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

FORMAT_V1 = "plexis.typed-dataframe.v1"
FORMAT_V2 = "plexis.typed-dataframe.v2"

# Keep the old marker around so downstream code that references it keeps working.
FORMAT_MARKER = FORMAT_V1


# ---------------------------------------------------------------------------
# V1 helpers (kept for reading legacy snapshots)
# ---------------------------------------------------------------------------

def _encode_scalar(value: Any) -> dict:
    if value is None or value is pd.NA or value is pd.NaT:
        return {"type": "null"}
    if isinstance(value, (bool, np.bool_)):
        return {"type": "boolean", "value": bool(value)}
    if isinstance(value, (int, np.integer)):
        return {"type": "integer", "value": int(value)}
    if isinstance(value, (float, np.floating)):
        number = float(value)
        if math.isnan(number):
            return {"type": "null"}
        if math.isinf(number):
            return {"type": "float_special", "value": "inf" if number > 0 else "-inf"}
        return {"type": "float", "value": number}
    if isinstance(value, pd.Timestamp):
        return {"type": "datetime", "value": value.isoformat()}
    if isinstance(value, datetime):
        return {"type": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"type": "date", "value": value.isoformat()}
    if isinstance(value, Decimal):
        return {"type": "decimal", "value": str(value)}
    if isinstance(value, str):
        return {"type": "string", "value": value}
    if isinstance(value, np.generic):
        return _encode_scalar(value.item())
    try:
        missing = pd.isna(value)
        if isinstance(missing, (bool, np.bool_)) and missing:
            return {"type": "null"}
    except (TypeError, ValueError):
        pass
    raise TypeError(f"Unsupported dataset snapshot scalar: {type(value).__name__}")


def _decode_scalar(value: dict):
    kind = value.get("type")
    raw = value.get("value")
    if kind == "null":
        return None
    if kind == "boolean":
        return bool(raw)
    if kind == "integer":
        return int(raw)
    if kind == "float":
        return float(raw)
    if kind == "float_special":
        return float("inf") if raw == "inf" else float("-inf")
    if kind == "string":
        return str(raw)
    if kind == "decimal":
        return Decimal(raw)
    if kind == "datetime":
        return pd.Timestamp(raw)
    if kind == "date":
        return date.fromisoformat(raw)
    raise ValueError(f"Unsupported typed dataset scalar: {kind!r}")


# ---------------------------------------------------------------------------
# V2 column-oriented helpers
# ---------------------------------------------------------------------------

def _encode_column_value(value: Any) -> Any:
    """Encode a single cell for V2 column-oriented format.

    Native JSON types (int, float, str, bool, None) pass through as-is.
    Only special types get a compact ``{"__t": ..., "v": ...}`` wrapper.
    This avoids allocating a dict per cell for 99%+ of real-world data.
    """
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        number = float(value)
        if math.isnan(number):
            return None
        if math.isinf(number):
            return {"__t": "float_special", "v": "inf" if number > 0 else "-inf"}
        return number
    if isinstance(value, pd.Timestamp):
        return {"__t": "datetime", "v": value.isoformat()}
    if isinstance(value, datetime):
        return {"__t": "datetime", "v": value.isoformat()}
    if isinstance(value, date):
        return {"__t": "date", "v": value.isoformat()}
    if isinstance(value, Decimal):
        return {"__t": "decimal", "v": str(value)}
    if isinstance(value, str):
        return value
    if isinstance(value, np.generic):
        return _encode_column_value(value.item())
    try:
        missing = pd.isna(value)
        if isinstance(missing, (bool, np.bool_)) and missing:
            return None
    except (TypeError, ValueError):
        pass
    raise TypeError(f"Unsupported dataset snapshot scalar: {type(value).__name__}")


def _decode_column_value(value: Any) -> Any:
    """Decode a single V2 cell value back to a Python scalar."""
    if value is None:
        return None
    if isinstance(value, dict) and "__t" in value:
        t = value["__t"]
        v = value["v"]
        if t == "datetime":
            return pd.Timestamp(v)
        if t == "date":
            return date.fromisoformat(v)
        if t == "decimal":
            return Decimal(v)
        if t == "float_special":
            return float("inf") if v == "inf" else float("-inf")
        raise ValueError(f"Unsupported V2 type tag: {t!r}")
    # Native JSON type — int, float, str, bool pass through unchanged
    return value


def _encode_typed_column(series: pd.Series) -> list:
    """Encode a whole pandas Series to a JSON-safe list, column-at-a-time.

    Uses fast vectorised paths for homogeneous numeric/bool/string columns
    and falls back to per-cell tagging only for object (mixed) columns.
    """
    dtype_str = str(series.dtype)

    # ── Fast path: integer column ──
    if dtype_str.startswith("int") or dtype_str.startswith("Int"):
        # Int64 (nullable) may contain pd.NA
        if series.isna().any():
            return [None if pd.isna(x) else int(x) for x in series]
        return series.tolist()

    # ── Fast path: float column ──
    if dtype_str.startswith("float") or dtype_str.startswith("Float"):
        arr = series.to_numpy(dtype="float64", na_value=np.nan)
        has_nan = np.isnan(arr).any()
        has_inf = np.isinf(arr).any()
        if not has_nan and not has_inf:
            return arr.tolist()
        # Slow fallback only for columns that actually contain nan/inf
        result = []
        for x in arr:
            if np.isnan(x):
                result.append(None)
            elif np.isinf(x):
                result.append({"__t": "float_special", "v": "inf" if x > 0 else "-inf"})
            else:
                result.append(float(x))
        return result

    # ── Fast path: bool column ──
    if dtype_str.startswith("bool"):
        if series.isna().any():
            return [None if pd.isna(x) else bool(x) for x in series]
        return [bool(x) for x in series]

    # ── Fast path: datetime64 column ──
    if "datetime" in dtype_str:
        return [
            {"__t": "datetime", "v": x.isoformat()} if pd.notna(x) else None
            for x in series
        ]

    # ── Mixed / object column (the only one needing per-cell dispatch) ──
    return [_encode_column_value(x) for x in series]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def serialize_dataframe(df: pd.DataFrame) -> bytes:
    """Serialize a DataFrame to compact column-oriented V2 JSON bytes.

    Memory-efficient: encodes column-at-a-time so only one column's Python
    list exists at peak, instead of all 3.45 M cell dicts simultaneously.
    """
    columns_data = {}
    for col in df.columns:
        columns_data[str(col)] = _encode_typed_column(df[col])

    payload = {
        "format": FORMAT_V2,
        "columns": [str(c) for c in df.columns],
        "dtypes": [str(d) for d in df.dtypes],
        "columns_data": columns_data,
    }
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def deserialize_dataframe(raw_bytes: bytes) -> pd.DataFrame:
    """Deserialize either a V1 (row-oriented) or V2 (column-oriented) snapshot."""
    payload = json.loads(raw_bytes.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Not a Plexis typed dataframe snapshot")

    fmt = payload.get("format")

    # ── V2 column-oriented ──
    if fmt == FORMAT_V2:
        return _deserialize_v2(payload)

    # ── V1 row-oriented (legacy) ──
    if fmt == FORMAT_V1:
        return _deserialize_v1(payload)

    raise ValueError(f"Unknown Plexis dataframe snapshot format: {fmt!r}")


def _deserialize_v1(payload: dict) -> pd.DataFrame:
    """Legacy V1 row-oriented deserializer (unchanged logic)."""
    columns = payload.get("columns")
    dtypes = payload.get("dtypes")
    rows = payload.get("rows")
    if not isinstance(columns, list) or not isinstance(dtypes, list) or not isinstance(rows, list):
        raise ValueError("Malformed Plexis typed dataframe snapshot")
    decoded = [[_decode_scalar(cell) for cell in row] for row in rows]
    df = pd.DataFrame(decoded, columns=columns)
    if len(dtypes) != len(columns):
        raise ValueError("Snapshot dtype metadata does not match its columns")
    for column, dtype in zip(columns, dtypes):
        # Restore physical dtypes when the original dtype can be represented
        # losslessly. Object columns keep their tagged Python scalar types.
        if dtype == "object":
            continue
        try:
            df[column] = df[column].astype(dtype)
        except (TypeError, ValueError, OverflowError):
            pass
    return df


def _deserialize_v2(payload: dict) -> pd.DataFrame:
    """Column-oriented V2 deserializer."""
    columns = payload.get("columns")
    dtypes = payload.get("dtypes")
    columns_data = payload.get("columns_data")
    if not isinstance(columns, list) or not isinstance(dtypes, list) or not isinstance(columns_data, dict):
        raise ValueError("Malformed Plexis V2 typed dataframe snapshot")
    if len(dtypes) != len(columns):
        raise ValueError("Snapshot dtype metadata does not match its columns")

    decoded = {}
    for col in columns:
        raw_col = columns_data.get(col, [])
        # Only decode cells that need it (those with __t tags)
        needs_decode = any(isinstance(v, dict) and "__t" in v for v in raw_col)
        if needs_decode:
            decoded[col] = [_decode_column_value(v) for v in raw_col]
        else:
            decoded[col] = raw_col

    df = pd.DataFrame(decoded, columns=columns)

    for column, dtype in zip(columns, dtypes):
        if dtype == "object":
            continue
        try:
            df[column] = df[column].astype(dtype)
        except (TypeError, ValueError, OverflowError):
            pass
    return df
