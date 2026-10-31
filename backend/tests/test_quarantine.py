"""
Tests for Global Provider Quarantine and Circuit Breakers.

Verifies:
- Failure classification & category-specific cooldowns
- Provider exclusion from subsequent requests (10-15 calls test)
- Concurrency safety and probe slot race prevention
- Quota scope isolation across shared accounts
- Independent provider health continuity
- Half-open probe success and failure transitions
- Retry-After header and quota-reset parsing
- Safe degradation on state store errors
- End-to-end fallback with structured response validation
"""
import sys
import os
import time
import threading
from typing import List, Dict, Any, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from providers.base import ProviderResponse, BaseProvider
from providers.domain.contracts import AIRequest, ProviderCapabilities
from providers.quarantine import (
    QuarantineManager,
    InMemoryStateStore,
    HealthStateStore,
    CircuitState,
    FailureCategory,
    CooldownPolicy,
    ScopeKey,
    classify_error,
    parse_retry_after,
)
from providers.registry import ProviderRegistry
from providers.model_registry import ModelRegistry, ModelMetadata
from providers.selector import ModelSelector
from providers.fallback import FallbackEngine
from providers.circuit_breaker import CircuitBreaker
from providers.health import HealthMonitor


# ── Controllable Clock ─────────────────────────────────────────────────────────

class ControllableClock:
    def __init__(self, start_time: float = 1000.0):
        self.current_time = start_time

    def time(self) -> float:
        return self.current_time

    def advance(self, seconds: float) -> None:
        self.current_time += seconds


# ── Mock Provider ──────────────────────────────────────────────────────────────

class MockProvider(BaseProvider):
    def __init__(self, name: str, fail_error: Optional[str] = None, status_code: Optional[int] = None, retry_after: Optional[float] = None):
        self._name = name
        self.call_count = 0
        self.fail_error = fail_error
        self.status_code = status_code
        self.retry_after = retry_after
        self.responses: List[ProviderResponse] = []

    @property
    def provider_name(self) -> str:
        return self._name

    def generate(self, request: AIRequest) -> ProviderResponse:
        self.call_count += 1
        if self.fail_error:
            headers = {"retry-after": str(self.retry_after)} if self.retry_after else {}
            return ProviderResponse(
                text="",
                model=f"{self._name}-model",
                provider=self._name,
                success=False,
                error=self.fail_error,
                status_code=self.status_code,
                retry_after=self.retry_after,
                headers=headers,
            )
        return ProviderResponse(
            text='{"result": "success"}',
            model=f"{self._name}-model",
            provider=self._name,
            success=True,
            latency_ms=10.0,
        )

    def generate_json(self, *args, **kwargs) -> ProviderResponse:
        return self.generate(AIRequest(task="test", json_mode=True))

    def health_check(self) -> bool:
        return True


# ══════════════════════════════════════════════════════════════════════════════
# 1. Error Classification & Retry-After Parsing Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestClassificationAndParsing:
    def test_parse_retry_after_numeric(self):
        assert parse_retry_after("45") == 45.0
        assert parse_retry_after(30) == 30.0
        assert parse_retry_after("0") == 0.0

    def test_parse_retry_after_text_duration(self):
        assert parse_retry_after("Retry in 7h17m45s") == (7 * 3600) + (17 * 60) + 45
        assert parse_retry_after("Please wait 30s before retrying") == 30.0

    def test_classify_rate_limit(self):
        cat, retry_after = classify_error("HTTP 429: Too Many Requests on Nvidia", status_code=429, headers={"retry-after": "60"})
        assert cat == FailureCategory.RATE_LIMIT
        assert retry_after == 60.0

    def test_classify_quota_exhausted(self):
        cat, retry_after = classify_error("Resource has been exhausted (e.g. check quota)", status_code=429)
        assert cat == FailureCategory.QUOTA_EXHAUSTED

    def test_classify_server_error(self):
        cat, _ = classify_error("503 Service Unavailable", status_code=503)
        assert cat == FailureCategory.SERVER_ERROR

    def test_classify_timeout(self):
        cat, _ = classify_error("HTTPSConnectionPool: Read timed out")
        assert cat == FailureCategory.TIMEOUT

    def test_classify_auth_failure(self):
        cat, _ = classify_error("401 Unauthorized: Invalid API key", status_code=401)
        assert cat == FailureCategory.AUTH_FAILURE


# ══════════════════════════════════════════════════════════════════════════════
# 2. Provider Exclusion & Subsequent 10-15 Requests Test
# ══════════════════════════════════════════════════════════════════════════════

class TestQuarantineExclusion:
    def test_quarantined_provider_excluded_from_subsequent_15_requests(self):
        clock = ControllableClock(1000.0)
        qm = QuarantineManager(clock=clock.time)

        provider = "primary_llm"
        api_key = "secret_key_123"

        assert qm.is_eligible(provider, api_key=api_key) is True

        # Provider fails with 429 Rate Limit
        state, cooldown_until = qm.record_failure(
            provider=provider,
            error="HTTP 429: Too Many Requests",
            api_key=api_key,
            status_code=429,
        )
        assert state == CircuitState.OPEN
        assert cooldown_until > clock.time()

        # Run 15 subsequent requests — every single one must be blocked!
        for i in range(15):
            assert qm.is_eligible(provider, api_key=api_key) is False, f"Request {i+1} unexpectedly bypassed quarantine!"

        # An independent provider must remain completely eligible
        assert qm.is_eligible("secondary_llm", api_key="other_key") is True


# ══════════════════════════════════════════════════════════════════════════════
# 3. Concurrency Safety: Multiple Threads Cannot Bypass OPEN Circuit
# ══════════════════════════════════════════════════════════════════════════════

class TestConcurrencySafety:
    def test_concurrent_requests_cannot_bypass_open_circuit(self):
        clock = ControllableClock(1000.0)
        qm = QuarantineManager(clock=clock.time)

        provider = "threaded_provider"
        api_key = "thread_key"

        # Trip circuit
        qm.record_failure(provider, error="429 Rate limit", api_key=api_key, status_code=429)
        assert qm.is_eligible(provider, api_key=api_key) is False

        results = []
        threads = []

        def worker():
            for _ in range(50):
                eligible = qm.is_eligible(provider, api_key=api_key)
                results.append(eligible)

        for _ in range(10):
            t = threading.Thread(target=worker)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # All 500 concurrent checks must be False
        assert len(results) == 500
        assert all(res is False for res in results)

    def test_concurrent_requests_bounded_to_single_probe_in_half_open(self):
        clock = ControllableClock(1000.0)
        qm = QuarantineManager(clock=clock.time)

        provider = "probe_test_provider"
        api_key = "probe_key"

        # Trip circuit with 10s cooldown
        qm.record_failure(provider, error="503 Service Unavailable", api_key=api_key, status_code=503)
        qm.record_failure(provider, error="503 Service Unavailable", api_key=api_key, status_code=503)

        # Advance clock past cooldown
        clock.advance(100.0)

        # 20 concurrent threads attempt to acquire the probe slot
        acquired_slots = []
        threads = []

        def probe_worker():
            if qm.is_eligible(provider, api_key=api_key):
                got_slot = qm.acquire_probe_slot(provider, api_key=api_key)
                acquired_slots.append(got_slot)
            else:
                acquired_slots.append(False)

        for _ in range(20):
            t = threading.Thread(target=probe_worker)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # EXACTLY ONE thread must have acquired the probe slot!
        assert acquired_slots.count(True) == 1
        assert acquired_slots.count(False) == 19


# ══════════════════════════════════════════════════════════════════════════════
# 4. Shared Quota Scope: Models on Same Account Quota are Quarantined Together
# ══════════════════════════════════════════════════════════════════════════════

class TestSharedQuotaScope:
    def test_exhausted_account_quota_quarantines_all_models_on_that_account(self):
        clock = ControllableClock(1000.0)
        qm = QuarantineManager(clock=clock.time)

        provider = "nvidia"
        api_key = "nvidia_shared_api_key"
        model_a = "google/diffusiongemma-26b-a4b-it"
        model_b = "google/gemma-4-31b-it"

        # Both models initially healthy
        assert qm.is_eligible(provider, api_key=api_key, model_id=model_a) is True
        assert qm.is_eligible(provider, api_key=api_key, model_id=model_b) is True

        # Model A fails with account-level 429 quota exhaustion
        qm.record_failure(
            provider=provider,
            error="HTTP 429: Too Many Requests on Nvidia",
            api_key=api_key,
            model_id=model_a,
            status_code=429,
        )

        # Both Model A AND Model B must now be quarantined because they share the same account quota!
        assert qm.is_eligible(provider, api_key=api_key, model_id=model_a) is False
        assert qm.is_eligible(provider, api_key=api_key, model_id=model_b) is False, (
            "Model B was falsely treated as independent failover under the same exhausted account quota!"
        )

        # An independent provider with a different account remains eligible
        assert qm.is_eligible("gemini", api_key="gemini_key", model_id="gemini-3.5-flash") is True


# ══════════════════════════════════════════════════════════════════════════════
# 5. Recovery & Probe Dynamics: Probe Success and Failure
# ══════════════════════════════════════════════════════════════════════════════

class TestProbeTransitions:
    def test_successful_half_open_probe_restores_eligibility(self):
        clock = ControllableClock(1000.0)
        qm = QuarantineManager(clock=clock.time)

        provider = "recovering_provider"
        api_key = "rec_key"

        # Trip circuit
        qm.record_failure(provider, error="429 Rate limit", api_key=api_key, status_code=429)
        assert qm.is_eligible(provider, api_key=api_key) is False

        # Advance past cooldown
        clock.advance(50.0)
        assert qm.is_eligible(provider, api_key=api_key) is True
        assert qm.acquire_probe_slot(provider, api_key=api_key) is True

        # Probe succeeds
        qm.record_success(provider, api_key=api_key)

        # Circuit is now CLOSED and normal calls proceed freely
        assert qm.is_eligible(provider, api_key=api_key) is True
        metrics = qm.get_metrics()
        assert metrics["probes_succeeded"] == 1
        assert metrics["transitions_to_closed"] == 1

    def test_failed_probe_extends_quarantine(self):
        clock = ControllableClock(1000.0)
        qm = QuarantineManager(clock=clock.time)

        provider = "relapsing_provider"
        api_key = "relapse_key"

        # Trip circuit
        qm.record_failure(provider, error="429 Rate limit", api_key=api_key, status_code=429)

        # Advance past cooldown
        clock.advance(50.0)
        assert qm.acquire_probe_slot(provider, api_key=api_key) is True

        # Probe fails
        state, new_cooldown = qm.record_failure(provider, error="Still 429", api_key=api_key, status_code=429)

        assert state == CircuitState.OPEN
        assert new_cooldown > clock.time()

        # Provider must be quarantined again!
        assert qm.is_eligible(provider, api_key=api_key) is False
        metrics = qm.get_metrics()
        assert metrics["probes_failed"] == 1


# ══════════════════════════════════════════════════════════════════════════════
# 6. Retry-After and Quota Reset Times
# ══════════════════════════════════════════════════════════════════════════════

class TestRetryAfterAdherence:
    def test_retry_after_header_respected(self):
        clock = ControllableClock(1000.0)
        qm = QuarantineManager(clock=clock.time)

        provider = "strict_rate_limit_provider"
        api_key = "strict_key"

        # 429 with explicit Retry-After: 90 seconds
        state, cd_until = qm.record_failure(
            provider=provider,
            error="429 Rate limit",
            api_key=api_key,
            status_code=429,
            headers={"retry-after": "90"}
        )

        assert state == CircuitState.OPEN
        # Exactly 90 seconds from current clock
        assert pytest.approx(cd_until, abs=0.5) == 1090.0

        clock.advance(80.0)
        assert qm.is_eligible(provider, api_key=api_key) is False

        clock.advance(15.0)  # Total 95s > 90s
        assert qm.is_eligible(provider, api_key=api_key) is True


# ══════════════════════════════════════════════════════════════════════════════
# 7. Safe Degradation on Health Store Failure
# ══════════════════════════════════════════════════════════════════════════════

class FailingStateStore(HealthStateStore):
    """Simulates a broken Redis / DB persistence layer."""
    def get(self, key_str: str):
        raise ConnectionError("Redis connection lost")

    def set(self, key_str: str, data: Dict[str, Any]):
        raise ConnectionError("Redis connection lost")

    def get_all(self):
        raise ConnectionError("Redis connection lost")


class TestSafeDegradation:
    def test_store_failure_does_not_crash_or_cause_retry_storm(self):
        clock = ControllableClock(1000.0)
        failing_store = FailingStateStore()
        qm = QuarantineManager(store=failing_store, clock=clock.time)

        provider = "resilient_provider"
        api_key = "res_key"

        # Recording failure when store fails should degrade to local cache gracefully
        state, cd = qm.record_failure(provider, error="500 Internal Error", api_key=api_key, status_code=500)
        state, cd = qm.record_failure(provider, error="500 Internal Error", api_key=api_key, status_code=500)
        assert state == CircuitState.OPEN

        # Should NOT treat provider as healthy just because the store crashed!
        assert qm.is_eligible(provider, api_key=api_key) is False


# ══════════════════════════════════════════════════════════════════════════════
# 8. Cross-Subsystem Unified State via CircuitBreaker & HealthMonitor Delegates
# ══════════════════════════════════════════════════════════════════════════════

class TestCrossSubsystemSharing:
    def test_circuit_breaker_and_health_monitor_share_identical_state(self):
        from providers.quarantine import quarantine_manager
        quarantine_manager.reset_all()

        cb = CircuitBreaker()
        hm = HealthMonitor()

        provider = "shared_provider"
        from providers.registry import provider_registry
        provider_registry._api_keys[provider] = "shared_key"

        assert cb.can_call(provider) is True
        assert hm.is_healthy(provider) is True

        # Failure recorded via CircuitBreaker
        cb.record_failure(provider, error="429 Rate limit")

        # HealthMonitor and QuarantineManager immediately reflect the quarantine!
        assert cb.can_call(provider) is False
        assert hm.is_healthy(provider) is False
        assert quarantine_manager.is_eligible(provider, api_key="shared_key") is False

        # Success via HealthMonitor restores it
        hm.record_success(provider)
        assert cb.can_call(provider) is True
        assert hm.is_healthy(provider) is True
        assert quarantine_manager.is_eligible(provider, api_key="shared_key") is True


# ══════════════════════════════════════════════════════════════════════════════
# 9. End-to-End Fallback Engine with Contract Preservation
# ══════════════════════════════════════════════════════════════════════════════

class TestEndToEndFallback:
    def test_fallback_switches_provider_and_preserves_contract(self):
        from providers.quarantine import quarantine_manager
        quarantine_manager.reset_all()

        from providers.registry import provider_registry
        from providers.model_registry import model_registry
        from providers.fallback import fallback_engine

        saved_providers = dict(provider_registry._providers)
        saved_keys = dict(provider_registry._api_keys)
        saved_models = dict(model_registry._models)

        try:
            provider_registry._providers.clear()
            provider_registry._api_keys.clear()
            model_registry._models.clear()

            # Setup mock failing primary provider and working secondary provider
            mock_fail = MockProvider("mock_primary", fail_error="HTTP 429: Too Many Requests on Nvidia", status_code=429)
            mock_succ = MockProvider("mock_secondary")

            provider_registry.register(mock_fail, "key_primary")
            provider_registry.register(mock_succ, "key_secondary")

            model_registry.register(ModelMetadata(
                model_id="mock_primary/model-1",
                model_name="mock_primary/model-1",
                provider="mock_primary",
                priority=0,
                task_affinities={"analysis": 1.0, "fallback": 1.0},
                context_window=4096, max_output_tokens=1024,
                supports_vision=False, supports_json_mode=True, supports_tool_calling=False,
                supports_streaming=False, supports_reasoning=False,
            ))

            model_registry.register(ModelMetadata(
                model_id="mock_secondary/model-2",
                model_name="mock_secondary/model-2",
                provider="mock_secondary",
                priority=1,
                task_affinities={"analysis": 1.0, "fallback": 1.0},
                context_window=4096, max_output_tokens=1024,
                supports_vision=False, supports_json_mode=True, supports_tool_calling=False,
                supports_streaming=False, supports_reasoning=False,
            ))

            request = AIRequest(task="analysis", json_mode=True)

            # Execute fallback
            resp = fallback_engine.execute(request)

            # Primary failed and was quarantined
            assert mock_fail.call_count == 1
            assert quarantine_manager.is_eligible("mock_primary", api_key="key_primary") is False

            # Fallback to secondary succeeded
            assert mock_succ.call_count == 1
            assert resp.success is True
            assert resp.provider == "mock_secondary"
            assert resp.text == '{"result": "success"}'

            # Next request must bypass primary entirely and call secondary directly!
            resp2 = fallback_engine.execute(request)
            assert mock_fail.call_count == 1  # Was NOT called!
            assert mock_succ.call_count == 2  # Handled directly!
        finally:
            provider_registry._providers = saved_providers
            provider_registry._api_keys = saved_keys
            model_registry._models = saved_models

    def test_all_providers_unavailable_returns_explicit_recoverable_error(self):
        from providers.quarantine import quarantine_manager
        quarantine_manager.reset_all()

        from providers.registry import provider_registry
        from providers.model_registry import model_registry
        from providers.fallback import fallback_engine

        saved_providers = dict(provider_registry._providers)
        saved_keys = dict(provider_registry._api_keys)
        saved_models = dict(model_registry._models)

        try:
            provider_registry._providers.clear()
            provider_registry._api_keys.clear()
            model_registry._models.clear()

            # Both fail with 429
            mock_fail1 = MockProvider("p1", fail_error="429 Rate limit", status_code=429)
            mock_fail2 = MockProvider("p2", fail_error="429 Rate limit", status_code=429)

            provider_registry.register(mock_fail1, "k1")
            provider_registry.register(mock_fail2, "k2")

            model_registry.register(ModelMetadata(
                model_id="p1/m1", model_name="p1/m1", provider="p1", priority=0,
                task_affinities={"analysis": 1.0, "fallback": 1.0}, context_window=4096, max_output_tokens=1024,
                supports_vision=False, supports_json_mode=True, supports_tool_calling=False,
                supports_streaming=False, supports_reasoning=False,
            ))
            model_registry.register(ModelMetadata(
                model_id="p2/m2", model_name="p2/m2", provider="p2", priority=1,
                task_affinities={"analysis": 1.0, "fallback": 1.0}, context_window=4096, max_output_tokens=1024,
                supports_vision=False, supports_json_mode=True, supports_tool_calling=False,
                supports_streaming=False, supports_reasoning=False,
            ))

            request = AIRequest(task="analysis", json_mode=True)
            resp = fallback_engine.execute(request)

            # Must return explicit failure with raw_response indicating exhaustion
            assert resp.success is False
            assert resp.raw_response.get("error_type") == "all_providers_unavailable"
            assert resp.raw_response.get("retryable") is True
        finally:
            provider_registry._providers = saved_providers
            provider_registry._api_keys = saved_keys
            model_registry._models = saved_models

    def test_secret_safety_in_metrics_and_status(self):
        clock = ControllableClock(1000.0)
        qm = QuarantineManager(clock=clock.time)

        secret_api_key = "super_secret_sk-live-999888777666"
        qm.record_failure("test_provider", error="429 Too Many", api_key=secret_api_key, status_code=429)

        status_str = str(qm.get_status())
        metrics_str = str(qm.get_metrics())

        # Secret API key must NEVER appear anywhere in status or metrics
        assert secret_api_key not in status_str
        assert secret_api_key not in metrics_str
        assert "super_secret" not in status_str
        assert "999888" not in status_str
