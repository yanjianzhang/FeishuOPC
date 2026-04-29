"""Provider pool with retry-with-backoff and provider-level failover.

Why this module exists
----------------------
The original ``LlmAgentAdapter.execute_with_tools`` calls the single
configured provider once per turn. In production we observed two
patterns that broke sessions mid-loop:

1. **Transient 429s / 503s from a single provider** — one throttled
   turn killed the entire orchestration, including already-committed
   code that hadn't yet been pushed.
2. **Single-provider outage** — when the primary upstream (Horay / Ark
   / OpenAI / Anthropic) had a regional incident, every Feishu
   message failed identically until we rotated ``server.env`` by hand.

Hermes Agent handles this with ``runtime_provider.py`` + a provider
registry that maps ``(provider, model) → (api_mode, api_key, base_url)``
and lets the agent loop switch providers without rebuilding the agent.
We mirror the same idea at a smaller scope: a pool of provider
configurations, an exponential-backoff retry on transient errors, and a
"try the next provider" escape hatch when retries are exhausted.

Design decisions
----------------
- **Ordered priority list, not round-robin**: the first entry is the
  preferred provider; later entries are explicit fallbacks. We want the
  default case (first provider healthy) to be deterministic in latency
  and cost.
- **Retry only on transient classes**: timeouts, 429, and 5xx. We do
  NOT retry 4xx (except 429) — those are caller errors (bad prompt,
  bad model name, insufficient quota) and retrying burns money without
  helping.
- **Backoff resets per provider**: when we fail over to provider B, the
  retry counter restarts. B's transient issues are independent from A's.
- **Observable at every hop**: each attempt emits an audit-friendly
  record (provider, attempt, error class, latency). The adapter can
  surface these to the Feishu thread so the user sees "switched from
  glm to deepseek after 3 retries" instead of a silent freeze.
- **No global state, adapter-scoped**: a pool is built at adapter
  construction time from config. Tests build their own with stub
  providers; production builds one from ``model_sources.json``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LlmProviderConfig:
    """Connection config for a single chat-completion provider.

    Kept frozen so a running adapter can't mutate a live provider out
    from under itself — switching providers means swapping the whole
    config object. Matches Hermes's immutable ``RuntimeProvider``
    records.

    Transport variants
    ------------------
    ``transport="openai_http"`` (default) is the historical path: the
    pool opens an ``httpx.AsyncClient`` with
    ``Authorization: Bearer <api_key>`` and the caller's send callback
    POSTs to ``/chat/completions``. ``base_url`` / ``api_key`` /
    ``model`` are all required.

    ``transport="anthropic_bedrock"`` is the AWS-signed path used for
    Bedrock fallback. The pool does **not** build an httpx client for
    this transport — it calls the send callback with ``client=None``,
    and the callback is expected to use an
    ``anthropic.AsyncAnthropicBedrock`` SDK client built from the
    ``aws_*`` fields on this config. ``base_url`` / ``api_key`` are
    ignored. ``model`` carries the Bedrock inference-profile ARN (or
    model ID).
    """

    name: str
    base_url: str
    api_key: str
    model: str
    # Read timeout for a single chat-completion HTTP call. Claude-class
    # models frequently take 60-180s to produce the first byte when the
    # input context is large (200k+ tokens) or when the model is tool-
    # calling under load. 120s was biting on the developer sub-session
    # every time the context got heavy, so the safer default is 300s —
    # still well under the agent-level ``role_agent_timeout_seconds``
    # envelope (420s) so the pool can retry / failover before the outer
    # role agent declares the whole session dead.
    timeout_seconds: float = 300.0
    transport: str = "openai_http"
    aws_region: str = ""
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""


@dataclass
class AttemptRecord:
    """One attempt against one provider. Kept in memory for the current
    ``execute_with_failover`` call only — callers that want persistence
    append to their own audit log."""

    provider_name: str
    attempt_index: int
    error_class: str | None
    http_status: int | None
    latency_ms: int
    succeeded: bool


@dataclass
class PoolRunSummary:
    """All attempts from a single pool invocation. Returned alongside
    the final response so the caller can log how many retries /
    failovers were consumed."""

    attempts: list[AttemptRecord] = field(default_factory=list)
    final_provider_name: str | None = None

    def retries_used(self) -> int:
        """Number of retries beyond the first attempt on the winning
        provider. If we failed over, this is the count on the provider
        that ultimately succeeded (or 0 if the first call succeeded)."""
        if not self.final_provider_name:
            return 0
        same = [
            a for a in self.attempts if a.provider_name == self.final_provider_name
        ]
        return max(0, len(same) - 1)

    def providers_tried(self) -> list[str]:
        seen: list[str] = []
        for a in self.attempts:
            if a.provider_name not in seen:
                seen.append(a.provider_name)
        return seen


class AllProvidersExhaustedError(RuntimeError):
    """Raised when every provider in the pool has exhausted its retries.

    Carries the attempt trail so the caller can surface a specific error
    to the user (different failure modes: "all upstream returned 429"
    vs "DNS failure on primary, 500 on secondary") rather than a generic
    "LLM failed" message.
    """

    def __init__(
        self, summary: PoolRunSummary, last_error: BaseException | None
    ) -> None:
        providers = summary.providers_tried()
        super().__init__(
            f"All {len(providers)} LLM providers exhausted after "
            f"{len(summary.attempts)} attempts: {providers}. "
            f"Last error: {type(last_error).__name__ if last_error else 'unknown'}"
        )
        self.summary = summary
        self.last_error = last_error

    def classify(self) -> str:
        """Return a human-friendly category for user-facing error text.

        Inspects the HTTP status distribution of failed attempts and
        picks a category that maps to operator action:

        - ``rate_limited`` — every failed attempt was 429. Wait or
          raise the provider's quota.
        - ``auth_failed`` — every failed attempt was 401/403. Rotate
          the API key.
        - ``upstream_down`` — every failed attempt was 5xx. The
          provider is having an incident.
        - ``timeout`` — every failed attempt was a transport / timeout
          error (no status). Check network reachability.
        - ``mixed`` — anything else. Logs carry the full attempt trail.
        """
        failed = [a for a in self.summary.attempts if not a.succeeded]
        if not failed:
            # Shouldn't happen — we only raise on failure paths — but
            # degrade gracefully.
            return "mixed"
        statuses = {a.http_status for a in failed}
        errors = {a.error_class for a in failed}
        if statuses == {429}:
            return "rate_limited"
        if statuses and statuses.issubset({401, 403}):
            return "auth_failed"
        if statuses and all(
            s is not None and 500 <= s < 600 for s in statuses
        ):
            return "upstream_down"
        if statuses == {None} and errors and any(
            e and "Timeout" in e for e in errors
        ):
            return "timeout"
        return "mixed"

    def user_message(self) -> str:
        """Short, non-technical message suited for a Feishu thread.

        Still includes the attempt count and providers so an operator
        reading logs can correlate, but leads with the human-meaningful
        classification.
        """
        category = self.classify()
        hints = {
            "rate_limited": "模型服务被限流，稍等再试或联系管理员扩容",
            "auth_failed": "模型服务鉴权失败，请核对并轮换 API key",
            "upstream_down": "上游模型服务出现事故，等待恢复或切备用 provider",
            "timeout": "连接模型服务超时，检查网络或提升 timeout 配额",
            "mixed": "多个 provider 连续失败（详见日志）",
        }
        providers = self.summary.providers_tried()
        return (
            f"{hints[category]}；尝试了 {len(self.summary.attempts)} 次，"
            f"涉及 provider: {providers}"
        )


# Caller-side callback: given a provider config and an optional httpx
# client, issue ONE request and return the parsed JSON body. The pool
# wraps this in retry + failover logic. Keeping the pool ignorant of
# request shape means we can reuse it for both chat-completion calls
# AND the auxiliary summarizer used by ``TailWindowCompressor``.
#
# The ``client`` argument is ``None`` when the provider's transport is
# not ``"openai_http"`` (e.g. ``"anthropic_bedrock"``) — the callback
# owns client construction for those transports.
RequestSender = Callable[
    [LlmProviderConfig, "httpx.AsyncClient | None"], Awaitable[Any]
]


class LlmProviderPool:
    """Ordered pool of provider configs with retry + failover.

    Usage
    -----
    .. code-block:: python

        pool = LlmProviderPool(
            providers=[primary, secondary],
            max_retries_per_provider=3,
            base_backoff_seconds=0.5,
        )
        response, summary = await pool.execute_with_failover(
            send=lambda cfg, client: _post_chat(cfg, client, payload),
        )

    A single call may produce up to
    ``len(providers) * (max_retries_per_provider + 1)`` HTTP requests
    in the worst case. Caller should size timeouts accordingly; the
    wall-clock ceiling we observe in practice with 2 providers × 3
    retries and 0.5/1/2s backoffs is ≈15s of pure backoff (not counting
    in-flight time).
    """

    # HTTP statuses we consider transient. 429 is intentionally in this
    # set even though it's 4xx — "rate limited" is the single most common
    # reason to retry an LLM call.
    TRANSIENT_STATUSES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})

    def __init__(
        self,
        *,
        providers: list[LlmProviderConfig],
        # 3 retries = 4 attempts. Upstream Claude-class relays routinely
        # drop a single long-running connection (~100s) when under load;
        # a 2-retry budget (3 attempts) was burning through in ~330s
        # with almost no gap between attempts, which is exactly when the
        # upstream is still saturated. Four attempts plus longer backoff
        # (see ``base_backoff_seconds``) lets the upstream cool down
        # between tries instead of retrying into the same overload wave.
        max_retries_per_provider: int = 3,
        # Old defaults (0.5 / 8.0) were tuned for 429 rate-limit bursts
        # where coming back 1-8s later was enough. For ReadTimeout on
        # Claude-via-relay, the recovery window is measured in tens of
        # seconds — the upstream is still streaming the previous
        # response when we retry. Start at 5s, grow to 60s so the last
        # retry waits a full minute before giving up.
        base_backoff_seconds: float = 5.0,
        max_backoff_seconds: float = 60.0,
        jitter_seconds: float = 0.25,
        on_attempt: Callable[[AttemptRecord], None] | None = None,
    ) -> None:
        if not providers:
            raise ValueError("LlmProviderPool requires at least one provider")
        if max_retries_per_provider < 0:
            raise ValueError("max_retries_per_provider must be >= 0")
        # Require a real minimum base backoff: zero or near-zero produces
        # a retry storm on 429 responses that upstream providers treat
        # as abuse and may escalate to IP-level blocks. 0.1s is the
        # threshold below which exponential backoff is meaningless even
        # with our jitter.
        if base_backoff_seconds < 0.1:
            raise ValueError(
                "base_backoff_seconds must be >= 0.1 to avoid retry storms"
            )
        if max_backoff_seconds <= 0:
            raise ValueError("max_backoff_seconds must be positive")
        if max_backoff_seconds < base_backoff_seconds:
            raise ValueError(
                "max_backoff_seconds must be >= base_backoff_seconds"
            )

        self._providers = list(providers)
        self._max_retries = max_retries_per_provider
        self._base_backoff = base_backoff_seconds
        self._max_backoff = max_backoff_seconds
        self._jitter = jitter_seconds
        self._on_attempt = on_attempt

    @property
    def providers(self) -> list[LlmProviderConfig]:
        """Read-only view for callers that need to build clients."""
        return list(self._providers)

    def _backoff_for_attempt(self, attempt: int) -> float:
        """Exponential backoff + jitter. attempt=0 is the first retry."""
        window = min(self._max_backoff, self._base_backoff * (2**attempt))
        # Jitter to avoid thundering herd when many sessions retry
        # simultaneously (we have multiple Feishu threads hitting the
        # same provider via the same API key).
        return window + random.uniform(0, self._jitter)

    @classmethod
    def _is_transient(cls, exc: BaseException) -> tuple[bool, int | None]:
        """Decide if an error warrants retry. Returns (retryable, status_code_if_known).

        Recognizes two transport families:

        - ``httpx`` exceptions — the OpenAI-compat HTTP path.
        - ``anthropic`` SDK exceptions — the Bedrock path. We recognize
          them by class-name duck-typing so the pool doesn't hard-import
          the ``anthropic`` package (keeps test fixtures lightweight and
          avoids a boot-time import cost for deployments that never use
          Bedrock).
        """
        if isinstance(exc, httpx.TimeoutException):
            return True, None
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            return status in cls.TRANSIENT_STATUSES, status
        if isinstance(exc, httpx.TransportError):
            # DNS, connection refused, TLS — all transient from the
            # caller's POV. Worth retrying (maybe once, before falling
            # over).
            return True, None
        if isinstance(exc, httpx.RequestError):
            # Catches anything else httpx considers a request-level
            # failure (e.g. ``RemoteProtocolError`` subclasses in
            # older httpx versions that aren't grouped under
            # ``TransportError``). Safer to retry than to give up.
            return True, None

        # Anthropic SDK family — detected by module+class name so we don't
        # take an import dependency on ``anthropic`` in this module.
        # The SDK hierarchy we care about:
        #   - APITimeoutError / APIConnectionError → transient, no status
        #   - APIStatusError (and its subclasses RateLimitError,
        #     InternalServerError, ServiceUnavailableError, etc.) → status
        #     code on ``.status_code``; delegate to TRANSIENT_STATUSES.
        mod = type(exc).__module__ or ""
        if mod.startswith("anthropic"):
            name = type(exc).__name__
            status = getattr(exc, "status_code", None)
            if name in {"APITimeoutError", "APIConnectionError"}:
                return True, None
            if isinstance(status, int):
                return status in cls.TRANSIENT_STATUSES, status
            # Unknown anthropic error with no status — treat as transient
            # (same generosity as httpx.RequestError) so a first-hop hiccup
            # doesn't kill the whole session.
            return True, None
        return False, None

    async def execute_with_failover(
        self,
        *,
        send: RequestSender,
        client_factory: Callable[[LlmProviderConfig], httpx.AsyncClient] | None = None,
    ) -> tuple[Any, PoolRunSummary]:
        """Run ``send`` against each provider in turn, with retries.

        ``client_factory`` is optional so tests can inject a single
        mocked client; production code wires a real factory that builds
        a short-lived ``httpx.AsyncClient`` per provider (so a
        connection pool for a downed provider doesn't hang around).
        """
        summary = PoolRunSummary()
        last_error: BaseException | None = None

        for provider in self._providers:
            # attempts go from 0 (the initial try) to max_retries (after
            # max_retries failed retries)
            for attempt_index in range(self._max_retries + 1):
                t0 = time.monotonic()
                client: httpx.AsyncClient | None = None
                try:
                    # Transport branch: the OpenAI-compat path owns an
                    # ``httpx.AsyncClient``; the Bedrock path owns an
                    # ``anthropic.AsyncAnthropicBedrock`` built lazily
                    # inside the send callback. We do NOT build an httpx
                    # client for non-http transports — passing a
                    # pre-built client the caller won't use would just
                    # leak a TLS handshake per attempt and misclassify
                    # the resulting error as an httpx transport error.
                    if provider.transport == "openai_http":
                        # Split timeouts: a single scalar gets applied to
                        # every phase, which means a sick relay can stall
                        # the connect handshake for the full read budget.
                        # We want connect/write to fail fast (so retries
                        # get a chance) while giving the read phase the
                        # full tail — large Claude responses routinely take
                        # 60-180s on ``write_project_code_batch`` turns.
                        connect_budget = min(15.0, float(provider.timeout_seconds))
                        client = (
                            client_factory(provider)
                            if client_factory is not None
                            else httpx.AsyncClient(
                                base_url=provider.base_url.rstrip("/"),
                                headers={
                                    "Authorization": f"Bearer {provider.api_key}",
                                    "Content-Type": "application/json",
                                },
                                timeout=httpx.Timeout(
                                    connect=connect_budget,
                                    read=float(provider.timeout_seconds),
                                    write=30.0,
                                    pool=5.0,
                                ),
                            )
                        )
                    else:
                        client = None

                    try:
                        result = await send(provider, client)
                    finally:
                        # Only close the client if we built it ourselves;
                        # caller-provided ones are the caller's problem.
                        if client_factory is None and client is not None:
                            await client.aclose()

                    latency_ms = int((time.monotonic() - t0) * 1000)
                    rec = AttemptRecord(
                        provider_name=provider.name,
                        attempt_index=attempt_index,
                        error_class=None,
                        http_status=None,
                        latency_ms=latency_ms,
                        succeeded=True,
                    )
                    summary.attempts.append(rec)
                    summary.final_provider_name = provider.name
                    self._emit(rec)
                    return result, summary

                except BaseException as exc:
                    # Clean the client if we built it (and didn't already)
                    if client_factory is None and client is not None:
                        try:
                            await client.aclose()
                        except Exception:  # pragma: no cover — defensive
                            logger.debug("client close failed", exc_info=True)

                    retryable, status = self._is_transient(exc)
                    latency_ms = int((time.monotonic() - t0) * 1000)
                    rec = AttemptRecord(
                        provider_name=provider.name,
                        attempt_index=attempt_index,
                        error_class=type(exc).__name__,
                        http_status=status,
                        latency_ms=latency_ms,
                        succeeded=False,
                    )
                    summary.attempts.append(rec)
                    self._emit(rec)
                    last_error = exc

                    if not retryable:
                        # Non-retryable from this provider — but we should
                        # still fall over to the next one (e.g. 401 on
                        # primary + valid key on secondary is a real
                        # scenario during key rotation).
                        #
                        # Include a truncated response body for 4xx so
                        # operators can actually diagnose API-shape rejects
                        # (malformed tool result, context overrun on some
                        # relays that return 400 instead of 413, etc.)
                        # without needing httpx wire logging.
                        body_snippet = ""
                        if isinstance(exc, httpx.HTTPStatusError):
                            try:
                                body_snippet = (exc.response.text or "")[:2000]
                            except Exception:  # pragma: no cover - defensive
                                body_snippet = "<unable to read response body>"
                        logger.warning(
                            "non-retryable error %s status=%s on provider=%s; "
                            "moving to next provider. response_body=%r",
                            type(exc).__name__,
                            status,
                            provider.name,
                            body_snippet,
                        )
                        break

                    if attempt_index >= self._max_retries:
                        logger.warning(
                            "retries exhausted on provider=%s; moving to next",
                            provider.name,
                        )
                        break

                    # Otherwise sleep and retry the same provider
                    delay = self._backoff_for_attempt(attempt_index)
                    logger.info(
                        "transient error %s on provider=%s attempt=%d; "
                        "sleeping %.2fs before retry",
                        type(exc).__name__,
                        provider.name,
                        attempt_index,
                        delay,
                    )
                    await asyncio.sleep(delay)

        raise AllProvidersExhaustedError(summary, last_error)

    def _emit(self, rec: AttemptRecord) -> None:
        if self._on_attempt is None:
            return
        try:
            self._on_attempt(rec)
        except Exception:  # pragma: no cover — observer must never break caller
            logger.warning("on_attempt observer failed", exc_info=True)
