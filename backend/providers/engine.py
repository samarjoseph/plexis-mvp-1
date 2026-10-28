"""Provider Engine public API."""
import json
import logging
from typing import Generator, Optional, Dict, Any

from providers.base import ProviderResponse
from providers.domain.contracts import AIRequest
from providers.registry import provider_registry
from providers.health import health_monitor
from providers.circuit_breaker import circuit_breaker
from providers.model_registry import model_registry
from providers.fallback import fallback_engine

logger = logging.getLogger(__name__)


class ProviderEngine:
    """Main entrypoint for LLM provider orchestration."""

    def __init__(self) -> None:
        """Initialize engine (providers registered lazily on first call)."""
        self._initialized: bool = False

    def _ensure_initialized(self) -> None:
        """Lazily initialize providers on first use."""
        if self._initialized:
            return
        self._initialized = True

        try:
            from providers.mock_provider import MockProvider
            mock_provider = MockProvider(None)
            provider_registry.register(mock_provider, "mock_key")
            logger.info("ProviderEngine: registered provider=mock")
        except Exception as e:
            logger.error(f"Failed to load mock provider: {e}")

        from config import Config

        # Each provider is initialized independently.
        # A failure in one (e.g., SDK import error or missing API key) must NOT
        # prevent the others from loading. This is the provider isolation guarantee.

        if Config.GEMINI_API_KEYS:
            try:
                from providers.gemini import GeminiProvider
                gemini_provider = GeminiProvider(Config.GEMINI_API_KEYS[0])
                provider_registry.register(gemini_provider, Config.GEMINI_API_KEYS)
                logger.info(f"ProviderEngine: registered provider=gemini with {len(Config.GEMINI_API_KEYS)} key slots")
            except Exception as e:
                logger.warning(
                    f"ProviderEngine: skipping gemini provider — failed to register: {e}"
                )

        if Config.OPENROUTER_API_KEYS:
            try:
                from providers.openrouter import OpenRouterProvider
                openrouter_provider = OpenRouterProvider(Config.OPENROUTER_API_KEYS[0])
                provider_registry.register(openrouter_provider, Config.OPENROUTER_API_KEYS)
                logger.info(f"ProviderEngine: registered provider=openrouter with {len(Config.OPENROUTER_API_KEYS)} key slots")
            except Exception as e:
                logger.warning(
                    f"ProviderEngine: skipping openrouter provider — failed to register: {e}"
                )

        if Config.NVIDIA_API_KEYS:
            try:
                from providers.nvidia import NvidiaProvider
                nvidia_provider = NvidiaProvider(Config.NVIDIA_API_KEYS[0])
                provider_registry.register(nvidia_provider, Config.NVIDIA_API_KEYS)
                logger.info(f"ProviderEngine: registered provider=nvidia with {len(Config.NVIDIA_API_KEYS)} key slots")
            except Exception as e:
                logger.warning(f"ProviderEngine: skipping nvidia provider — failed to register: {e}")

        if Config.MISTRAL_API_KEY:
            try:
                from providers.mistral import MistralProvider
                mistral_provider = MistralProvider(Config.MISTRAL_API_KEY)
                provider_registry.register(mistral_provider, Config.MISTRAL_API_KEY)
                logger.info("ProviderEngine: registered provider=mistral")
            except Exception as e:
                logger.warning(
                    f"ProviderEngine: skipping mistral provider — failed to register: {e}"
                )

        if Config.GROQ_API_KEY:
            try:
                from providers.groq import GroqProvider
                groq_provider = GroqProvider(Config.GROQ_API_KEY)
                provider_registry.register(groq_provider, Config.GROQ_API_KEY)
                logger.info("ProviderEngine: registered provider=groq")
            except Exception as e:
                logger.warning(
                    f"ProviderEngine: skipping groq provider — failed to register: {e}"
                )

        available = provider_registry.get_available()
        logger.info(f"ProviderEngine: initialization complete. Available providers: {available}")

    def generate(self, request: AIRequest) -> ProviderResponse:
        """Generate text utilizing provider fallbacks and circuit breakers."""
        self._ensure_initialized()

        response = fallback_engine.execute(request)

        if response.success:
            model_registry.update_latency(response.model, response.latency_ms)
            model_registry.update_reliability(response.model, True)
            from core.events import event_bus, ProviderCalledEvent
            event_bus.publish(ProviderCalledEvent(
                provider=response.provider,
                model=response.model,
                latency_ms=response.latency_ms,
            ))
        else:
            if response.model != 'unknown':
                model_registry.update_reliability(response.model, False)

        return response

    def generate_stream(self, request: AIRequest) -> Generator[str, None, None]:
        """
        Stream text chunks from the best available provider.

        Uses the same provider selection and fallback logic as generate(),
        but calls generate_stream() on the selected provider to yield
        tokens progressively. If streaming fails on the primary provider,
        falls back to the base-class default (yields full text as one chunk).

        Callers are responsible for SSE formatting — this yields plain strings.
        """
        self._ensure_initialized()

        # Reuse the fallback engine's provider selection logic
        from providers.selector import model_selector
        model_meta = model_selector.select_fallback(
            task=request.task or "stream",
            require_json=request.json_mode,
            failed_providers=[]
        )
        if not model_meta:
            logger.error("generate_stream: no available providers")
            return

        provider_instance = provider_registry.get(model_meta.provider)
        if not provider_instance:
            logger.error(f"generate_stream: provider '{model_meta.provider}' not found in registry")
            return

        logger.info(
            f"generate_stream: using provider='{model_meta.provider}' "
            f"model='{model_meta.model_name}'"
        )

        from providers.scheduler import credential_scheduler
        slot = credential_scheduler.select_and_reserve(model_meta.provider, model_id=model_meta.model_id)
        api_key = slot.api_key if slot else provider_registry.get_api_key(model_meta.provider)

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
                f"\n\n[Active Runtime Model Context: This response is powered by model '{model_meta.model_id}' "
                f"via provider '{model_meta.provider}'. If the user asks which underlying model or provider powers this response, "
                f"disclose this information accurately while maintaining your identity as Plexis.]"
            )
            if hasattr(request, 'model_copy'):
                active_request = request.model_copy(update={
                    "model_id": model_meta.model_id,
                    "system_prompt": request.system_prompt + runtime_note
                })
            else:
                request.model_id = model_meta.model_id
                request.system_prompt += runtime_note
        elif hasattr(request, 'model_copy'):
            active_request = request.model_copy(update={"model_id": model_meta.model_id})
        elif hasattr(request, 'model_id'):
            request.model_id = model_meta.model_id

        try:
            try:
                yield from provider_instance.generate_stream(active_request, api_key=api_key)
            except TypeError:
                yield from provider_instance.generate_stream(active_request)
        except Exception as e:
            logger.error(f"generate_stream: provider '{model_meta.provider}' raised {e}")
            # Final fallback — non-streaming generate
            try:
                response = provider_instance.generate(active_request, api_key=api_key)
            except TypeError:
                response = provider_instance.generate(active_request)
            if response.success and response.text:
                yield response.text
        finally:
            if slot:
                credential_scheduler.release_reservation(slot)

    def generate_json(self, request: AIRequest) -> Dict[str, Any]:
        """Generate structured JSON utilizing provider fallbacks."""
        request.json_mode = True
        response = self.generate(request)

        if not response.success:
            return {'error': response.error or 'Failed to generate JSON', 'raw': None}

        try:
            return json.loads(response.text)
        except json.JSONDecodeError as e:
            logger.error(f"ProviderEngine failed to parse JSON: {e}")
            return {'error': 'Failed to parse JSON', 'raw': response.text}


# Module-level singleton — safe to import without triggering API calls
provider_engine = ProviderEngine()
