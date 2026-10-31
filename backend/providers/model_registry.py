"""
Model registry and metadata definitions for LLM providers.

Authoritative Provider & Role Architecture:
1. Router Role:
   - Primary:   mistral / mistral-small-latest (priority 0)
   - Fallback:  nvidia / google/diffusiongemma-26b-a4b-it (priority 1)
2. Analysis Role:
   - Primary:        openrouter / google/gemma-2-27b-it:free (priority 0)
   - Model Fallback: openrouter / nvidia/nemotron-3-super-120b-a12b:free (priority 1)
   - Secondary:      gemini / gemini-2.0-flash (priority 2)
3. Transformation Role:
   - Dedicated:      gemini / gemini-2.0-flash (priority 0)
   - NO cross-role fallback to Mistral or Nvidia NIM.
4. Conversation Role:
   - Primary:        openrouter / google/gemma-2-27b-it:free (priority 0)
   - Model Fallback: openrouter / nvidia/nemotron-3-super-120b-a12b:free (priority 1)
   - Secondary:      gemini / gemini-2.0-flash (priority 2)
"""
from dataclasses import dataclass, field
from typing import Optional, Dict, List
import logging
from config import Config

logger = logging.getLogger(__name__)


@dataclass
class ModelMetadata:
    model_id: str
    provider: str
    model_name: str
    context_window: int
    max_output_tokens: int
    supports_vision: bool
    supports_json_mode: bool
    supports_tool_calling: bool
    supports_streaming: bool
    supports_reasoning: bool
    task_affinities: Dict[str, float] = field(default_factory=dict)
    avg_latency_ms: float = 0.0
    estimated_cost_per_1k: float = 0.0
    reliability_score: float = 1.0
    health_score: float = 1.0
    priority: int = 0  # Lower number = higher priority


class ModelRegistry:
    """Registry to manage metadata and role matching for all available models."""

    def __init__(self):
        self._models: Dict[str, ModelMetadata] = {}

    def register(self, model: ModelMetadata) -> None:
        """Register a model with metadata."""
        self._models[model.model_id] = model
        logger.debug(
            "[ModelRegistry] Registered model %s (provider=%s health=%.1f priority=%d)",
            model.model_id, model.provider, model.health_score, model.priority
        )

    def get(self, model_id: str) -> Optional[ModelMetadata]:
        """Get model metadata by ID."""
        return self._models.get(model_id)

    def get_models_for_provider(self, provider: str) -> List[ModelMetadata]:
        """Get all ACTIVE (health_score > 0.3) models for a specific provider."""
        return [
            m for m in self._models.values()
            if m.provider == provider.lower() and m.health_score > 0.3
        ]

    def get_models_for_task(self, task: str, require_json: bool = False) -> List[ModelMetadata]:
        """
        Get all active models eligible for a task, sorted by priority (ascending)
        and then task affinity (descending).
        """
        task_clean = (task or "").lower().strip()
        result = []
        for m in self._models.values():
            if m.health_score <= 0.3:
                continue
            if require_json and not m.supports_json_mode:
                continue
            affinity = m.task_affinities.get(task_clean, 0.0)
            if affinity > 0:
                result.append(m)

        result.sort(key=lambda m: (m.priority, -m.task_affinities.get(task_clean, 0.0)))
        return result

    def all_models(self) -> List[ModelMetadata]:
        """Get all registered models (including inactive)."""
        return list(self._models.values())

    def active_models(self) -> List[ModelMetadata]:
        """Get only active models (health_score > 0.3)."""
        return [m for m in self._models.values() if m.health_score > 0.3]

    def update_health(self, model_id: str, score: float) -> None:
        if model_id in self._models:
            self._models[model_id].health_score = max(0.0, min(1.0, score))

    def mark_permanently_failed(self, model_id: str) -> None:
        """Mark a model as permanently unavailable (e.g. 404 / decommissioned)."""
        if model_id in self._models:
            self._models[model_id].health_score = 0.0
            logger.warning("[ModelRegistry] Model permanently unavailable: %s", model_id)

    def update_latency(self, model_id: str, latency_ms: float) -> None:
        if model_id in self._models:
            model = self._models[model_id]
            model.avg_latency_ms = (model.avg_latency_ms * 0.9) + (latency_ms * 0.1)

    def update_reliability(self, model_id: str, success: bool) -> None:
        if model_id in self._models:
            model = self._models[model_id]
            value = 1.0 if success else 0.0
            model.reliability_score = (model.reliability_score * 0.95) + (value * 0.05)


model_registry = ModelRegistry()

# ══════════════════════════════════════════════════════════════════════════════
# 0. GROQ ULTRA-FAST MODELS (When Config.GROQ_API_KEY is present)
# ══════════════════════════════════════════════════════════════════════════════
if Config.GROQ_API_KEY:
    # GROQ FAST MODEL: 20B ultra-fast routing & conversation (Priority 0)
    model_registry.register(ModelMetadata(
        model_id=Config.GROQ_MODEL,  # openai/gpt-oss-20b
        provider="groq",
        model_name=Config.GROQ_MODEL,
        context_window=131072,
        max_output_tokens=8192,
        supports_vision=False,
        supports_json_mode=True,
        supports_tool_calling=True,
        supports_streaming=True,
        supports_reasoning=False,
        task_affinities={
            "classification": 1.0,
            "router": 1.0,
            "chat": 1.0,
            "conversation": 1.0,
            "planner": 0.9,
            "interpreter": 0.9,
            "title": 1.0,
            "transformation_planning": 0.95,
            "transformation": 0.95,
        },
        avg_latency_ms=180.0,
        priority=0,
        health_score=1.0,
    ))

    # GROQ FRONTIER MODEL: 120B reasoning/analysis (Priority 1)
    model_registry.register(ModelMetadata(
        model_id=Config.GROQ_MODEL_HIGH_REASONING,  # openai/gpt-oss-120b
        provider="groq",
        model_name=Config.GROQ_MODEL_HIGH_REASONING,
        context_window=131072,
        max_output_tokens=8192,
        supports_vision=False,
        supports_json_mode=True,
        supports_tool_calling=True,
        supports_streaming=True,
        supports_reasoning=True,
        task_affinities={
            "classification": 0.95,
            "router": 0.95,
            "planner": 1.0,
            "interpreter": 1.0,
            "insight_synthesizer": 1.0,
            "explanation": 1.0,
            "spreadsheet_interpret": 1.0,
            "analysis": 1.0,
            "complex_analysis": 1.0,
            "report": 1.0,
            "high_reasoning": 1.0,
            "chat": 0.95,
            "conversation": 0.95,
            "presentation": 1.0,
            "title": 0.9,
        },
        avg_latency_ms=300.0,
        priority=1,
        health_score=1.0,
    ))

# ══════════════════════════════════════════════════════════════════════════════
# 1. ROUTER ROLE MODELS
# ══════════════════════════════════════════════════════════════════════════════

# PRIMARY ROUTER: Mistral Small (Priority 1 if Groq active, else Priority 0)
model_registry.register(ModelMetadata(
    model_id=Config.ROUTER_MISTRAL_MODEL,
    provider="mistral",
    model_name=Config.ROUTER_MISTRAL_MODEL,
    context_window=32768,
    max_output_tokens=4096,
    supports_vision=False,
    supports_json_mode=True,
    supports_tool_calling=True,
    supports_streaming=True,
    supports_reasoning=False,
    task_affinities={
        "classification": 1.0,
        "router": 1.0,
    },
    avg_latency_ms=450.0,
    priority=1 if Config.GROQ_API_KEY else 0,
    health_score=1.0,
))

# SECONDARY ROUTER: NVIDIA NIM Direct (Priority 1 fallback)
model_registry.register(ModelMetadata(
    model_id=Config.ROUTER_NVIDIA_MODEL,
    provider="nvidia",
    model_name=Config.ROUTER_NVIDIA_MODEL,
    context_window=128000,
    max_output_tokens=4096,
    supports_vision=True,
    supports_json_mode=True,
    supports_tool_calling=False,
    supports_streaming=True,
    supports_reasoning=False,
    task_affinities={
        "classification": 0.9,
        "router": 0.9,
    },
    avg_latency_ms=550.0,
    priority=1,
    health_score=1.0,
))


# ══════════════════════════════════════════════════════════════════════════════
# 2. ANALYSIS & CONVERSATION ROLE MODELS
# ══════════════════════════════════════════════════════════════════════════════

# PRIMARY ANALYSIS & CONVERSATION: OpenRouter Gemma (Priority 0)
model_registry.register(ModelMetadata(
    model_id=Config.ANALYSIS_OPENROUTER_MODEL,
    provider="openrouter",
    model_name=Config.ANALYSIS_OPENROUTER_MODEL,
    context_window=131072,
    max_output_tokens=8192,
    supports_vision=True,
    supports_json_mode=True,
    supports_tool_calling=True,
    supports_streaming=True,
    supports_reasoning=False,
    task_affinities={
        # Analysis tasks
        "planner": 1.0,
        "interpreter": 1.0,
        "insight_synthesizer": 1.0,
        "explanation": 1.0,
        "spreadsheet_interpret": 1.0,
        "analysis": 1.0,
        "complex_analysis": 1.0,
        "report": 1.0,
        # Router & Classification tasks (fast fallback)
        "classification": 0.95,
        "router": 0.95,
        # Transformation tasks
        "transformation_planning": 0.95,
        "transformation": 0.95,
        # Conversation tasks
        "chat": 1.0,
        "conversation": 1.0,
        "presentation": 1.0,
        "title": 0.6,
        "stream": 1.0,
    },
    avg_latency_ms=650.0,
    priority=0,
    health_score=1.0,
))

# MODEL-LEVEL FALLBACK: OpenRouter Nemotron 3 Super (Priority 1)
model_registry.register(ModelMetadata(
    model_id=Config.ANALYSIS_OPENROUTER_FALLBACK_MODEL,
    provider="openrouter",
    model_name=Config.ANALYSIS_OPENROUTER_FALLBACK_MODEL,
    context_window=128000,
    max_output_tokens=8192,
    supports_vision=False,
    supports_json_mode=True,
    supports_tool_calling=True,
    supports_streaming=True,
    supports_reasoning=True,
    task_affinities={
        # Analysis tasks
        "planner": 0.9,
        "interpreter": 0.9,
        "insight_synthesizer": 0.9,
        "explanation": 0.9,
        "spreadsheet_interpret": 0.9,
        "analysis": 0.9,
        "complex_analysis": 1.0,
        "report": 0.9,
        "high_reasoning": 1.0,
        # Conversation tasks
        "chat": 0.9,
        "conversation": 0.9,
        "presentation": 0.9,
        "title": 0.5,
        "stream": 0.9,
    },
    avg_latency_ms=900.0,
    priority=1,
    health_score=1.0,
))

# SECONDARY PROVIDER (Analysis & Conversation): Google Gemini (Priority 2)
# AND PRIMARY DEDICATED PROVIDER FOR TRANSFORMATION (Priority 0)
model_registry.register(ModelMetadata(
    model_id=Config.TRANSFORMATION_GEMINI_MODEL,
    provider="gemini",
    model_name=Config.TRANSFORMATION_GEMINI_MODEL,
    context_window=1048576,
    max_output_tokens=8192,
    supports_vision=True,
    supports_json_mode=True,
    supports_tool_calling=True,
    supports_streaming=True,
    supports_reasoning=True,
    task_affinities={
        # Dedicated Transformation Role (Priority 0 for transformation)
        "transformation_planning": 1.0,
        "transformation": 1.0,
        # Classification & Router Fallback (Fast, resilient failover)
        "classification": 0.95,
        "router": 0.95,
        # Secondary Analysis & Conversation (Priority 1 fallback)
        "planner": 0.9,
        "interpreter": 0.9,
        "insight_synthesizer": 0.9,
        "explanation": 0.9,
        "spreadsheet_interpret": 0.9,
        "analysis": 0.9,
        "complex_analysis": 0.9,
        "report": 0.9,
        "chat": 0.9,
        "conversation": 0.9,
        "presentation": 0.9,
        "title": 0.5,
        "stream": 0.9,
    },
    avg_latency_ms=750.0,
    priority=3,  # Free tier fallback only
    health_score=0.8,
))

# FLAGSHIP GEMINI MODEL: gemini-3.5-flash (High speed, 4-key rotation, rock-solid primary)
model_registry.register(ModelMetadata(
    model_id="gemini-3.5-flash",
    provider="gemini",
    model_name="gemini-3.5-flash",
    context_window=1048576,
    max_output_tokens=8192,
    supports_vision=True,
    supports_json_mode=True,
    supports_tool_calling=True,
    supports_streaming=True,
    supports_reasoning=True,
    task_affinities={
        "transformation_planning": 1.0,
        "transformation": 1.0,
        "classification": 0.98,
        "router": 0.98,
        "planner": 0.98,
        "interpreter": 0.98,
        "insight_synthesizer": 0.98,
        "explanation": 0.98,
        "spreadsheet_interpret": 0.98,
        "analysis": 0.98,
        "complex_analysis": 0.98,
        "report": 0.98,
        "chat": 0.98,
        "conversation": 0.98,
        "presentation": 0.98,
        "title": 1.0,
        "stream": 0.98,
    },
    avg_latency_ms=500.0,
    priority=0,
    health_score=1.0,
))

# Secondary / fallback alias for gemini-2.5-flash
model_registry.register(ModelMetadata(
    model_id="gemini-2.5-flash",
    provider="gemini",
    model_name="gemini-2.5-flash",
    context_window=1048576,
    max_output_tokens=8192,
    supports_vision=True,
    supports_json_mode=True,
    supports_tool_calling=True,
    supports_streaming=True,
    supports_reasoning=True,
    task_affinities={
        "transformation_planning": 0.8,
        "transformation": 0.8,
        "classification": 0.8,
        "router": 0.8,
        "planner": 0.8,
        "interpreter": 0.8,
        "insight_synthesizer": 0.8,
        "explanation": 0.8,
        "spreadsheet_interpret": 0.8,
        "analysis": 0.8,
        "complex_analysis": 0.8,
        "report": 0.8,
        "chat": 0.8,
        "conversation": 0.8,
        "presentation": 0.8,
        "title": 0.8,
        "stream": 0.8,
    },
    avg_latency_ms=500.0,
    priority=1,
    health_score=1.0,
))


# ══════════════════════════════════════════════════════════════════════════════
# 3. MOCK PROVIDER (testing only)
# ══════════════════════════════════════════════════════════════════════════════
model_registry.register(ModelMetadata(
    model_id="mock",
    provider="mock",
    model_name="mock",
    context_window=1024,
    max_output_tokens=1024,
    supports_vision=False,
    supports_json_mode=True,
    supports_tool_calling=False,
    supports_streaming=False,
    supports_reasoning=False,
    task_affinities={
        "classification": 1.0,
        "router": 1.0,
        "planner": 1.0,
        "chat": 1.0,
        "conversation": 1.0,
        "transformation_planning": 1.0,
        "transformation": 1.0,
    },
    priority=99,
    health_score=0.0,  # Enabled dynamically in tests
))
