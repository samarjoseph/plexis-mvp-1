"""
Model Selection Engine with Role-Aware Isolation and Global Quarantine.

Enforces:
1. Hard role constraints:
   - Router role (classification, router): Mistral Small 4 -> Nvidia NIM direct
   - Analysis role: OpenRouter Gemma -> OpenRouter Nemotron -> Gemini
   - Transformation role (transformation_planning, transformation): Gemini pool only (no cross-role fallback)
   - Conversation role (chat, conversation, presentation, title): OpenRouter Gemma -> OpenRouter Nemotron -> Gemini
2. Quarantine and circuit eligibility check for provider, credential slot, and model ID.
3. Multi-credential quota awareness via CredentialScheduler.
"""
import logging
from typing import Optional, List

from providers.model_registry import model_registry, ModelMetadata
from providers.quarantine import quarantine_manager
from providers.registry import provider_registry
from providers.scheduler import credential_scheduler

logger = logging.getLogger(__name__)


class ModelSelector:
    """Selects the best available model for a task with hard role constraints."""

    def select(
        self,
        task: str,
        require_json: bool = False,
        exclude_providers: Optional[List[str]] = None,
    ) -> Optional[ModelMetadata]:
        """Select best model for task, excluding given providers (legacy API)."""
        exclude_providers = [p.lower() for p in (exclude_providers or [])]
        models = model_registry.get_models_for_task(task, require_json)

        valid_models = []
        for model in models:
            if model.provider.lower() in exclude_providers:
                continue
            # Check if any slot is eligible in scheduler
            slots = credential_scheduler.get_slots(model.provider)
            if slots:
                has_eligible_slot = any(
                    quarantine_manager.is_eligible(model.provider, api_key=s.api_key, model_id=model.model_id)
                    for s in slots
                )
                if not has_eligible_slot:
                    continue
            else:
                api_key = provider_registry.get_api_key(model.provider)
                if not quarantine_manager.is_eligible(model.provider, api_key=api_key, model_id=model.model_id):
                    continue
            valid_models.append(model)

        if not valid_models:
            logger.warning("No valid models found for task: %s", task)
            return None

        return self._score_and_pick(valid_models, task)

    def select_fallback(
        self,
        task: str,
        require_json: bool,
        failed_providers: List[str],
    ) -> Optional[ModelMetadata]:
        """Select a fallback model excluding failed providers (legacy API)."""
        return self.select(task, require_json, failed_providers)

    def select_by_model_fallback(
        self,
        task: str,
        require_json: bool,
        failed_model_ids: List[str],
    ) -> Optional[ModelMetadata]:
        """
        Select the best model for a task, excluding specific failed model IDs.
        Enforces role isolation, quarantine state, and quota scopes.
        """
        task_clean = (task or "fallback").lower().strip()
        models = model_registry.get_models_for_task(task_clean, require_json)

        valid_models = []
        for model in models:
            if model.model_id in failed_model_ids or model.model_name in failed_model_ids:
                continue

            # Verify credential eligibility via CredentialScheduler / QuarantineManager
            slots = credential_scheduler.get_slots(model.provider)
            if slots:
                has_eligible_slot = any(
                    quarantine_manager.is_eligible(model.provider, api_key=s.api_key, model_id=model.model_id)
                    for s in slots
                )
                if not has_eligible_slot:
                    logger.debug(
                        "[ModelSelector] All slots quarantined for provider=%s model=%s — skipping",
                        model.provider, model.model_id
                    )
                    continue
            else:
                api_key = provider_registry.get_api_key(model.provider)
                if not quarantine_manager.is_eligible(model.provider, api_key=api_key, model_id=model.model_id):
                    logger.debug(
                        "[ModelSelector] Quarantine ACTIVE for provider=%s model=%s — skipping",
                        model.provider, model.model_id
                    )
                    continue

            valid_models.append(model)

        if not valid_models:
            logger.warning(
                "[ModelSelector] No valid models for task=%s after excluding %s",
                task, failed_model_ids
            )
            return None

        selected = self._score_and_pick(valid_models, task_clean)
        if selected:
            logger.info(
                "[ModelSelector] Selected: model=%s provider=%s priority=%d task=%s",
                selected.model_id, selected.provider, selected.priority, task
            )
        return selected

    @staticmethod
    def _score_and_pick(models: List[ModelMetadata], task: str) -> Optional[ModelMetadata]:
        """Score models by effective priority, task affinity, reliability, and cost."""
        if not models:
            return None

        task_lower = (task or "").lower().strip()
        scored = []
        for model in models:
            affinity = model.task_affinities.get(task_lower, 0.5)
            cost_factor = 1.0 / (1.0 + getattr(model, 'estimated_cost_per_1k', 0.0))
            priority = getattr(model, 'priority', 5)

            # For transformation tasks, Gemini has dedicated Priority 0
            if task_lower in ("transformation_planning", "transformation") and model.provider == "gemini":
                priority = 0

            # For title generation, prioritize Gemini 2.5 Flash / Groq for sub-second latency and smart summarization
            if task_lower == "title" and model.provider in ("gemini", "groq"):
                priority = 0

            reliability = getattr(model, 'reliability_score', 1.0)

            # Priority is primary sort key (lower priority value = better score)
            score = (100 - priority * 10) * affinity * reliability * cost_factor
            scored.append((score, model))

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1]


model_selector = ModelSelector()
