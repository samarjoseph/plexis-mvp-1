import json
import time
import requests
from typing import Optional, Any, Generator
from providers.base import ProviderResponse
from providers.domain.contracts import IProviderAdapter, AIRequest, ProviderCapabilities

import logging
logger = logging.getLogger(__name__)

# HTTP status codes that indicate a permanent model configuration error.
# These should NOT be retried — the model does not exist on this provider.
_PERMANENT_FAILURE_CODES = {404, 422}


class GroqProvider(IProviderAdapter):
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.base_url = "https://api.groq.com/openai/v1/chat/completions"
        self._name = "groq"

    @property
    def provider_name(self) -> str:
        return self._name

    def get_capabilities(self, model_name: str) -> ProviderCapabilities:
        """Return capabilities for the given Groq model."""
        return ProviderCapabilities(
            vision=False,
            embeddings=False,
            parallel_tool_calling=True,
            structured_output=True,
            temperature_support=True,
            multimodal=False,
            streaming=True,
            json_mode=True,
            reasoning_level="none",
            max_input_tokens=128000,
            max_output_tokens=8192,
            latency_profile="low",
            cost_profile="low"
        )

    def _get_model_for_request(self, request: AIRequest) -> str:
        """
        Select the correct model for this request from the ModelRegistry.

        Priority:
          1. model_registry: highest-affinity, highest-health model for task, provider=groq
          2. Config.GROQ_MODEL (env-override default)
          3. Hard fallback: openai/gpt-oss-20b

        NEVER uses request.quality_target to pick between models.
        """
        try:
            from providers.model_registry import model_registry
            task = (request.task or '').lower()
            groq_models = model_registry.get_models_for_provider('groq')
            if groq_models:
                # Prefer task-affinity sorted models (get_models_for_provider already filters health > 0.3)
                task_models = [
                    m for m in groq_models
                    if m.task_affinities.get(task, 0) > 0
                ]
                if task_models:
                    task_models.sort(key=lambda m: (m.priority, -m.task_affinities.get(task, 0)))
                    model = task_models[0].model_name
                    logger.info(
                        '[MODEL_SELECTED] provider=groq task=%s '
                        'selected_model=%s outgoing_model=%s',
                        task, model, model
                    )
                    return model
                # No task-specific model — use highest-priority groq model
                groq_models_sorted = sorted(groq_models, key=lambda m: m.priority)
                return groq_models_sorted[0].model_name
        except Exception as exc:
            logger.warning('[GroqProvider] model_registry lookup failed: %s', exc)

        # Config fallback
        try:
            from config import Config
            if Config.GROQ_MODEL:
                return Config.GROQ_MODEL
        except Exception:
            pass

        # Hard fallback — primary Groq model
        return 'openai/gpt-oss-20b'

    def _is_permanent_error(self, status_code: int) -> bool:
        """Return True for HTTP errors that mean the model does not exist and retrying is pointless."""
        return status_code in _PERMANENT_FAILURE_CODES

    def generate(self, request: AIRequest) -> ProviderResponse:
        start_time = time.time()
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        messages = request.messages.copy() if request.messages else []
        if not messages and request.task:
            messages.append({"role": "user", "content": request.task})

        if request.system_prompt and not any(m.get("role") == "system" for m in messages):
            messages.insert(0, {"role": "system", "content": request.system_prompt})

        # Model from registry — never hardcoded
        model_name = self._get_model_for_request(request)
        logger.info(
            '[MODEL_SELECTED] provider=groq selected_model=%s outgoing_model=%s',
            model_name, model_name,
        )

        payload = {
            "model": model_name,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens or 4096,
        }

        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}
            # Ensure "json" appears in messages as required by OpenAI/Groq API spec
            if not any("json" in m.get("content", "").lower() for m in messages):
                if messages and messages[0].get("role") == "system":
                    messages[0]["content"] += " Output must be a valid JSON object."
                else:
                    messages.insert(0, {"role": "system", "content": "Output must be a valid JSON object."})

        try:
            response = requests.post(self.base_url, headers=headers, json=payload, timeout=15)

            # Fast-fail on permanent errors (404 = model not found, 422 = bad model name)
            if response.status_code in _PERMANENT_FAILURE_CODES:
                logger.error(
                    '[GroqProvider] PERMANENT ERROR %s for model=%s — '
                    'marking model unavailable, will not retry.',
                    response.status_code, model_name
                )
                try:
                    from providers.model_registry import model_registry
                    model_registry.mark_permanently_failed(model_name)
                except Exception:
                    pass
                latency_ms = (time.time() - start_time) * 1000
                return ProviderResponse(
                    text="",
                    model=model_name,
                    provider=self._name,
                    latency_ms=latency_ms,
                    success=False,
                    error=f"HTTP {response.status_code}: model '{model_name}' not available on Groq",
                    status_code=response.status_code,
                    headers=dict(response.headers)
                )

            response.raise_for_status()
            data = response.json()

            latency_ms = (time.time() - start_time) * 1000
            choices = data.get("choices", [])
            msg = choices[0].get("message", {}) if choices else {}
            content = (msg.get("content") or "").strip()
            # Security: NEVER fall back to msg.get("reasoning").
            # Internal model reasoning traces must never be treated as user-visible content.

            usage = data.get("usage", {})
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)

            success = bool(content.strip())
            err_msg = None if success else f"Empty completion from Groq model {model_name}"

            return ProviderResponse(
                text=content,
                model=model_name,
                provider=self._name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                success=success,
                error=err_msg,
                status_code=200 if success else 502,
                raw_response=data
            )
        except Exception as e:
            latency_ms = (time.time() - start_time) * 1000
            logger.error("[GroqProvider] generate() error: %s", e)
            status_code = getattr(getattr(e, 'response', None), 'status_code', None)
            resp_headers = dict(getattr(getattr(e, 'response', None), 'headers', {})) if getattr(e, 'response', None) else None
            from providers.quarantine import parse_retry_after
            retry_after = parse_retry_after(resp_headers.get("retry-after") or resp_headers.get("Retry-After")) if resp_headers else None
            return ProviderResponse(
                text="",
                model=model_name,
                provider=self._name,
                latency_ms=latency_ms,
                success=False,
                error=str(e),
                status_code=status_code,
                headers=resp_headers,
                retry_after=retry_after
            )

    def generate_stream(self, request: AIRequest) -> Generator[str, None, None]:
        """
        Stream text chunks using Groq's OpenAI-compatible streaming API.
        Model is selected from model_registry — never hardcoded.

        Diagnostics logged:
          - HTTP status code
          - Total SSE lines received
          - Number of data: lines
          - Number of non-empty content chunks
          - finish_reason from the final choice

        If model returns HTTP 200 but yields zero content chunks (common for some
        model/API combinations), automatically falls back to non-streaming generate().
        """
        messages = request.messages.copy() if request.messages else []
        if not messages and request.task:
            messages.append({"role": "user", "content": request.task})

        if request.system_prompt and not any(m.get("role") == "system" for m in messages):
            messages.insert(0, {"role": "system", "content": request.system_prompt})

        # Model from registry — never hardcoded
        model_name = self._get_model_for_request(request)
        logger.info(
            '[MODEL_SELECTED] provider=groq selected_model=%s outgoing_model=%s',
            model_name, model_name,
        )

        payload = {
            "model": model_name,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens or 4096,
            "stream": True,
        }

        try:
            with requests.post(
                self.base_url,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=payload,
                stream=True,
                timeout=60,
            ) as r:
                if r.status_code in _PERMANENT_FAILURE_CODES:
                    logger.error(
                        '[GroqProvider] PERMANENT ERROR %s for model=%s — marking unavailable.',
                        r.status_code, model_name
                    )
                    try:
                        from providers.model_registry import model_registry
                        model_registry.mark_permanently_failed(model_name)
                    except Exception:
                        pass
                    raise RuntimeError(
                        f"HTTP {r.status_code}: model '{model_name}' not available on Groq"
                    )
                if r.status_code >= 400:
                    logger.error("[GroqProvider] HTTP ERROR: %s - %s", r.status_code, r.text[:500])
                r.raise_for_status()

                total_lines = 0
                data_lines = 0
                content_chunks = 0
                finish_reason = None

                for line in r.iter_lines():
                    total_lines += 1
                    if not line:
                        continue
                    if isinstance(line, bytes):
                        line = line.decode("utf-8")
                    if not line.startswith("data: "):
                        continue
                    data_lines += 1
                    payload_str = line[6:].strip()
                    if payload_str == "[DONE]":
                        break
                    try:
                        data = json.loads(payload_str)
                        choice = data["choices"][0]
                        delta = choice.get("delta", {}).get("content", "")
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
                        if delta:
                            content_chunks += 1
                            yield delta
                    except (KeyError, json.JSONDecodeError):
                        continue

                logger.info(
                    "[GroqProvider] stream_complete model=%s http_status=%s "
                    "total_lines=%d data_lines=%d content_chunks=%d finish_reason=%s",
                    model_name, r.status_code,
                    total_lines, data_lines, content_chunks, finish_reason,
                )

                # If HTTP 200 but zero content chunks, the model may not support
                # streaming in this configuration. Fall back to non-streaming.
                if content_chunks == 0:
                    logger.warning(
                        "[GroqProvider] generate_stream yielded 0 chunks for model=%s "
                        "(http_status=200, data_lines=%d). "
                        "Falling back to non-streaming generate().",
                        model_name, data_lines,
                    )
                    # Build a non-streaming request with same params
                    ns_payload = {
                        "model": model_name,
                        "messages": messages,
                        "temperature": request.temperature,
                        "max_tokens": request.max_tokens or 4096,
                    }
                    ns_resp = requests.post(
                        self.base_url,
                        headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                        json=ns_payload,
                        timeout=60,
                    )
                    if ns_resp.status_code == 200:
                        ns_data = ns_resp.json()
                        text = ns_data["choices"][0]["message"]["content"]
                        if text:
                            logger.info(
                                "[GroqProvider] non-streaming fallback succeeded model=%s chars=%d",
                                model_name, len(text),
                            )
                            yield text
                        else:
                            logger.warning(
                                "[GroqProvider] non-streaming fallback also yielded empty content model=%s",
                                model_name,
                            )
                    else:
                        logger.error(
                            "[GroqProvider] non-streaming fallback failed model=%s status=%s",
                            model_name, ns_resp.status_code,
                        )

        except Exception as e:
            logger.error("[GroqProvider] generate_stream error: %s", e)
            raise e
