"""
Fallback and Execution Engine.

Model-level and provider-level fallback with Global Provider Quarantine and Circuit Breaker integration:
  - Centralized quarantine and circuit state across all subsystems.
  - Quota-scoped failure isolation with CredentialScheduler multi-key slot management.
  - Bounded half-open probe execution.
  - Category-specific cooldowns and Retry-After header adherence.
  - Role-compatible failover with strict preservation of semantic intent.
"""
import logging
from typing import List, Optional

from providers.base import ProviderResponse
from providers.domain.contracts import AIRequest
from providers.selector import model_selector
from providers.registry import provider_registry
from providers.quarantine import quarantine_manager
from providers.scheduler import credential_scheduler
from providers.model_registry import model_registry
from core.events import event_bus, ProviderFailedEvent, ProviderSwitchedEvent

logger = logging.getLogger(__name__)


class FallbackEngine:
    """Engine to orchestrate model-level and provider-level execution with fallbacks and global quarantine."""

    def execute(self, request: AIRequest) -> ProviderResponse:
        """Execute request with model-level fallbacks, multi-slot rotation, and global quarantine."""
        import time as _t
        start_time_exec = _t.time()
        failed_model_ids: List[str] = []
        attempts = 0
        max_attempts = 5

        while attempts < max_attempts:
            # Enforce max 12s total fallback budget so requests never hang for 40s
            if _t.time() - start_time_exec > 12.0 and len(failed_model_ids) > 0:
                logger.warning("[FallbackEngine] Total fallback budget (12s) reached. Aborting further model attempts.")
                break
            model_meta = model_selector.select_by_model_fallback(
                task=request.task or "fallback",
                require_json=request.json_mode,
                failed_model_ids=failed_model_ids,
            )
            if not model_meta:
                logger.warning("[FallbackEngine] No eligible models available (all models failed or quarantined).")
                quarantine_manager._metrics["all_providers_unavailable"] += 1
                return ProviderResponse(
                    text='',
                    model='unknown',
                    provider='unknown',
                    success=False,
                    error='All available providers are quarantined or exhausted.',
                    raw_response={'error_type': 'all_providers_unavailable', 'retryable': True}
                )

            provider_name = model_meta.provider
            model_id = model_meta.model_id
            provider_instance = provider_registry.get(provider_name)

            if not provider_instance:
                logger.warning(
                    "[FallbackEngine] Provider '%s' not registered — skipping model %s",
                    provider_name, model_id
                )
                failed_model_ids.append(model_id)
                continue

            # Select and reserve a credential slot via CredentialScheduler
            slot = credential_scheduler.select_and_reserve(provider_name, model_id=model_id)
            api_key = slot.api_key if slot else provider_registry.get_api_key(provider_name)

            # Check quarantine eligibility
            if not quarantine_manager.is_eligible(provider_name, api_key=api_key, model_id=model_id):
                if slot:
                    credential_scheduler.release_reservation(slot)
                logger.warning(
                    "[FallbackEngine] Scope quarantined for provider=%s model=%s — skipping",
                    provider_name, model_id
                )
                failed_model_ids.append(model_id)
                continue

            # Concurrency-safe half-open probe check
            if not quarantine_manager.acquire_probe_slot(provider_name, api_key=api_key, model_id=model_id):
                if slot:
                    credential_scheduler.release_reservation(slot)
                logger.warning(
                    "[FallbackEngine] Probe already in flight for provider=%s model=%s — skipping to avoid probe storm",
                    provider_name, model_id
                )
                failed_model_ids.append(model_id)
                continue

            attempts += 1
            slot_id_str = slot.slot_id if slot else "default"
            logger.info(
                "[FallbackEngine] Attempt %d: model=%s provider=%s slot=%s",
                attempts, model_id, provider_name, slot_id_str
            )

            # Call provider with active model awareness
            try:
                active_request = request
                user_prompt = getattr(request, 'prompt', '') or ''
                if not user_prompt and getattr(request, 'messages', None):
                    user_prompt = ' '.join(
                        str(m.get('content', '')) for m in request.messages if isinstance(m, dict) and m.get('role') == 'user'
                    )
                model_query_keywords = [
                    'what model', 'which model', 'underlying model', 'what llm', 'which llm',
                    'what ai model', 'who powers you', 'what powers you', 'are you nemotron',
                    'are you gpt', 'are you claude', 'are you gemini'
                ]
                should_inject = any(kw in user_prompt.lower() for kw in model_query_keywords)

                if getattr(request, 'system_prompt', None) and should_inject:
                    runtime_note = (
                        f"\n\n[Active Runtime Model Context: This response is powered by model '{model_id}' "
                        f"via provider '{provider_name}'. If the user asks which underlying model or provider powers this response, "
                        f"disclose this information accurately while maintaining your identity as Plexis.]"
                    )
                    if hasattr(request, 'model_copy'):
                        active_request = request.model_copy(update={
                            "model_id": model_id,
                            "system_prompt": request.system_prompt + runtime_note
                        })
                    else:
                        request.model_id = model_id
                        request.system_prompt += runtime_note
                elif hasattr(request, 'model_copy'):
                    active_request = request.model_copy(update={"model_id": model_id})
                elif hasattr(request, 'model_id'):
                    request.model_id = model_id

                # Pass api_key to provider adapter if supported
                try:
                    response = provider_instance.generate(active_request, api_key=api_key)
                except TypeError:
                    # Fallback for adapters with older signatures
                    response = provider_instance.generate(active_request)
            except Exception as exc:
                if slot:
                    credential_scheduler.release_reservation(slot)
                logger.error(
                    "[FallbackEngine] Provider '%s' raised exception: %s",
                    provider_name, exc
                )
                quarantine_manager.record_failure(
                    provider=provider_name,
                    error=exc,
                    api_key=api_key,
                    model_id=model_id,
                )
                event_bus.publish(ProviderFailedEvent(
                    provider=provider_name,
                    error=str(exc)
                ))
                failed_model_ids.append(model_id)
                continue

            # Release reserved slot capacity
            if slot:
                credential_scheduler.release_reservation(slot)

            # Successful response
            if response.success:
                quarantine_manager.record_success(
                    provider=provider_name,
                    api_key=api_key,
                    model_id=model_id,
                )
                logger.info(
                    "[FallbackEngine] Success: model=%s provider=%s slot=%s",
                    response.model, response.provider, slot_id_str
                )
                return response

            # Qualifying failure returned (HTTP 429, 5xx, auth, config, etc.)
            logger.warning(
                "[FallbackEngine] Provider error for model=%s provider=%s slot=%s: %s",
                model_id, provider_name, slot_id_str, response.error
            )

            quarantine_manager.record_failure(
                provider=provider_name,
                error=response.error,
                api_key=api_key,
                model_id=model_id,
                status_code=getattr(response, 'status_code', None),
                headers=getattr(response, 'headers', None),
                raw_response=getattr(response, 'raw_response', None),
            )

            event_bus.publish(ProviderFailedEvent(
                provider=provider_name,
                error=str(response.error)
            ))

            # Fast model failover: If error is model-level (404, 422, 5xx, timeout, or empty completion),
            # retrying other credential slots on the exact same provider model will fail the same way.
            status_code = getattr(response, 'status_code', None)
            err_str = str(response.error).lower()
            is_model_failure = (
                status_code in (404, 422, 500, 502, 503, 504)
                or "not found" in err_str
                or "empty completion" in err_str
                or "timeout" in err_str
                or "timed out" in err_str
                or "deadline" in err_str
            )
            if is_model_failure:
                if status_code in (404, 422) or "not found" in err_str:
                    model_registry.mark_permanently_failed(model_id)
                failed_model_ids.append(model_id)
            else:
                # Only 429 quota exhaustion is slot-specific — retry other available slots if eligible
                remaining_slots = [
                    s for s in credential_scheduler.get_slots(provider_name)
                    if quarantine_manager.is_eligible(provider_name, api_key=s.api_key, model_id=model_id)
                ]
                if not remaining_slots:
                    failed_model_ids.append(model_id)

            # Emit provider-switched event if next model is on a different provider
            next_model = model_selector.select_by_model_fallback(
                task=request.task or "fallback",
                require_json=request.json_mode,
                failed_model_ids=failed_model_ids,
            )
            if next_model and next_model.provider != provider_name:
                event_bus.publish(ProviderSwitchedEvent(
                    from_provider=provider_name,
                    to_provider=next_model.provider
                ))

        quarantine_manager._metrics["all_providers_unavailable"] += 1
        return ProviderResponse(
            text='',
            model='unknown',
            provider='unknown',
            success=False,
            error='All fallback attempts exhausted. Providers unavailable or quarantined.',
            raw_response={'error_type': 'all_providers_unavailable', 'retryable': True},
        )


fallback_engine = FallbackEngine()
