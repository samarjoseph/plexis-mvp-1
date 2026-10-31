"""
Gemini provider implementation using google-genai SDK.

SDK imports are LAZY (inside methods) to ensure provider isolation:
  - A Gemini SDK import failure does NOT crash Groq/Mistral/OpenRouter initialization.
  - Each provider loads its SDK only when actually called.
"""
import time
import json
import logging
from typing import Optional, Any, Generator, Dict

from providers.base import ProviderResponse
from providers.domain.contracts import IProviderAdapter, AIRequest, ProviderCapabilities
from providers.errors import ProviderExecutionError, ProviderAuthenticationError, ProviderRateLimitError

logger = logging.getLogger(__name__)


def _get_genai_client(api_key: str):
    """
    Lazily import and construct the google.genai client.
    Raises ImportError if the SDK is not available.
    Raises RuntimeError if client construction fails.
    """
    try:
        from google import genai as _genai
        from google.genai import types as _types  # noqa: F401
    except ImportError as e:
        raise ImportError(
            f"google-genai SDK not available: {e}. "
            "Install with: pip install google-genai"
        ) from e

    try:
        http_opts = _types.HttpOptions(timeout=10000) if hasattr(_types, 'HttpOptions') else None
        client = _genai.Client(api_key=api_key, http_options=http_opts)
        return client, _genai
    except Exception as e:
        raise RuntimeError(f"Failed to construct google.genai.Client: {e}") from e


class GeminiProvider(IProviderAdapter):
    """Gemini provider using google-genai SDK, with lazy SDK loading and multi-key support."""

    def __init__(self, api_key: str):
        """Store API key only — SDK imported lazily on first call."""
        self._api_key = api_key
        self._clients: Dict[str, Any] = {}

    def _get_client(self, api_key: Optional[str] = None):
        """Return (or create) the genai Client for the given API key."""
        key = api_key or self._api_key
        if key not in self._clients:
            client, _ = _get_genai_client(key)
            self._clients[key] = client
        return self._clients[key]

    @property
    def provider_name(self) -> str:
        return 'gemini'

    def get_capabilities(self, model_name: str) -> ProviderCapabilities:
        """Return capabilities for the given Gemini model."""
        if "pro" in model_name.lower() or "flash" in model_name.lower():
            return ProviderCapabilities(
                vision=True,
                embeddings=False,
                parallel_tool_calling=True,
                structured_output=True,
                temperature_support=True,
                multimodal=True,
                streaming=True,
                json_mode=True,
                reasoning_level="advanced",
                max_input_tokens=2000000,
                max_output_tokens=8192,
                latency_profile="medium",
                cost_profile="high"
            )
        else:
            return ProviderCapabilities(
                vision=True,
                embeddings=False,
                parallel_tool_calling=True,
                structured_output=True,
                temperature_support=True,
                multimodal=True,
                streaming=True,
                json_mode=True,
                reasoning_level="basic",
                max_input_tokens=1000000,
                max_output_tokens=8192,
                latency_profile="low",
                cost_profile="low"
            )

    def _convert_messages(self, messages: list) -> str:
        """Convert messages list to a prompt string, excluding system messages to avoid duplication."""
        non_system = [m for m in messages if m.get('role') != 'system']
        return "\n".join([f"{m.get('role', 'user')}: {m.get('content', '')}" for m in non_system])

    def _map_exception(self, e: Exception) -> Exception:
        """Map SDK exception to internal Provider errors."""
        try:
            from google.genai.errors import APIError
            if isinstance(e, APIError):
                if e.code in (401, 403):
                    return ProviderAuthenticationError(f"Gemini Auth Error: {e.message}")
                elif e.code == 429:
                    return ProviderRateLimitError(f"Gemini Rate Limit Exceeded: {e.message}")
                return ProviderExecutionError(f"Gemini API Error: {e.message}")
        except ImportError:
            pass
        return ProviderExecutionError(str(e))

    def _get_model_for_request(self, request: AIRequest) -> str:
        """Select model from model_registry for the given task."""
        model_name = None
        if hasattr(request, "model_id") and request.model_id:
            model_name = request.model_id
        else:
            try:
                from providers.model_registry import model_registry
                task = (request.task or '').lower()
                gemini_models = model_registry.get_models_for_provider('gemini')
                if gemini_models:
                    task_models = [
                        m for m in gemini_models
                        if m.task_affinities.get(task, 0) > 0 and m.health_score > 0.3
                    ]
                    if task_models:
                        task_models.sort(key=lambda m: (-m.task_affinities.get(task, 0), m.priority))
                        model_name = task_models[0].model_name
                    else:
                        gemini_models.sort(key=lambda m: m.priority)
                        model_name = gemini_models[0].model_name
            except Exception as exc:
                logger.warning('[GeminiProvider] model_registry lookup failed: %s', exc)
            if not model_name:
                from config import Config
                model_name = Config.TRANSFORMATION_GEMINI_MODEL

        # Gracefully alias obsolete 2.5-flash to 3.5-flash
        if model_name in ("gemini-2.5-flash", "models/gemini-2.5-flash"):
            model_name = "gemini-3.5-flash"
        return model_name

    def generate(self, request: AIRequest, api_key: Optional[str] = None) -> ProviderResponse:
        """Generate text using Gemini, governed by AIRequest."""
        try:
            from google.genai import types as _types

            client = self._get_client(api_key)
            prompt = self._convert_messages(request.messages) if request.messages else request.task
            model_name = self._get_model_for_request(request)

            system_instruction = request.system_prompt
            if not system_instruction and request.messages:
                system_msgs = [m.get('content', '') for m in request.messages if m.get('role') == 'system']
                if system_msgs:
                    system_instruction = "\n\n".join(system_msgs)

            response_mime_type = 'application/json' if request.json_mode else 'text/plain'

            config = _types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=request.temperature,
                max_output_tokens=request.max_tokens or 8192,
                response_mime_type=response_mime_type
            )

            start_time = time.time()
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=config
                )
            except Exception as call_err:
                if ("404" in str(call_err) or "NOT_FOUND" in str(call_err)) and model_name != "gemini-3.5-flash":
                    logger.warning(f"[GeminiProvider] Model {model_name} failed with 404/NOT_FOUND, retrying with gemini-3.5-flash")
                    model_name = "gemini-3.5-flash"
                    response = client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=config
                    )
                else:
                    raise call_err
            latency_ms = (time.time() - start_time) * 1000

            input_tokens = 0
            output_tokens = 0
            if response.usage_metadata:
                input_tokens = response.usage_metadata.prompt_token_count or 0
                output_tokens = response.usage_metadata.candidates_token_count or 0

            return ProviderResponse(
                text=response.text or "",
                model=model_name,
                provider=self.provider_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                success=True,
                status_code=200,
                raw_response={"text": response.text}
            )
        except (ImportError, RuntimeError) as sdk_err:
            logger.error(f"Gemini SDK unavailable: {sdk_err}")
            return ProviderResponse(
                text='', model="unknown", provider=self.provider_name,
                success=False, error=f"Gemini SDK unavailable: {sdk_err}"
            )
        except Exception as e:
            logger.error(f"Gemini generate error: {e}")
            mapped_error = self._map_exception(e)
            sc = getattr(e, 'code', None) or getattr(e, 'status_code', None)
            if sc is None and ('429' in str(e) or 'RESOURCE_EXHAUSTED' in str(e)):
                sc = 429
            elif sc is None and ('401' in str(e) or '403' in str(e) or 'UNAUTHENTICATED' in str(e)):
                sc = 401
            elif sc is None and ('504' in str(e) or 'DEADLINE_EXCEEDED' in str(e) or 'timeout' in str(e).lower()):
                sc = 504
            return ProviderResponse(
                text='', model=model_name if 'model_name' in locals() else 'unknown', provider=self.provider_name,
                success=False, error=str(mapped_error),
                status_code=sc
            )

    def generate_stream(self, request: AIRequest, api_key: Optional[str] = None) -> Generator[str, None, None]:
        """Stream text chunks using Gemini's native streaming API."""
        try:
            from google.genai import types as _types

            client = self._get_client(api_key)
            prompt = self._convert_messages(request.messages) if request.messages else request.task
            model_name = self._get_model_for_request(request)

            system_instruction = request.system_prompt
            if not system_instruction and request.messages:
                system_msgs = [m.get('content', '') for m in request.messages if m.get('role') == 'system']
                if system_msgs:
                    system_instruction = "\n\n".join(system_msgs)

            config = _types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=request.temperature,
                max_output_tokens=request.max_tokens or 8192,
            )

            try:
                response_stream = client.models.generate_content_stream(
                    model=model_name,
                    contents=prompt,
                    config=config
                )
            except Exception as call_err:
                if ("404" in str(call_err) or "NOT_FOUND" in str(call_err)) and model_name != "gemini-3.5-flash":
                    logger.warning(f"[GeminiProvider] Model {model_name} failed with 404/NOT_FOUND in stream, retrying with gemini-3.5-flash")
                    model_name = "gemini-3.5-flash"
                    response_stream = client.models.generate_content_stream(
                        model=model_name,
                        contents=prompt,
                        config=config
                    )
                else:
                    raise call_err
            for chunk in response_stream:
                if chunk.text:
                    yield chunk.text

        except (ImportError, RuntimeError) as sdk_err:
            logger.error(f"Gemini SDK unavailable for streaming: {sdk_err}")
            raise ProviderExecutionError(f"Gemini SDK unavailable: {sdk_err}")
        except Exception as e:
            logger.error(f"Gemini generate_stream error: {e}")
            raise self._map_exception(e)

    def health_check(self) -> bool:
        """Health check verifying credential presence."""
        return bool(self._api_key)
