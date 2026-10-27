"""Plexis configuration management."""
import os
import warnings
from typing import List
from dotenv import load_dotenv

# Deterministic .env loading: load from the directory of this file, not CWD
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_ENV_PATH = os.path.join(_BACKEND_DIR, ".env")
load_dotenv(dotenv_path=_ENV_PATH)


def _dedup_clean_keys(keys: List[str]) -> List[str]:
    """Deduplicate non-empty keys preserving order without exposing secrets."""
    seen = set()
    result = []
    for k in keys:
        k_clean = (k or "").strip()
        if k_clean and k_clean not in seen:
            seen.add(k_clean)
            result.append(k_clean)
    return result


class Config:
    """Application configuration."""

    FLASK_PORT: int = int(os.environ.get("PORT", os.environ.get("FLASK_PORT", "5000")))
    FLASK_DEBUG: bool = os.environ.get("FLASK_DEBUG", "false").lower() in ("true", "1")
    SECRET_KEY: str = os.environ.get("SECRET_KEY", "plexis-secret-key-change-in-production-v2")

    # ── Role: Router ──────────────────────────────────────────────────────────
    MISTRAL_API_KEY: str = os.environ.get("MISTRAL_API_KEY", "").strip()
    NVIDIA_API_KEY: str = os.environ.get("NVIDIA_API_KEY", "").strip()
    NVIDIA_API_KEYS: List[str] = _dedup_clean_keys([
        os.environ.get("NVIDIA_API_KEY", ""),
        os.environ.get("NVIDIA_API_KEY_2", ""),
        os.environ.get("NVIDIA_API_KEY_3", ""),
    ])
    ROUTER_MISTRAL_MODEL: str = os.environ.get("ROUTER_MISTRAL_MODEL", "mistral-small-latest").strip()
    ROUTER_NVIDIA_MODEL: str = os.environ.get("ROUTER_NVIDIA_MODEL", "google/diffusiongemma-26b-a4b-it").strip()

    # ── Role: Transformation (Primary: Gemini Pool) ───────────────────────────
    GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "").strip()
    GEMINI_API_KEYS: List[str] = _dedup_clean_keys([
        os.environ.get("GEMINI_API_KEY", ""),
        os.environ.get("GEMINI_API_KEY_2", ""),
        os.environ.get("GEMINI_API_KEY_3", ""),
        os.environ.get("GEMINI_API_KEY_4", ""),
        os.environ.get("GEMINI_API_KEY_5", ""),
        os.environ.get("GEMINI_API_KEY_6", ""),
        os.environ.get("GEMINI_API_KEY_7", ""),
    ])
    TRANSFORMATION_GEMINI_MODEL: str = os.environ.get("TRANSFORMATION_GEMINI_MODEL", "gemini-2.5-flash").strip()

    # ── Role: Analysis & Conversation (Primary: OpenRouter Pool, Sec: Gemini) ─
    OPENROUTER_API_KEY: str = os.environ.get("OPENROUTER_API_KEY", "").strip()
    OPENROUTER_API_KEYS: List[str] = _dedup_clean_keys([
        os.environ.get("OPENROUTER_API_KEY", ""),
        os.environ.get("OPENROUTER_API_KEY_2", ""),
        os.environ.get("OPENROUTER_API_KEY_3", ""),
        os.environ.get("OPENROUTER_API_KEY_4", ""),
        os.environ.get("OPENROUTER_API_KEY_5", ""),
        os.environ.get("OPENROUTER_API_KEY_6", ""),
    ])
    OPENROUTER_FALLBACK_MODELS: str = os.environ.get(
        "OPENROUTER_FALLBACK_MODELS", "nvidia/nemotron-3.5-lightning:free,google/gemma-2-27b-it:free"
    ).strip()

    ANALYSIS_OPENROUTER_MODEL: str = os.environ.get("ANALYSIS_OPENROUTER_MODEL", "nvidia/nemotron-3.5-lightning:free").strip()
    ANALYSIS_OPENROUTER_FALLBACK_MODEL: str = os.environ.get(
        "ANALYSIS_OPENROUTER_FALLBACK_MODEL", "nvidia/nemotron-3.5-lightning:free"
    ).strip()
    ANALYSIS_GEMINI_MODEL: str = os.environ.get("ANALYSIS_GEMINI_MODEL", "gemini-2.5-flash").strip()

    CONVERSATION_OPENROUTER_MODEL: str = os.environ.get("CONVERSATION_OPENROUTER_MODEL", "nvidia/nemotron-3.5-lightning:free").strip()
    CONVERSATION_OPENROUTER_FALLBACK_MODEL: str = os.environ.get(
        "CONVERSATION_OPENROUTER_FALLBACK_MODEL", "nvidia/nemotron-3.5-lightning:free"
    ).strip()
    CONVERSATION_GEMINI_MODEL: str = os.environ.get("CONVERSATION_GEMINI_MODEL", "gemini-2.5-flash").strip()

    # ── Optional/Legacy Providers ─────────────────────────────────────────────
    GROQ_API_KEY: str = os.environ.get("GROQ_API_KEY", "").strip()
    GROQ_MODEL: str = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b").strip()
    GROQ_MODEL_FALLBACK: str = os.environ.get("GROQ_MODEL_FALLBACK", "openai/gpt-oss-120b").strip()
    GROQ_MODEL_HIGH_REASONING: str = os.environ.get("GROQ_MODEL_HIGH_REASONING", "openai/gpt-oss-120b").strip()

    # ── Temperature Policy ────────────────────────────────────────────────────
    PLANNER_TEMPERATURE: float = float(os.environ.get("PLANNER_TEMPERATURE", "0.0"))
    CONVERSATION_TEMPERATURE: float = float(os.environ.get("CONVERSATION_TEMPERATURE", "0.7"))

    MAX_UPLOAD_SIZE_MB: int = int(os.environ.get("MAX_UPLOAD_SIZE_MB", "35"))
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "DEBUG")
    ENABLE_DUCKDB_ENGINE: bool = os.environ.get("ENABLE_DUCKDB_ENGINE", "true").lower() in ("true", "1", "yes")

    # Parse comma-separated CORS origins into a list, combining configured origins with local dev
    _DEFAULT_CORS = "http://localhost:5173,http://localhost:5174,http://localhost:3000,http://127.0.0.1:5173"
    _CONFIGURED_CORS = os.environ.get("CORS_ORIGINS", "").strip()
    _COMBINED_CORS = f"{_DEFAULT_CORS},{_CONFIGURED_CORS}" if _CONFIGURED_CORS else _DEFAULT_CORS
    CORS_ORIGINS: List[str] = [
        origin.strip()
        for origin in _COMBINED_CORS.split(",")
        if origin.strip()
    ]

    UPLOAD_FOLDER: str = os.environ.get("UPLOAD_FOLDER", "storage/uploads")
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)

    # ── Object Storage (Backblaze B2 / S3-compatible) ────────────────────────
    STORAGE_PROVIDER: str = os.environ.get("STORAGE_PROVIDER", "b2").strip().lower()

    # Backblaze B2 Account A (Primary)
    B2_APPLICATION_KEY_ID: str = (
        os.environ.get("B2_APPLICATION_KEY_ID")
        or os.environ.get("B2_KEY_ID")
        or os.environ.get("B2_A_APPLICATION_KEY_ID")
        or ""
    ).strip()
    B2_APPLICATION_KEY: str = (
        os.environ.get("B2_APPLICATION_KEY")
        or os.environ.get("B2_APP_KEY")
        or os.environ.get("B2_A_APPLICATION_KEY")
        or ""
    ).strip()
    B2_BUCKET_NAME: str = (
        os.environ.get("B2_BUCKET_NAME")
        or os.environ.get("B2_A_BUCKET_NAME")
        or "plexis-datasets"
    ).strip()
    B2_S3_ENDPOINT_URL: str = (
        os.environ.get("B2_S3_ENDPOINT_URL")
        or os.environ.get("B2_ENDPOINT_URL")
        or os.environ.get("B2_A_S3_ENDPOINT_URL")
        or ""
    ).strip()
    B2_REGION: str = (
        os.environ.get("B2_REGION")
        or os.environ.get("B2_A_REGION")
        or ""
    ).strip()

    # Backblaze B2 Account B (Secondary / Optional)
    B2_B_APPLICATION_KEY_ID: str = os.environ.get("B2_B_APPLICATION_KEY_ID", "").strip()
    B2_B_APPLICATION_KEY: str = os.environ.get("B2_B_APPLICATION_KEY", "").strip()
    B2_B_BUCKET_NAME: str = os.environ.get("B2_B_BUCKET_NAME", "").strip()
    B2_B_S3_ENDPOINT_URL: str = os.environ.get("B2_B_S3_ENDPOINT_URL", "").strip()
    B2_B_REGION: str = os.environ.get("B2_B_REGION", "").strip()

    # Storage Quotas & Limits (Safeguards for zero-budget MVP)
    # Default maximum tracked storage: 10 GB (10,000 MB, Backblaze B2 free tier limit)
    B2_MAX_STORAGE_MB: int = int(os.environ.get("B2_MAX_STORAGE_MB", "10000"))
    MAX_UPLOAD_SIZE_MB: int = int(os.environ.get("MAX_UPLOAD_SIZE_MB", "35"))

    # Spreadsheet Snapshot Compaction
    SNAPSHOT_INTERVAL_ACTIONS: int = int(os.environ.get("SNAPSHOT_INTERVAL_ACTIONS", "20"))
    MAX_ACTION_HISTORY_DEPTH: int = int(os.environ.get("MAX_ACTION_HISTORY_DEPTH", "100"))

    # PostgreSQL database URL with postgres:// -> postgresql:// normalization for Supabase/Render
    _RAW_DB_URL: str = os.environ.get("DATABASE_URL", "").strip()
    DATABASE_URL: str = (
        "postgresql://" + _RAW_DB_URL[len("postgres://"):]
        if _RAW_DB_URL.startswith("postgres://")
        else _RAW_DB_URL
    )
    DB_POOL_SIZE: int = int(os.environ.get("DB_POOL_SIZE", "25"))

    @classmethod
    def validate(cls) -> None:
        """Validate configuration settings per role."""
        if not cls.MISTRAL_API_KEY and not cls.NVIDIA_API_KEY:
            warnings.warn("Router role: Neither MISTRAL_API_KEY nor NVIDIA_API_KEY is configured.")

        if not cls.OPENROUTER_API_KEYS and not cls.GEMINI_API_KEYS:
            warnings.warn("Analysis/Conversation roles: Neither OpenRouter nor Gemini API keys are configured.")

        if not cls.GEMINI_API_KEYS:
            warnings.warn("Transformation role: No Gemini API keys configured.")

        if not cls.DATABASE_URL:
            warnings.warn(
                "DATABASE_URL is not set. PostgreSQL persistence is disabled. "
                "Set DATABASE_URL in .env to enable multi-user persistence."
            )

        if cls.STORAGE_PROVIDER == "b2":
            if not cls.B2_APPLICATION_KEY_ID or not cls.B2_APPLICATION_KEY or not cls.B2_BUCKET_NAME:
                warnings.warn(
                    "STORAGE_PROVIDER=b2 is configured but Backblaze B2 credentials/bucket are incomplete. "
                    "Storage will run in local emulated fallback mode until B2_APPLICATION_KEY_ID, "
                    "B2_APPLICATION_KEY, and B2_BUCKET_NAME are configured in .env."
                )
