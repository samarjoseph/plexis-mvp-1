import time
import json
import logging
import requests
from typing import Optional, Dict, Any, Generator

from providers.base import ProviderResponse
from providers.domain.contracts import IProviderAdapter, AIRequest, ProviderCapabilities
from providers.model_registry import model_registry

logger = logging.getLogger(__name__)


class OpenRouterProvider(IProviderAdapter):
    """OpenRouter provider using requests API, behind ACL."""

    def __init__(self, api_key: str):
        """Initialize OpenRouter provider."""
        self._api_key = api_key
        self.endpoint = 'https://openrouter.ai/api/v1/chat/completions'
        from config import Config
        self.fallback_models = [m.strip() for m in Config.OPENROUTER_FALLBACK_MODELS.split(",") if m.strip()]

    @property
    def provider_name(self) -> str:
        return 'openrouter'

    def get_capabilities(self, model_name: str) -> ProviderCapabilities:
        """Return capabilities for the given OpenRouter model."""
        return ProviderCapabilities(
            vision=True,
            embeddings=False,
            parallel_tool_calling=True,
            structured_output=True,
            temperature_support=True,
            multimodal=True,
            streaming=True,
            json_mode=True,
            reasoning_level="advanced" if "nemotron" in model_name.lower() or "120b" in model_name.lower() else "basic",
            max_input_tokens=128000,
            max_output_tokens=4096,
            latency_profile="medium",
            cost_profile="medium"
        )

    def _get_model_for_request(self, request: AIRequest) -> str:
        """Select the appropriate OpenRouter model dynamically."""
        if hasattr(request, "model_id") and request.model_id:
            return request.model_id

        try:
            task = (request.task or '').lower()
            openrouter_models = model_registry.get_models_for_provider('openrouter')
            if openrouter_models:
                task_models = [
                    m for m in openrouter_models
                    if m.task_affinities.get(task, 0) > 0 and m.health_score > 0.3
                ]
                if task_models:
                    task_models.sort(key=lambda m: (m.priority, -m.task_affinities.get(task, 0)))
                    return task_models[0].model_name
                openrouter_models_sorted = sorted(openrouter_models, key=lambda m: m.priority)
                return openrouter_models_sorted[0].model_name
        except Exception as exc:
            logger.warning('[OpenRouterProvider] model_registry lookup failed: %s', exc)

        from config import Config
        return Config.ANALYSIS_OPENROUTER_MODEL or "google/gemma-2-27b-it:free"

    def generate(self, request: AIRequest, api_key: Optional[str] = None) -> ProviderResponse:
        """Generate text using OpenRouter, governed by AIRequest."""
        effective_key = api_key or self._api_key
        try:
            messages = request.messages.copy() if request.messages else []
            if not messages and request.task:
                messages.append({'role': 'user', 'content': request.task})

            if request.system_prompt and not any(m.get('role') == 'system' for m in messages):
                messages.insert(0, {'role': 'system', 'content': request.system_prompt})

            headers = {
                'Authorization': f'Bearer {effective_key}',
                'Content-Type': 'application/json',
                'HTTP-Referer': 'https://plexis.ai',
                'X-Title': 'Plexis'
            }

            model_name = self._get_model_for_request(request)

            body = {
                'model': model_name,
                'messages': messages,
                'temperature': request.temperature,
                'max_tokens': request.max_tokens or 4096
            }

            if request.json_mode:
                body['response_format'] = {'type': 'json_object'}

            start_time = time.time()
            response = requests.post(self.endpoint, headers=headers, json=body, timeout=(3.0, 10.0))
            
            # Detect 429
            if response.status_code == 429:
                from providers.quarantine import parse_retry_after
                retry_after = parse_retry_after(response.headers.get("retry-after") or response.headers.get("Retry-After"))
                return ProviderResponse(
                    text='',
                    model=model_name,
                    provider=self.provider_name,
                    success=False,
                    error="HTTP 429: Too Many Requests on OpenRouter",
                    status_code=429,
                    retry_after=retry_after,
                    headers=dict(response.headers),
                    raw_response=response.text[:500] if hasattr(response, 'text') else None
                )
                
            response.raise_for_status()
            latency_ms = (time.time() - start_time) * 1000
            
            data = response.json()
            choices = data.get('choices', [])
            msg = choices[0].get('message', {}) if choices else {}
            raw_content = msg.get('content')
            text = (raw_content or '').strip()
            # Security: NEVER fall back to msg.get('reasoning').
            # Internal model reasoning traces must never be treated as user-visible content.

            usage = data.get('usage', {})
            input_tokens = usage.get('prompt_tokens', 0)
            output_tokens = usage.get('completion_tokens', 0)
            actual_model = data.get('model', model_name)

            success = bool(text)
            err_msg = None if success else f"Empty completion from OpenRouter model {model_name}"

            return ProviderResponse(
                text=text,
                model=actual_model,
                provider=self.provider_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                success=success,
                error=err_msg,
                status_code=200 if success else 502,
                headers=dict(response.headers),
                raw_response=data
            )
        except Exception as e:
            logger.error(f"OpenRouter generate error: {e}")
            status_code = getattr(getattr(e, 'response', None), 'status_code', None)
            resp_headers = dict(getattr(getattr(e, 'response', None), 'headers', {})) if getattr(e, 'response', None) else None
            from providers.quarantine import parse_retry_after
            retry_after = parse_retry_after(resp_headers.get("retry-after") or resp_headers.get("Retry-After")) if resp_headers else None
            return ProviderResponse(
                text='',
                model=model_name if 'model_name' in locals() else 'unknown',
                provider=self.provider_name,
                success=False,
                error=str(e),
                status_code=status_code,
                headers=resp_headers,
                retry_after=retry_after
            )

    def generate_stream(self, request: AIRequest, api_key: Optional[str] = None) -> Generator[str, None, None]:
        """Stream text chunks using OpenRouter API."""
        effective_key = api_key or self._api_key
        try:
            messages = request.messages.copy() if request.messages else []
            if not messages and request.task:
                messages.append({'role': 'user', 'content': request.task})

            if request.system_prompt and not any(m.get('role') == 'system' for m in messages):
                messages.insert(0, {'role': 'system', 'content': request.system_prompt})

            headers = {
                'Authorization': f'Bearer {effective_key}',
                'Content-Type': 'application/json',
                'HTTP-Referer': 'https://plexis.ai',
                'X-Title': 'Plexis'
            }

            model_name = self._get_model_for_request(request)

            body = {
                'model': model_name,
                'messages': messages,
                'temperature': request.temperature,
                'max_tokens': request.max_tokens or 4096,
                'stream': True
            }

            with requests.post(self.endpoint, headers=headers, json=body, stream=True, timeout=60) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line:
                        continue
                    if isinstance(line, bytes):
                        line = line.decode("utf-8")
                    if not line.startswith("data: "):
                        continue
                    payload_str = line[6:].strip()
                    if payload_str == "[DONE]":
                        break
                    try:
                        data = json.loads(payload_str)
                        delta = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                        if delta:
                            yield delta
                    except (KeyError, json.JSONDecodeError):
                        continue
        except Exception as e:
            logger.error(f"OpenRouter generate_stream error: {e}")
            raise e

    def health_check(self) -> bool:
        """Health check verifying credential presence."""
        return bool(self._api_key)
