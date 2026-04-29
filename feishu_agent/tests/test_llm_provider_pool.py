"""Unit tests for llm_provider_pool.

Coverage:
- Happy path: first provider succeeds on first try.
- Transient error: retries the same provider with backoff.
- Retries exhausted: falls over to next provider.
- All providers fail: AllProvidersExhaustedError carries attempt trail.
- Non-retryable 4xx: skip retries on current provider, try next.
- Observer is invoked once per attempt.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from feishu_agent.providers.llm_provider_pool import (
    AllProvidersExhaustedError,
    AttemptRecord,
    LlmProviderConfig,
    LlmProviderPool,
    PoolRunSummary,
)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Short-circuit backoff sleeps.

    Production validation requires ``base_backoff_seconds >= 0.1`` to
    prevent retry storms, so we can't pass zero. Patching the module's
    ``asyncio.sleep`` reference to a no-op keeps tests snappy without
    weakening production guardrails.
    """
    async def _noop(_delay):  # type: ignore[no-untyped-def]
        return None

    import feishu_agent.providers.llm_provider_pool as mod

    monkeypatch.setattr(mod.asyncio, "sleep", _noop)


def _cfg(name: str) -> LlmProviderConfig:
    return LlmProviderConfig(
        name=name,
        base_url=f"https://{name}.example.com/v1",
        api_key=f"key-{name}",
        model="test-model",
    )


def _make_response(status: int) -> httpx.Response:
    req = httpx.Request("POST", "https://x/chat/completions")
    return httpx.Response(status_code=status, request=req, json={"ok": status})


@pytest.mark.asyncio
async def test_first_provider_first_try_success():
    pool = LlmProviderPool(providers=[_cfg("a"), _cfg("b")])

    async def send(provider, client):
        return {"provider": provider.name}

    result, summary = await pool.execute_with_failover(
        send=send,
        client_factory=lambda p: MagicMock(spec=httpx.AsyncClient),
    )
    assert result["provider"] == "a"
    assert len(summary.attempts) == 1
    assert summary.attempts[0].succeeded is True
    assert summary.retries_used() == 0
    assert summary.providers_tried() == ["a"]


@pytest.mark.asyncio
async def test_transient_retries_same_provider_then_succeeds():
    pool = LlmProviderPool(
        providers=[_cfg("a")],
        max_retries_per_provider=2,
        base_backoff_seconds=0.1,  # no sleep in tests
        jitter_seconds=0,
    )

    call_count = {"n": 0}

    async def send(provider, client):
        call_count["n"] += 1
        if call_count["n"] <= 2:
            resp = _make_response(503)
            raise httpx.HTTPStatusError("upstream down", request=resp.request, response=resp)
        return {"ok": True}

    result, summary = await pool.execute_with_failover(
        send=send, client_factory=lambda p: MagicMock(spec=httpx.AsyncClient)
    )
    assert result == {"ok": True}
    assert len(summary.attempts) == 3
    assert summary.retries_used() == 2
    assert summary.providers_tried() == ["a"]


@pytest.mark.asyncio
async def test_failover_to_next_provider_when_retries_exhausted():
    pool = LlmProviderPool(
        providers=[_cfg("a"), _cfg("b")],
        max_retries_per_provider=1,
        base_backoff_seconds=0.1,
        jitter_seconds=0,
    )

    async def send(provider, client):
        if provider.name == "a":
            resp = _make_response(500)
            raise httpx.HTTPStatusError("broken", request=resp.request, response=resp)
        return {"won_by": provider.name}

    result, summary = await pool.execute_with_failover(
        send=send, client_factory=lambda p: MagicMock(spec=httpx.AsyncClient)
    )
    assert result["won_by"] == "b"
    assert summary.providers_tried() == ["a", "b"]
    # retries_used counts tries on the winning provider minus 1.
    assert summary.retries_used() == 0


@pytest.mark.asyncio
async def test_all_providers_fail_raises_with_trail():
    pool = LlmProviderPool(
        providers=[_cfg("a"), _cfg("b")],
        max_retries_per_provider=1,
        base_backoff_seconds=0.1,
        jitter_seconds=0,
    )

    async def send(provider, client):
        resp = _make_response(503)
        raise httpx.HTTPStatusError("down", request=resp.request, response=resp)

    with pytest.raises(AllProvidersExhaustedError) as exc_info:
        await pool.execute_with_failover(
            send=send, client_factory=lambda p: MagicMock(spec=httpx.AsyncClient)
        )
    exc = exc_info.value
    # 2 providers × 2 tries each = 4 attempts
    assert len(exc.summary.attempts) == 4
    assert exc.summary.providers_tried() == ["a", "b"]
    assert isinstance(exc.last_error, httpx.HTTPStatusError)


@pytest.mark.asyncio
async def test_non_retryable_4xx_skips_retries_but_tries_next_provider():
    pool = LlmProviderPool(
        providers=[_cfg("a"), _cfg("b")],
        max_retries_per_provider=5,  # would be many retries if they happened
        base_backoff_seconds=0.1,
        jitter_seconds=0,
    )

    calls: list[str] = []

    async def send(provider, client):
        calls.append(provider.name)
        if provider.name == "a":
            resp = _make_response(401)  # non-retryable
            raise httpx.HTTPStatusError("auth", request=resp.request, response=resp)
        return {"won_by": "b"}

    result, summary = await pool.execute_with_failover(
        send=send, client_factory=lambda p: MagicMock(spec=httpx.AsyncClient)
    )
    assert result["won_by"] == "b"
    # Only ONE hit on provider a (no retries on non-retryable), then b.
    assert calls == ["a", "b"]
    assert len(summary.attempts) == 2


@pytest.mark.asyncio
async def test_observer_invoked_for_each_attempt():
    observed: list = []
    pool = LlmProviderPool(
        providers=[_cfg("a")],
        max_retries_per_provider=1,
        base_backoff_seconds=0.1,
        jitter_seconds=0,
        on_attempt=observed.append,
    )

    call_count = {"n": 0}

    async def send(provider, client):
        call_count["n"] += 1
        if call_count["n"] == 1:
            resp = _make_response(429)
            raise httpx.HTTPStatusError("rate", request=resp.request, response=resp)
        return {"ok": True}

    await pool.execute_with_failover(
        send=send, client_factory=lambda p: MagicMock(spec=httpx.AsyncClient)
    )
    assert len(observed) == 2
    assert observed[0].succeeded is False and observed[0].http_status == 429
    assert observed[1].succeeded is True


@pytest.mark.asyncio
async def test_timeout_is_retried():
    pool = LlmProviderPool(
        providers=[_cfg("a")],
        max_retries_per_provider=1,
        base_backoff_seconds=0.1,
        jitter_seconds=0,
    )

    call_count = {"n": 0}

    async def send(provider, client):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise httpx.TimeoutException("timeout")
        return {"ok": True}

    result, summary = await pool.execute_with_failover(
        send=send, client_factory=lambda p: MagicMock(spec=httpx.AsyncClient)
    )
    assert result == {"ok": True}
    assert summary.retries_used() == 1


def test_constructor_rejects_empty_pool():
    with pytest.raises(ValueError):
        LlmProviderPool(providers=[])


def test_constructor_rejects_near_zero_backoff():
    """S-4: zero / near-zero base_backoff produces a retry storm on
    rate-limited providers. Production must reject the config at
    construction time rather than burn the bridge at runtime."""
    with pytest.raises(ValueError, match="retry storm"):
        LlmProviderPool(providers=[_cfg("a")], base_backoff_seconds=0)
    with pytest.raises(ValueError, match="retry storm"):
        LlmProviderPool(providers=[_cfg("a")], base_backoff_seconds=0.05)


def test_constructor_rejects_inverted_backoff_bounds():
    """``max`` smaller than ``base`` is nonsense — the backoff window
    would be upper-bounded below the first retry's delay."""
    with pytest.raises(ValueError, match="must be >= base"):
        LlmProviderPool(
            providers=[_cfg("a")],
            base_backoff_seconds=1.0,
            max_backoff_seconds=0.5,
        )


# ---------------------------------------------------------------------------
# S-5: classified error messages
# ---------------------------------------------------------------------------


def _exhausted(attempts: list[AttemptRecord], last: BaseException | None = None) -> AllProvidersExhaustedError:
    summary = PoolRunSummary(attempts=attempts)
    return AllProvidersExhaustedError(summary, last)


def _att(name: str, status: int | None, error_class: str = "HTTPStatusError") -> AttemptRecord:
    return AttemptRecord(
        provider_name=name,
        attempt_index=0,
        error_class=error_class,
        http_status=status,
        latency_ms=1,
        succeeded=False,
    )


def test_exhausted_classify_rate_limited_when_all_429():
    exc = _exhausted([_att("a", 429), _att("a", 429), _att("b", 429)])
    assert exc.classify() == "rate_limited"
    assert "限流" in exc.user_message()


def test_exhausted_classify_auth_failed_on_401_and_403():
    exc = _exhausted([_att("a", 401), _att("b", 403)])
    assert exc.classify() == "auth_failed"
    assert "鉴权" in exc.user_message() or "API key" in exc.user_message()


def test_exhausted_classify_upstream_down_on_5xx():
    exc = _exhausted([_att("a", 503), _att("b", 500)])
    assert exc.classify() == "upstream_down"
    assert "上游" in exc.user_message() or "事故" in exc.user_message()


def test_exhausted_classify_timeout_on_no_status_timeout_errors():
    exc = _exhausted(
        [
            _att("a", None, error_class="TimeoutException"),
            _att("b", None, error_class="ConnectTimeout"),
        ]
    )
    assert exc.classify() == "timeout"
    assert "超时" in exc.user_message()


def test_exhausted_classify_mixed_fallback():
    exc = _exhausted([_att("a", 429), _att("b", 503)])
    assert exc.classify() == "mixed"
    # Still surfaces attempt count and providers list
    msg = exc.user_message()
    assert "2 次" in msg
    assert "'a'" in msg and "'b'" in msg


# ---------------------------------------------------------------------------
# Bedrock transport: pool recognizes anthropic SDK errors + skips httpx client
# ---------------------------------------------------------------------------


class _FakeAnthropicTimeout(Exception):
    """Mimic ``anthropic.APITimeoutError`` for classification tests.

    We don't want the test suite to depend on the real SDK being
    importable (many CI boxes skip optional extras). The pool's
    ``_is_transient`` duck-types on ``__module__.startswith("anthropic")``
    + class name, so we force both via ``__module__``.
    """

    __module__ = "anthropic"  # override default module


_FakeAnthropicTimeout.__name__ = "APITimeoutError"


class _FakeAnthropicStatusError(Exception):
    __module__ = "anthropic"

    def __init__(self, status_code: int, message: str = "bedrock error") -> None:
        super().__init__(message)
        self.status_code = status_code


_FakeAnthropicStatusError.__name__ = "APIStatusError"


def test_is_transient_recognizes_anthropic_timeout():
    retryable, status = LlmProviderPool._is_transient(_FakeAnthropicTimeout())
    assert retryable is True
    assert status is None


def test_is_transient_recognizes_anthropic_429_as_retryable():
    retryable, status = LlmProviderPool._is_transient(
        _FakeAnthropicStatusError(status_code=429)
    )
    assert retryable is True
    assert status == 429


def test_is_transient_recognizes_anthropic_400_as_non_retryable():
    retryable, status = LlmProviderPool._is_transient(
        _FakeAnthropicStatusError(status_code=400)
    )
    assert retryable is False
    assert status == 400


def _bedrock_cfg(name: str) -> LlmProviderConfig:
    return LlmProviderConfig(
        name=name,
        base_url="",
        api_key="",
        model="arn:aws:bedrock:us-west-2:0:application-inference-profile/test",
        transport="anthropic_bedrock",
        aws_region="us-west-2",
        aws_access_key_id="k",
        aws_secret_access_key="s",
    )


@pytest.mark.asyncio
async def test_pool_passes_none_client_for_bedrock_transport():
    """Bedrock providers own their SDK client; pool must not pass a
    pre-built httpx client they'd ignore."""
    pool = LlmProviderPool(
        providers=[_bedrock_cfg("bedrock")],
        max_retries_per_provider=0,
        base_backoff_seconds=0.1,
    )
    received_client: list = []

    async def send(provider, client):
        received_client.append(client)
        return {"ok": True}

    result, _ = await pool.execute_with_failover(send=send)
    assert result == {"ok": True}
    assert received_client == [None]


@pytest.mark.asyncio
async def test_pool_retries_bedrock_on_anthropic_timeout_then_succeeds():
    pool = LlmProviderPool(
        providers=[_bedrock_cfg("bedrock")],
        max_retries_per_provider=2,
        base_backoff_seconds=0.1,
        jitter_seconds=0,
    )
    call_count = {"n": 0}

    async def send(provider, client):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise _FakeAnthropicTimeout()
        return {"bedrock_ok": True}

    result, summary = await pool.execute_with_failover(send=send)
    assert result == {"bedrock_ok": True}
    assert summary.retries_used() == 1
    assert summary.providers_tried() == ["bedrock"]


@pytest.mark.asyncio
async def test_pool_fails_over_from_openai_http_to_bedrock():
    """End-to-end pool behavior: primary (openai_http) exhausts retries
    on 503, pool falls over to secondary (bedrock) which succeeds.
    Validates the mixed-transport scenario that production uses."""
    pool = LlmProviderPool(
        providers=[_cfg("primary"), _bedrock_cfg("bedrock")],
        max_retries_per_provider=1,
        base_backoff_seconds=0.1,
        jitter_seconds=0,
    )

    async def send(provider, client):
        if provider.name == "primary":
            resp = _make_response(503)
            raise httpx.HTTPStatusError(
                "upstream down", request=resp.request, response=resp
            )
        # Bedrock path: client is None, we're imitating a real SDK call
        assert client is None
        return {"served_by": provider.name}

    result, summary = await pool.execute_with_failover(
        send=send, client_factory=lambda p: MagicMock(spec=httpx.AsyncClient)
    )
    assert result == {"served_by": "bedrock"}
    assert summary.providers_tried() == ["primary", "bedrock"]
