import time
import json
import logging
from typing import Generator, Optional
import requests

from providers.domain.contracts import IProviderAdapter, ProviderCapabilities, AIRequest
from providers.base import ProviderResponse
from providers.model_registry import model_registry
from config import Config

logger = logging.getLogger(__name__)


class NvidiaProvider(IProviderAdapter):
    """Nvidia NIM provider using requests API."""

    def __init__(self, api_key: str):
        self._api_key = api_key
        self.endpoint = 'https://integrate.api.nvidia.com/v1/chat/completions'

    @property
    def provider_name(self) -> str:
        return 'nvidia'

    def get_capabilities(self, model_name: str) -> ProviderCapabilities:
        return ProviderCapabilities(
            vision=True,
            embeddings=False,
            parallel_tool_calling=False,
            structured_output=True,
            temperature_support=True,
            multimodal=True,
            streaming=True,
            json_mode=True,
            reasoning_level="advanced",
            max_input_tokens=128000,
            max_output_tokens=4096,
            latency_profile="medium",
            cost_profile="low"
        )
        
    def _get_model_for_request(self, request: AIRequest) -> str:
        if hasattr(request, "model_id") and request.model_id:
            return request.model_id

        try:
            task = (request.task or '').lower()
            nvidia_models = model_registry.get_models_for_provider('nvidia')
            if nvidia_models:
                task_models = [
                    m for m in nvidia_models
                    if m.task_affinities.get(task, 0) > 0 and m.health_score > 0.3
                ]
                if task_models:
                    task_models.sort(key=lambda m: (m.priority, -m.task_affinities.get(task, 0)))
                    return task_models[0].model_name
                nvidia_models_sorted = sorted(nvidia_models, key=lambda m: m.priority)
                return nvidia_models_sorted[0].model_name
        except Exception as exc:
            logger.warning('[NvidiaProvider] model_registry lookup failed: %s', exc)

        return Config.ROUTER_NVIDIA_MODEL or "nvidia/nemotron-3.5-lightning-30b-a3b"

    def generate(self, request: AIRequest, api_key: Optional[str] = None) -> ProviderResponse:
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
                'Accept': 'application/json'
            }

            model_name = self._get_model_for_request(request)

            body = {
                'model': model_name,
                'messages': messages,
                'temperature': request.temperature,
                'max_tokens': request.max_tokens or 4096,
                'stream': False
            }

            if request.json_mode:
                body['response_format'] = {'type': 'json_object'}

            start_time = time.time()
            response = requests.post(self.endpoint, headers=headers, json=body, timeout=(3.0, 6.0))
            
            if response.status_code == 429:
                from providers.quarantine import parse_retry_after
                retry_after = parse_retry_after(response.headers.get("retry-after") or response.headers.get("Retry-After"))
                return ProviderResponse(
                    text='',
                    model=model_name,
                    provider=self.provider_name,
                    success=False,
                    error="HTTP 429: Too Many Requests on Nvidia",
                    status_code=429,
                    retry_after=retry_after,
                    headers=dict(response.headers),
                    raw_response=response.text[:500] if hasattr(response, 'text') else None
                )
                
            response.raise_for_status()
            latency_ms = (time.time() - start_time) * 1000
            
            data = response.json()
            message_obj = data.get('choices', [{}])[0].get('message', {})
            raw_content = message_obj.get('content')
            if not raw_content and message_obj.get('reasoning_content'):
                raw_content = message_obj['reasoning_content']
            text = (raw_content or '').strip()
            usage = data.get('usage', {})
            input_tokens = usage.get('prompt_tokens', 0)
            output_tokens = usage.get('completion_tokens', 0)
            actual_model = data.get('model', model_name)

            return ProviderResponse(
                text=text,
                model=actual_model,
                provider=self.provider_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                success=True,
                status_code=200,
                headers=dict(response.headers),
                raw_response=data
            )
        except Exception as e:
            logger.error(f"Nvidia generate error: {e}")
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
                'Accept': 'text/event-stream'
            }

            model_name = self._get_model_for_request(request)

            body = {
                'model': model_name,
                'messages': messages,
                'temperature': request.temperature,
                'max_tokens': request.max_tokens or 4096,
                'stream': True
            }

            with requests.post(self.endpoint, headers=headers, json=body, stream=True, timeout=(3.0, 10.0)) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line:
                        continue
                    if isinstance(line, bytes):
                        line = line.decode('utf-8')
                    if not line.startswith('data: '):
                        continue
                    payload_str = line[6:].strip()
                    if payload_str == '[DONE]':
                        break
                    try:
                        data = json.loads(payload_str)
                        delta = data.get('choices', [{}])[0].get('delta', {}).get('content', '')
                        if delta:
                            yield delta
                    except (KeyError, json.JSONDecodeError):
                        continue
        except Exception as e:
            logger.error(f"Nvidia generate_stream error: {e}")
            raise e

    def health_check(self) -> bool:
        return bool(self._api_key)
