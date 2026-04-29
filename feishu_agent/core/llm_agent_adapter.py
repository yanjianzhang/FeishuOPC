from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Union

import httpx

from feishu_agent.core.agent_types import AgentToolExecutor
from feishu_agent.core.cancel_token import (
    CancelToken,
)
from feishu_agent.core.context_compression import (
    ContextCompressor,
    NoOpContextCompressor,
)
from feishu_agent.core.hook_bus import NULL_BUS, HookBus
from feishu_agent.providers.llm_provider_pool import (
    AllProvidersExhaustedError,
    LlmProviderPool,
)
from feishu_agent.core.llm_gateway_shim import (
    AgentConfig,
    ClientConfig,
    ExecutionOptions,
    OpenClawClient,
)
from feishu_agent.tools.tool_verification import ToolVerifier

ToolCallObserver = Callable[
    [str, dict[str, Any], Any, int], Union[Awaitable[None], None]
]

logger = logging.getLogger(__name__)

# Default tool-loop ceiling. Tech-lead orchestration (delegate → wait →
# inspect → commit → push → PR) routinely chains 6–10 tool calls, so
# bumping from the old 8 to 16 keeps complex sessions from failing with
# ``max_turns`` while still bounding runaway loops. Callers may override
# per-instance via ``LlmAgentAdapter(max_tool_turns=…)``.
DEFAULT_MAX_TOOL_TURNS = 16


def _default_dispatch_concurrency_group(tc: dict[str, Any]) -> str | None:
    """Built-in B-2 concurrency-group heuristic for role dispatch.

    Two calls to ``dispatch_role_agent`` with the same ``role_name`` must
    serialise — concurrent child sessions for the same role would race
    on role-scoped state (artifact envelope, sprint claim, shared scratch
    dir). We derive the group from the tool arguments alone so the
    adapter doesn't need the TL role-registry in scope.

    An explicit ``concurrency_group`` in args wins over the role_name
    default, so the TL can force a custom group (e.g. one-shot serialise
    two different roles that share a downstream resource) via
    ``DispatchRoleAgentArgs.concurrency_group``. Any tool other than
    ``dispatch_role_agent`` is left ungrouped (``None``) so the
    caller-supplied resolver is still the authority there.
    """
    fn = tc.get("function") or {}
    if fn.get("name") != "dispatch_role_agent":
        return None
    try:
        args = json.loads(fn.get("arguments", "{}") or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(args, dict):
        return None
    explicit = args.get("concurrency_group")
    if isinstance(explicit, str) and explicit.strip():
        return f"dispatch:{explicit.strip()}"
    role = args.get("role_name")
    if isinstance(role, str) and role.strip():
        return f"dispatch:{role.strip()}"
    return None

# After this many CONSECUTIVE tool calls to the same tool that fail with
# the same set of "missing required fields", abort the session instead
# of burning more LLM budget. Empirically the model almost never
# self-recovers from this pattern — it's a signal that upstream is
# truncating tool-call JSON (see 2026-04-19 Story 3-2 incident where
# ``write_project_code_batch`` emitted ``{}`` 5 turns in a row, then
# ``write_project_code`` emitted ``{}`` another 6 turns, then timed
# out waiting for the next LLM response at 740s). Cutting the loop
# early gives us a clear failure signal in the Feishu thread so the
# tech lead can re-dispatch with a smaller task.
TOOL_ARG_MISSING_LOOP_BUDGET = 3

# Regex used to spot "Field required" validation errors emitted by
# pydantic v2. The error text looks like::
#
#     1 validation error for WriteProjectCodeArgs
#     content
#       Field required [type=missing, input_value={...}, input_type=dict]
#
# so we scan for a bare field-name line followed by "Field required".
_FIELD_NAME_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*$")


def _extract_missing_required_fields(error_text: str) -> list[str]:
    """Parse a pydantic v2 ``ValidationError`` string into the set of
    missing required field names.

    Returns an empty list for errors that are NOT of the
    ``Field required`` flavour (e.g. type errors, value errors): those
    don't benefit from the same circuit-breaker because the model can
    usually self-correct from a concrete "wrong type" message.

    Defensive by design — if pydantic's output format changes we just
    miss the optimisation and fall through to the old raw-string
    path, we do NOT crash the tool loop.
    """
    if not error_text or "Field required" not in error_text:
        return []
    fields: list[str] = []
    lines = error_text.splitlines()
    for i, raw_line in enumerate(lines):
        if "Field required" not in raw_line:
            continue
        if i == 0:
            continue
        prev = lines[i - 1]
        m = _FIELD_NAME_LINE_RE.match(prev)
        if m:
            fields.append(m.group(1))
    # De-dupe while preserving order for nicer logging.
    seen: set[str] = set()
    unique: list[str] = []
    for f in fields:
        if f not in seen:
            seen.add(f)
            unique.append(f)
    return unique
# Kept as an alias so existing callers / tests that import MAX_TOOL_TURNS
# don't break. Prefer ``adapter.max_tool_turns`` going forward.
MAX_TOOL_TURNS = DEFAULT_MAX_TOOL_TURNS


@dataclass
class AgentHandle:
    """Lightweight agent handle storing config for direct LLM calls."""

    agent_id: str
    system_prompt: str
    model: str

    async def execute(self, query: str, **kwargs: Any) -> Any:
        raise NotImplementedError("Use adapter.execute_agent()")


@dataclass
class LlmSessionResult:
    success: bool = False
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    token_usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0
    error_message: str | None = None
    completed_at: datetime | None = None
    stop_reason: str | None = None

    @classmethod
    def from_execution_result(cls, result: Any) -> LlmSessionResult:
        tool_calls = []
        if getattr(result, "tool_calls", None):
            tool_calls = [
                {
                    "tool": tc.tool,
                    "input": tc.input,
                    "output": tc.output,
                    "duration_ms": tc.duration_ms,
                }
                for tc in result.tool_calls
            ]

        token_usage: dict[str, int] = {}
        if getattr(result, "token_usage", None):
            tu = result.token_usage
            token_usage = {
                "input": getattr(tu, "input", 0),
                "output": getattr(tu, "output", 0),
                "cache_read": getattr(tu, "cache_read", 0),
                "cache_write": getattr(tu, "cache_write", 0),
                "total_tokens": getattr(tu, "total_tokens", 0),
            }

        return cls(
            success=result.success,
            content=result.content or "",
            tool_calls=tool_calls,
            token_usage=token_usage,
            latency_ms=result.latency_ms or 0,
            error_message=result.error_message,
            completed_at=getattr(result, "completed_at", None),
            stop_reason=result.stop_reason,
        )


class LlmAgentAdapter:
    """Bridge for role-agent LLM calls — production uses direct HTTP Chat Completions.

    When ``gateway`` is provided (unit tests), :class:`OpenClawClient` from
    :mod:`feishu_agent.core.llm_gateway_shim` drives the same agent surface
    without any external agent-runtime SDK.
    """

    def __init__(
        self,
        *,
        llm_base_url: str = "",
        llm_api_key: str = "",
        default_model: str = "doubao-seed-2-0-pro-260215",
        timeout: int = 120,
        gateway: Any | None = None,
        gateway_url: str = "",
        max_tool_turns: int | None = None,
        max_output_tokens: int | None = None,
        # Back-compat no-ops: older call-sites passed ``api_key`` /
        # ``llm_provider``; they were never stored. Keep the kwargs so
        # those call-sites don't break, but do not silently use them —
        # use ``llm_api_key`` (which is what the HTTP client actually
        # reads). ``gateway_url`` is also accepted for legacy reasons
        # (the mock SDK reads from ``gateway`` directly).
        api_key: str | None = None,
        llm_provider: str | None = None,
        # Harness improvements — all optional, all no-op defaults.
        # Wiring them is the responsibility of
        # ``feishu_runtime_service`` (production) or the test fixture.
        context_compressor: ContextCompressor | None = None,
        tool_verifier: ToolVerifier | None = None,
        provider_pool: LlmProviderPool | None = None,
        # B-2 effect-aware fan-out. ``None`` disables the feature and
        # the adapter runs the legacy sequential tool loop — identical
        # semantics to pre-B-2. A positive value turns on
        # ``partition_by_effect`` + bounded ``asyncio.gather``. Callers
        # that want the feature pass
        # ``settings.max_parallel_tool_calls``; tests can pass a small
        # fixed number to keep timing deterministic.
        max_parallel_tool_calls: int | None = None,
        concurrency_group_resolver: (
            Callable[[dict[str, Any]], str | None] | None
        ) = None,
    ) -> None:
        self.llm_base_url = llm_base_url
        # If a caller passes only the legacy ``api_key``, honor it so we
        # degrade gracefully rather than silently making unauthenticated
        # requests.
        self.llm_api_key = llm_api_key or (api_key or "")
        self.default_model = default_model
        self.timeout = timeout
        self.max_tool_turns = (
            max_tool_turns if max_tool_turns and max_tool_turns > 0
            else DEFAULT_MAX_TOOL_TURNS
        )
        # Maximum output tokens per LLM response. Left unset (``None``),
        # we don't send ``max_tokens`` and the relay/provider picks its
        # own default — which for some Anthropic-compat relays is as
        # low as 4096 and can truncate large tool-call JSON mid-write
        # (``write_project_code_batch`` with a big ``files`` array is
        # the canonical offender). Setting this to e.g. 16384 gives
        # the model enough room to emit a full tool-call envelope.
        self.max_output_tokens: int | None = (
            max_output_tokens if max_output_tokens and max_output_tokens > 0
            else None
        )
        self._gateway = gateway
        self._http: httpx.AsyncClient | None = None
        self._mock_client: Any | None = None
        # Harness plugins. ``NoOpContextCompressor`` is installed by
        # default so downstream code never has to branch on "is there
        # a compressor"; ``tool_verifier`` and ``provider_pool`` stay
        # ``None`` when not configured so we can use identity checks
        # to keep the fast path cheap.
        self._compressor: ContextCompressor = (
            context_compressor or NoOpContextCompressor()
        )
        self._tool_verifier: ToolVerifier | None = tool_verifier
        self._provider_pool: LlmProviderPool | None = provider_pool
        # ``_max_parallel_tool_calls`` is normalised to ``None`` for
        # the feature-off path (no import of asyncio.Semaphore, no
        # behavioural change) and to a bounded positive int otherwise.
        # Clamping at 1 is equivalent to disabling fan-out; clamping at
        # 8 matches the Settings upper bound rationale (provider-pool
        # budgets).
        if max_parallel_tool_calls is not None:
            self._max_parallel_tool_calls: int | None = max(
                1, min(int(max_parallel_tool_calls), 8)
            )
        else:
            self._max_parallel_tool_calls = None
        self._concurrency_group_resolver = concurrency_group_resolver
        if llm_provider is not None and llm_provider != "openai":
            # Not an error — we only support OpenAI-compatible chat
            # completions today — but surface it so we don't lie to the
            # caller. Keeping this quiet would make misconfigs in
            # server.env invisible.
            logger.debug(
                "LlmAgentAdapter llm_provider=%s is currently informational; "
                "requests use the OpenAI-compatible /chat/completions endpoint.",
                llm_provider,
            )

    @property
    def is_connected(self) -> bool:
        if self._gateway:
            return self._mock_client is not None
        return self._http is not None

    async def connect(self) -> None:
        if self._gateway:
            config = ClientConfig(mode="auto", timeout=self.timeout)
            self._mock_client = OpenClawClient(config=config, gateway=self._gateway)
            if hasattr(self._gateway, "connect"):
                try:
                    await self._gateway.connect()
                except Exception:
                    pass
        else:
            if not self.llm_base_url:
                raise RuntimeError(
                    "LlmAgentAdapter requires llm_base_url for direct HTTP mode."
                )
            self._http = httpx.AsyncClient(
                base_url=self.llm_base_url.rstrip("/"),
                headers={
                    "Authorization": f"Bearer {self.llm_api_key}",
                    "Content-Type": "application/json",
                },
                timeout=float(self.timeout),
            )

    async def close(self) -> None:
        """Release the underlying ``httpx.AsyncClient`` (and any mock
        client). Safe to call multiple times; idempotent on a disconnected
        adapter.

        Must be called for every ``connect()`` — the adapter is created
        per Feishu message in production, so leaking the client here
        would leak a TCP/TLS connection pool per message.
        """
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:  # pragma: no cover - defensive
                logger.debug("LlmAgentAdapter http close failed", exc_info=True)
            finally:
                self._http = None
        if self._mock_client is not None:
            close_fn = getattr(self._mock_client, "close", None)
            if callable(close_fn):
                try:
                    maybe = close_fn()
                    if hasattr(maybe, "__await__"):
                        await maybe  # type: ignore[func-returns-value]
                except Exception:  # pragma: no cover - defensive
                    logger.debug("LlmAgentAdapter mock close failed", exc_info=True)
            self._mock_client = None

    async def __aenter__(self) -> "LlmAgentAdapter":
        if not self.is_connected:
            await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def create_agent(
        self,
        agent_id: str,
        system_prompt: str,
        llm_model: str | None = None,
        tool_policy: Any | None = None,
    ) -> Any:
        if self._gateway and self._mock_client:
            agent_config = AgentConfig(
                agent_id=agent_id,
                system_prompt=system_prompt,
                llm_provider="openai",
                llm_model=llm_model or self.default_model,
                tool_policy=tool_policy,
            )
            return await self._mock_client.create_agent(agent_config)

        if self._http is None:
            raise RuntimeError("LlmAgentAdapter not connected. Call connect() first.")

        return AgentHandle(
            agent_id=agent_id,
            system_prompt=system_prompt,
            model=llm_model or self.default_model,
        )

    async def execute_agent(
        self,
        agent: Any,
        query: str,
        timeout_seconds: int | None = None,
    ) -> LlmSessionResult:
        if self._gateway and self._mock_client:
            options = ExecutionOptions(
                timeout_seconds=timeout_seconds or self.timeout,
            )
            result = await agent.execute(query, options=options)
            return LlmSessionResult.from_execution_result(result)

        if self._http is None:
            raise RuntimeError("LlmAgentAdapter not connected. Call connect() first.")

        effective_timeout = float(timeout_seconds or self.timeout)
        sys_prompt = agent.system_prompt if isinstance(agent, AgentHandle) else ""
        model = agent.model if isinstance(agent, AgentHandle) else self.default_model

        t0 = time.monotonic()
        try:
            payload_no_tools: dict[str, Any] = {
                "model": model,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": query},
                ],
                "temperature": 0.3,
            }
            if self.max_output_tokens:
                payload_no_tools["max_tokens"] = self.max_output_tokens
            data = await self._send_chat_completion(
                payload=payload_no_tools,
                timeout=effective_timeout,
            )
        except httpx.TimeoutException:
            latency = int((time.monotonic() - t0) * 1000)
            return LlmSessionResult(
                success=False,
                error_message=f"LLM call timed out after {effective_timeout}s",
                latency_ms=latency,
                stop_reason="timeout",
            )
        except httpx.HTTPStatusError as exc:
            latency = int((time.monotonic() - t0) * 1000)
            body = exc.response.text[:300]
            return LlmSessionResult(
                success=False,
                error_message=f"LLM API error {exc.response.status_code}: {body}",
                latency_ms=latency,
                stop_reason="error",
            )
        except AllProvidersExhaustedError as exc:
            latency = int((time.monotonic() - t0) * 1000)
            return LlmSessionResult(
                success=False,
                error_message=(
                    f"All LLM providers exhausted: "
                    f"{type(exc.last_error).__name__ if exc.last_error else 'unknown'}"
                ),
                latency_ms=latency,
                stop_reason="error",
            )

        latency = int((time.monotonic() - t0) * 1000)

        content = ""
        stop_reason = "complete"
        if data.get("choices"):
            msg = data["choices"][0].get("message", {})
            content = msg.get("content") or ""
            stop_reason = data["choices"][0].get("finish_reason", "complete")

        usage = data.get("usage", {})
        token_usage = {
            "input": usage.get("prompt_tokens", 0),
            "output": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }

        logger.info(
            "LLM call completed model=%s latency=%dms tokens=%d",
            model,
            latency,
            token_usage.get("total_tokens", 0),
        )

        return LlmSessionResult(
            success=True,
            content=content,
            token_usage=token_usage,
            latency_ms=latency,
            stop_reason=stop_reason,
        )

    async def execute_with_tools(
        self,
        agent: Any,
        query: str,
        tool_executor: AgentToolExecutor,
        timeout_seconds: int | None = None,
        *,
        on_tool_call: ToolCallObserver | None = None,
        hook_bus: HookBus | None = None,
        cancel_token: CancelToken | None = None,
        trace_id: str | None = None,
        task_handle: Any | None = None,
    ) -> LlmSessionResult:
        """Execute with a multi-turn tool calling loop.

        Sends tool definitions to the LLM, executes tool calls locally
        via the tool_executor, feeds results back, and repeats until the
        model returns a final text response.

        If ``on_tool_call`` is provided, it is invoked after each tool
        execution with ``(tool_name, arguments, result_or_error, duration_ms)``.
        Observer exceptions are logged and swallowed — they never break the
        tool loop.

        ``hook_bus`` — optional lifecycle event dispatcher. When supplied,
        the loop fires ``pre_llm_call`` / ``post_llm_call`` /
        ``on_tool_call`` / ``on_session_start`` / ``on_session_end``
        around the relevant steps. Subscribers are best-effort; an
        exception from one never breaks the loop.

        ``cancel_token`` — optional cooperative cancellation flag. The
        loop checks it at three safe points per turn: before sending to
        the LLM, between tool calls, and after a provider-pool failover
        burst. A cancelled token stops with ``stop_reason="cancelled"``
        rather than letting the user wait out the full timeout.

        ``trace_id`` — optional identifier included in hook payloads so
        subscribers can correlate events with audit / Feishu thread
        state. Not used otherwise.

        ``task_handle`` — optional ``TaskHandle`` (from
        :mod:`feishu_agent.team.task_service`) for appending
        structured events to the per-thread append-only log. When
        supplied, the tool loop emits ``llm.request`` / ``llm.response``
        / ``tool.call`` / ``tool.result`` / ``llm.compression`` events
        alongside the existing ``HookBus`` fires. Defaults to ``None``
        so legacy tests / harnesses keep working untouched.
        """
        bus = hook_bus or NULL_BUS
        tid = trace_id or ""
        if self._gateway and self._mock_client:
            return await self.execute_agent(agent, query, timeout_seconds)

        if self._http is None:
            raise RuntimeError("LlmAgentAdapter not connected. Call connect() first.")

        effective_timeout = float(timeout_seconds or self.timeout)
        sys_prompt = agent.system_prompt if isinstance(agent, AgentHandle) else ""
        model = agent.model if isinstance(agent, AgentHandle) else self.default_model

        specs = tool_executor.tool_specs()
        tools = [spec.to_openai_chat_tool() for spec in specs]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": query},
        ]

        t0 = time.monotonic()
        total_tokens = 0

        await bus.afire(
            "on_session_start",
            {"trace_id": tid, "model": model, "query_preview": query[:200]},
        )

        try:
            return await self._run_tool_loop(
                agent=agent,
                tool_executor=tool_executor,
                messages=messages,
                tools=tools,
                model=model,
                effective_timeout=effective_timeout,
                t0=t0,
                total_tokens=total_tokens,
                on_tool_call=on_tool_call,
                bus=bus,
                cancel_token=cancel_token,
                trace_id=tid,
                task_handle=task_handle,
            )
        finally:
            # We fire on_session_end from inside ``_run_tool_loop`` so
            # it has the concrete result to attach; the ``finally`` here
            # is a safety net only (handles programmer error / unhandled
            # exception escaping the helper).
            pass

    async def _run_tool_loop(
        self,
        *,
        agent: Any,
        tool_executor: AgentToolExecutor,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        effective_timeout: float,
        t0: float,
        total_tokens: int,
        on_tool_call: ToolCallObserver | None,
        bus: HookBus,
        cancel_token: CancelToken | None,
        trace_id: str,
        task_handle: Any | None = None,
    ) -> LlmSessionResult:
        """Concrete tool loop. Split out of ``execute_with_tools`` so the
        session-start / session-end hook pair can bracket the full
        lifetime without nesting the control flow one level deeper.

        Every ``return`` path goes through ``_finalize`` so
        ``on_session_end`` fires exactly once, carrying the final
        ``LlmSessionResult``.
        """
        def _emit_task_event(
            kind: str, payload: dict[str, Any] | None = None
        ) -> None:
            """Best-effort ``TaskEventLog`` append. Never raises.

            Used for the M1 event stream emissions (``llm.request`` /
            ``llm.response`` / ``tool.*`` / ``llm.compression``).
            Silently skipped when the caller didn't provide a handle.
            """
            if task_handle is None:
                return
            try:
                task_handle.append(
                    kind=kind,
                    trace_id=trace_id or None,
                    payload=payload or {},
                )
            except Exception:  # pragma: no cover — must not break loop
                logger.debug(
                    "task event append failed kind=%s", kind, exc_info=True
                )

        async def _finalize(result: LlmSessionResult) -> LlmSessionResult:
            await bus.afire(
                "on_session_end",
                {
                    "trace_id": trace_id,
                    "model": model,
                    "ok": bool(result.success),
                    "stop_reason": result.stop_reason,
                    "latency_ms": result.latency_ms,
                },
            )
            _emit_task_event(
                "message.outbound",
                {
                    "ok": bool(result.success),
                    "stop_reason": result.stop_reason,
                    "latency_ms": result.latency_ms,
                    "content_preview": (result.content or "")[:400],
                    "tokens": result.token_usage,
                },
            )
            return result

        # Per-session state for the "model is stuck re-issuing the
        # same empty tool call" circuit breaker. Key = (tool_name,
        # sorted tuple of missing field names); value = consecutive
        # failure count. Reset whenever the model switches to a
        # different (tool, missing_fields) combination, because that
        # IS forward progress even if the new call also fails.
        validation_loop_counts: dict[tuple[str, tuple[str, ...]], int] = {}
        last_loop_key: tuple[str, tuple[str, ...]] | None = None

        for turn in range(self.max_tool_turns):
            # Cooperative cancel checkpoint #1 — top of turn.
            if cancel_token is not None and cancel_token.is_cancelled:
                return await _finalize(
                    LlmSessionResult(
                        success=False,
                        error_message=f"Session cancelled ({cancel_token.reason})",
                        latency_ms=int((time.monotonic() - t0) * 1000),
                        stop_reason="cancelled",
                    )
                )

            elapsed = time.monotonic() - t0
            remaining = effective_timeout - elapsed
            if remaining <= 0:
                return await _finalize(
                    LlmSessionResult(
                        success=False,
                        error_message=f"Tool loop timed out after {effective_timeout}s",
                        latency_ms=int(elapsed * 1000),
                        stop_reason="timeout",
                    )
                )

            # Apply context compression before building the payload.
            # The compressor is responsible for its own no-op short-
            # circuit; we just hand it the messages and use whatever it
            # returns. Compression is per-turn so a long-running tool
            # loop gets re-evaluated after every tool response.
            try:
                messages, compression_decision = await self._compressor.compress(
                    messages, model=model, task_handle=task_handle
                )
            except Exception:  # pragma: no cover — never break the loop
                logger.exception(
                    "context compressor raised; proceeding with original messages"
                )
            else:
                if compression_decision.applied:
                    logger.info(
                        "context compression turn=%d tokens %d→%d "
                        "collapsed=%d",
                        turn,
                        compression_decision.tokens_before,
                        compression_decision.tokens_after,
                        compression_decision.collapsed,
                    )
                    _emit_task_event(
                        "llm.compression",
                        {
                            "turn": turn,
                            "tokens_before": compression_decision.tokens_before,
                            "tokens_after": compression_decision.tokens_after,
                            "collapsed": compression_decision.collapsed,
                            "reason": compression_decision.reason,
                        },
                    )

            # M2: project the task log into a TaskState and inject
            # any fired reminders as a transient user message. The
            # reminder is NOT appended to ``messages`` — it lives only
            # in this turn's payload, so it is naturally exempt from
            # context compression (which only sees ``messages``) and
            # never accumulates in the persistent history.
            request_messages = messages
            if task_handle is not None:
                try:
                    from feishu_agent.team.memory_assembler import (
                        build_transient_reminder_fragment,
                    )

                    reminder_fragment = build_transient_reminder_fragment(
                        task_handle
                    )
                except Exception:  # pragma: no cover — never break loop
                    reminder_fragment = None
                if reminder_fragment is not None:
                    reminder_text = reminder_fragment.content
                    reminder_rule_ids = list(
                        reminder_fragment.metadata.get("rule_ids") or []
                    )
                    request_messages = messages + [
                        {"role": "user", "content": reminder_text}
                    ]
                    _emit_task_event(
                        "reminder.emitted",
                        {
                            "turn": turn,
                            "rule_ids": reminder_rule_ids,
                            "count": int(reminder_fragment.metadata.get("count") or 0),
                        },
                    )

            payload: dict[str, Any] = {
                "model": model,
                "messages": request_messages,
                "temperature": 0.3,
            }
            if tools:
                payload["tools"] = tools
            if self.max_output_tokens:
                payload["max_tokens"] = self.max_output_tokens

            await bus.afire(
                "pre_llm_call",
                {
                    "trace_id": trace_id,
                    "model": model,
                    "turn": turn,
                    "messages_len": len(request_messages),
                    "tools_count": len(tools),
                },
            )
            _emit_task_event(
                "llm.request",
                {
                    "turn": turn,
                    "model": model,
                    "messages_len": len(request_messages),
                    "tools_count": len(tools),
                },
            )
            llm_t0 = time.monotonic()
            try:
                data = await self._send_chat_completion(
                    payload=payload,
                    timeout=remaining,
                )
            except httpx.TimeoutException:
                return await _finalize(
                    LlmSessionResult(
                        success=False,
                        error_message=f"LLM call timed out (turn {turn + 1})",
                        latency_ms=int((time.monotonic() - t0) * 1000),
                        stop_reason="timeout",
                    )
                )
            except httpx.HTTPStatusError as exc:
                body = exc.response.text[:300]
                return await _finalize(
                    LlmSessionResult(
                        success=False,
                        error_message=f"LLM API error {exc.response.status_code}: {body}",
                        latency_ms=int((time.monotonic() - t0) * 1000),
                        stop_reason="error",
                    )
                )
            except AllProvidersExhaustedError as exc:
                # Pool-level failure: every provider tried, every retry
                # burned. Use ``exc.user_message()`` to give the user a
                # classification-aware string (rate_limited / auth_failed
                # / upstream_down / timeout / mixed) instead of a
                # generic "LLM failed" — distinguishes "rotate the key"
                # from "wait for the incident to clear."
                logger.warning(
                    "LLM pool exhausted category=%s attempts=%d providers=%s "
                    "last=%s",
                    exc.classify(),
                    len(exc.summary.attempts),
                    exc.summary.providers_tried(),
                    type(exc.last_error).__name__ if exc.last_error else "unknown",
                )
                return await _finalize(
                    LlmSessionResult(
                        success=False,
                        error_message=exc.user_message(),
                        latency_ms=int((time.monotonic() - t0) * 1000),
                        stop_reason="error",
                    )
                )

            # Provider-pool retry burst can be long; re-check cancel
            # before continuing with any more work this turn.
            if cancel_token is not None and cancel_token.is_cancelled:
                return await _finalize(
                    LlmSessionResult(
                        success=False,
                        error_message=f"Session cancelled ({cancel_token.reason})",
                        latency_ms=int((time.monotonic() - t0) * 1000),
                        stop_reason="cancelled",
                    )
                )

            usage = data.get("usage", {})
            total_tokens += usage.get("total_tokens", 0)
            llm_latency_ms = int((time.monotonic() - llm_t0) * 1000)
            finish_reason = (
                data.get("choices", [{}])[0].get("finish_reason")
                if data.get("choices")
                else None
            )
            await bus.afire(
                "post_llm_call",
                {
                    "trace_id": trace_id,
                    "model": model,
                    "turn": turn,
                    "usage": usage,
                    "stop_reason": finish_reason,
                    "latency_ms": llm_latency_ms,
                },
            )
            _emit_task_event(
                "llm.response",
                {
                    "turn": turn,
                    "usage": usage,
                    "stop_reason": finish_reason,
                    "latency_ms": llm_latency_ms,
                },
            )

            if not data.get("choices"):
                return await _finalize(
                    LlmSessionResult(
                        success=False,
                        error_message="LLM returned no choices",
                        latency_ms=int((time.monotonic() - t0) * 1000),
                        stop_reason="error",
                    )
                )

            choice = data["choices"][0]
            msg = choice.get("message", {})
            tool_calls = msg.get("tool_calls")

            if not tool_calls:
                latency = int((time.monotonic() - t0) * 1000)
                logger.info(
                    "LLM completed model=%s turns=%d latency=%dms tokens=%d",
                    model, turn + 1, latency, total_tokens,
                )
                return await _finalize(
                    LlmSessionResult(
                        success=True,
                        content=msg.get("content") or "",
                        token_usage={"input": usage.get("prompt_tokens", 0), "output": usage.get("completion_tokens", 0), "total_tokens": total_tokens},
                        latency_ms=latency,
                        stop_reason=choice.get("finish_reason", "complete"),
                    )
                )

            # Normalize assistant turn before feeding it back into the
            # next request. OpenAI's own spec permits
            # ``{"role":"assistant","content":null,"tool_calls":[...]}``
            # and ``{...}`` without a ``content`` key, but some
            # OpenAI-compatible relays that re-translate to the Anthropic
            # Messages API (e.g. horay.ai sr-endpoint) mishandle both
            # forms:
            #   * content=null  → synthesises an empty text block
            #                     ``{"type":"text"}`` that Anthropic
            #                     rejects with ``content[M].text:
            #                     Field required``.
            #   * content omitted → relay's own validator rejects with
            #                       ``content: Input should be a valid
            #                       list`` before forwarding.
            # A short non-empty placeholder survives both translations:
            # the relay produces a valid ``{"type":"text","text":"..."}``
            # block, Anthropic accepts it, and a tool-using assistant
            # message carrying a one-character filler doesn't measurably
            # affect model behaviour on the next turn.
            if tool_calls:
                content_val = msg.get("content")
                if content_val is None or (
                    isinstance(content_val, str) and not content_val.strip()
                ):
                    msg = {**msg, "content": "."}
            messages.append(msg)

            # B-2 effect-aware fan-out. When enabled, we pre-execute
            # the turn's tool calls according to ``partition_by_effect``
            # (reads/selfs in parallel, world-effecting calls
            # serialized). The raw results are cached by call_id; the
            # existing sequential post-processing loop below then
            # consumes them in ORIGINAL ORDER so every downstream
            # observer — validation-loop tracker, verifier, hook-bus,
            # message append — sees the same sequence it has always
            # seen. This two-phase split keeps the refactor small and
            # preserves the early-return on ``tool_arg_loop``, which
            # is a correctness contract the test suite relies on.
            # M1 fix — ``tool.call`` events fire BEFORE any tool
            # execution, matching the pre-B-2 ordering. Observers that
            # rely on ``tool.call`` as "about to run" (e.g. Feishu
            # progress updates) therefore see identical timing whether
            # the turn is serial or fan-out. Post-processing below does
            # NOT re-emit ``tool.call``; only ``tool.result`` /
            # ``tool.error`` fire after execution finishes.
            for _tc_pre in tool_calls or ():
                _fn_pre = _tc_pre.get("function", {}) or {}
                try:
                    _args_pre = json.loads(_fn_pre.get("arguments", "{}"))
                except json.JSONDecodeError:
                    _args_pre = {}
                _emit_task_event(
                    "tool.call",
                    {
                        "tool_name": _fn_pre.get("name", ""),
                        "call_id": _tc_pre.get("id"),
                        "args_preview": json.dumps(
                            _args_pre, ensure_ascii=False, default=str
                        )[:1000],
                        "prefetched": self._max_parallel_tool_calls
                        is not None,
                    },
                )

            prefetched: dict[str, tuple[Any, int, bool]] = {}
            if self._max_parallel_tool_calls is not None and tool_calls:
                prefetched = await self._prefetch_tool_calls(
                    tool_calls=tool_calls,
                    tool_executor=tool_executor,
                    cancel_token=cancel_token,
                    emit_task_event=_emit_task_event,
                    turn=turn,
                )

            for tc in tool_calls:
                # Cooperative cancel checkpoint #2 — between tool calls.
                # Covers the common case of a multi-tool-call turn where
                # the first tool took a while and the user gave up.
                if cancel_token is not None and cancel_token.is_cancelled:
                    return await _finalize(
                        LlmSessionResult(
                            success=False,
                            error_message=f"Session cancelled ({cancel_token.reason})",
                            latency_ms=int((time.monotonic() - t0) * 1000),
                            stop_reason="cancelled",
                        )
                    )

                fn = tc.get("function", {})
                tool_name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except json.JSONDecodeError:
                    args = {}

                logger.info("Tool call: %s(%s)", tool_name, json.dumps(args, ensure_ascii=False)[:200])
                # Either consume a result prefetched in the B-2 phase,
                # or fall back to the legacy inline execute path.
                # ``tool.call`` was already emitted above (M1) so
                # observers don't see the event twice.
                cid = tc.get("id") or ""
                if cid in prefetched:
                    tool_result, tool_duration_ms, tool_errored = (
                        prefetched[cid]
                    )
                else:
                    tool_t0 = time.monotonic()
                    tool_errored = False
                    try:
                        tool_result = await tool_executor.execute_tool(tool_name, args)
                    except Exception as exc:
                        logger.exception("Tool %s failed", tool_name)
                        tool_result = {"error": str(exc)}
                        tool_errored = True
                    tool_duration_ms = int((time.monotonic() - tool_t0) * 1000)
                _emit_task_event(
                    "tool.error" if tool_errored else "tool.result",
                    {
                        "tool_name": tool_name,
                        "call_id": tc.get("id"),
                        "duration_ms": tool_duration_ms,
                        "result_preview": (
                            json.dumps(
                                tool_result, ensure_ascii=False, default=str
                            )[:1500]
                            if not isinstance(tool_result, str)
                            else tool_result[:1500]
                        ),
                    },
                )

                # --- Validation-error circuit breaker ----------------
                # Detect the "model keeps re-issuing the same tool call
                # with missing required fields" failure mode. The raw
                # pydantic error string is noisy and does not teach the
                # model how to recover, so (a) rewrite it into a
                # structured, actionable payload, and (b) abort the
                # session after TOOL_ARG_MISSING_LOOP_BUDGET consecutive
                # identical failures before we burn the whole 740s
                # timeout budget waiting for a miracle.
                missing_fields: list[str] = []
                if isinstance(tool_result, dict) and tool_result.get("error"):
                    raw_err = str(tool_result.get("error") or "")
                    missing_fields = _extract_missing_required_fields(raw_err)
                if missing_fields:
                    logger.warning(
                        "Tool %s called with missing required fields %s; "
                        "rewriting error for LLM and tracking loop",
                        tool_name,
                        missing_fields,
                    )
                    tool_result = {
                        "error": "TOOL_CALL_ARG_MISSING",
                        "tool": tool_name,
                        "missing_required_fields": missing_fields,
                        "you_sent_arguments": args,
                        "guidance": (
                            "DO NOT re-issue this tool call with the same "
                            "missing fields — the next attempt will fail the "
                            "same way. Likely causes: (1) your tool-call "
                            "JSON is being truncated by the model's output "
                            "budget because the payload is too large; "
                            "(2) you forgot the field. Recovery options, "
                            "in order of preference: "
                            "(a) if the missing field is content/file-body, "
                            "split the work into SMALLER single-file "
                            "write_project_code calls (one logical section "
                            "at a time) or drop large comments/blank lines; "
                            "(b) if you still cannot fit the content, call "
                            "write_role_artifact with a hand-off note "
                            "describing what is NOT yet done, then stop; "
                            "(c) pick a different, smaller file to write "
                            "first and come back to this one later. Do NOT "
                            "retry the exact same call."
                        ),
                    }
                    key = (tool_name, tuple(sorted(missing_fields)))
                    if key == last_loop_key:
                        validation_loop_counts[key] = (
                            validation_loop_counts.get(key, 0) + 1
                        )
                    else:
                        validation_loop_counts = {key: 1}
                        last_loop_key = key
                    if (
                        validation_loop_counts[key]
                        >= TOOL_ARG_MISSING_LOOP_BUDGET
                    ):
                        abort_msg = (
                            f"Tool-argument loop detected: {tool_name} "
                            f"failed {validation_loop_counts[key]} times "
                            f"in a row with missing required field(s) "
                            f"{list(key[1])}. The model appears unable to "
                            f"produce valid arguments for this call "
                            f"(likely upstream output-token truncation). "
                            f"Aborting the session to avoid burning more "
                            f"LLM budget. Re-dispatch with a smaller "
                            f"scope, or tell the sub-agent to use "
                            f"single-file writes / write_role_artifact."
                        )
                        logger.error(abort_msg)
                        return await _finalize(
                            LlmSessionResult(
                                success=False,
                                error_message=abort_msg,
                                latency_ms=int((time.monotonic() - t0) * 1000),
                                stop_reason="tool_arg_loop",
                            )
                        )
                else:
                    # Any tool call that does NOT trip the missing-field
                    # pattern is considered forward progress — reset the
                    # loop tracker. This keeps the circuit breaker
                    # specific to "stuck on the same call" without
                    # penalising genuinely new activity that happens to
                    # error for a different reason.
                    validation_loop_counts = {}
                    last_loop_key = None

                # Post-dispatch verification. Replaces the result with a
                # structured error on hard-fail so the LLM sees the
                # failure instead of the fictional success. Verifier
                # with no registered validator for this tool is a no-op.
                if self._tool_verifier is not None:
                    verification = await self._tool_verifier.verify(
                        tool_name, args, tool_result
                    )
                    if not verification.ok:
                        logger.warning(
                            "tool verification failed tool=%s error=%s",
                            tool_name,
                            verification.error,
                        )
                        tool_result = {
                            "error": "TOOL_VERIFICATION_FAILED",
                            "tool": tool_name,
                            "verification_error": verification.error,
                            "original_result": tool_result,
                        }

                # Hook-bus subscribers (audit/lineage/thread-update) see
                # every tool call. Fire before the legacy ``on_tool_call``
                # observer so a bus subscriber can influence audit/
                # observability even if the legacy observer raises.
                await bus.afire(
                    "on_tool_call",
                    {
                        "trace_id": trace_id,
                        "tool_name": tool_name,
                        "args": args,
                        "result": tool_result,
                        "duration_ms": tool_duration_ms,
                    },
                )

                if on_tool_call is not None:
                    try:
                        maybe = on_tool_call(tool_name, args, tool_result, tool_duration_ms)
                        if hasattr(maybe, "__await__"):
                            await maybe  # type: ignore[func-returns-value]
                    except Exception:
                        logger.warning("on_tool_call observer failed", exc_info=True)

                result_str = json.dumps(tool_result, ensure_ascii=False, default=str) if not isinstance(tool_result, str) else tool_result
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": result_str,
                })

        return await _finalize(
            LlmSessionResult(
                success=False,
                error_message=f"Exceeded max tool turns ({self.max_tool_turns})",
                latency_ms=int((time.monotonic() - t0) * 1000),
                stop_reason="max_turns",
            )
        )

    async def _prefetch_tool_calls(
        self,
        *,
        tool_calls: list[dict[str, Any]],
        tool_executor: AgentToolExecutor,
        cancel_token: CancelToken | None,
        emit_task_event: Callable[[str, dict[str, Any]], None],
        turn: int,
    ) -> dict[str, tuple[Any, int, bool]]:
        """B-2 pre-execution phase.

        Partitions the turn's tool calls by effect and runs concurrent
        groups under a bounded semaphore. Returns a mapping of
        ``tool_call_id`` → ``(result, duration_ms, errored)`` so the
        outer sequential loop can finish the post-processing (validator,
        verifier, observer, message append) in the exact order the
        model emitted the calls.

        Raises nothing from inside a tool — per-call exceptions are
        caught and encoded as ``{"error": ...}`` so ``asyncio.gather``
        never propagates a partial failure that would drop sibling
        results. The outer loop's verifier / validation logic then
        sees the errored payload identically to the legacy inline
        path.

        Cancellation: a cancel before this helper runs is caught by
        the per-tc check in the outer loop. Cancellation DURING the
        helper is advisory — we let in-flight tool calls complete so
        their side-effects (writes, locks, network) don't leak, then
        the outer loop's checkpoint bails on the next iteration.
        """
        from asyncio import Semaphore, gather

        from feishu_agent.core.tool_fanout import partition_by_effect

        # Feature off (None) is handled by the caller, so we only
        # reach here with a bounded positive integer.
        assert self._max_parallel_tool_calls is not None
        cap = self._max_parallel_tool_calls

        spec_index: dict[str, Any] = {}
        try:
            for spec in tool_executor.tool_specs():
                spec_index[spec.name] = spec
        except Exception:
            # An executor that can't enumerate its specs just falls
            # back to "everything is world-effecting" — safe default.
            logger.debug(
                "tool_executor.tool_specs() raised during fan-out prefetch; "
                "defaulting all calls to world-effect",
                exc_info=True,
            )
            spec_index = {}

        # H1 fix — always apply the built-in ``dispatch_role_agent``
        # grouping (same role_name ⇒ same concurrency_group) so two
        # concurrent dispatches of the same role serialise. A caller
        # who supplied their own resolver wraps it: we first consult
        # the caller (explicit intent wins), and fall back to the
        # built-in heuristic. This closes the B-2 spec gap where the
        # default ``None`` resolver let same-role dispatches parallelise.
        caller_resolver = self._concurrency_group_resolver
        def _resolver(tc: dict[str, Any]) -> str | None:
            if caller_resolver is not None:
                try:
                    explicit = caller_resolver(tc)
                except Exception:  # pragma: no cover — defensive
                    explicit = None
                if explicit:
                    return explicit
            return _default_dispatch_concurrency_group(tc)

        groups = partition_by_effect(
            tool_calls, specs=spec_index, concurrency_group_of=_resolver
        )

        emit_task_event(
            "fanout.begin",
            {
                "turn": turn,
                "groups": [
                    {"size": g.size, "mode": g.mode} for g in groups
                ],
                "cap": cap,
            },
        )
        t_start = time.monotonic()

        sem = Semaphore(cap)
        prefetched: dict[str, tuple[Any, int, bool]] = {}

        async def _exec_one(tc: dict[str, Any]) -> None:
            cid = tc.get("id") or ""
            fn = tc.get("function", {}) or {}
            tool_name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}
            t0 = time.monotonic()
            async with sem:
                try:
                    result = await tool_executor.execute_tool(
                        tool_name, args
                    )
                    errored = False
                except Exception as exc:
                    # Executor raised. Encode as a structured error so
                    # the outer loop's post-processing treats this like
                    # any other tool failure; the caller's validation
                    # layer can still inspect the content and decide.
                    logger.exception(
                        "Tool %s failed during fan-out prefetch", tool_name
                    )
                    result = {"error": str(exc)}
                    errored = True
            prefetched[cid] = (
                result,
                int((time.monotonic() - t0) * 1000),
                errored,
            )

        for group in groups:
            if group.mode == "concurrent" and group.size > 1:
                await gather(*(_exec_one(tc) for tc in group.calls))
            else:
                for tc in group.calls:
                    if (
                        cancel_token is not None
                        and cancel_token.is_cancelled
                    ):
                        # Leave this and subsequent calls unpopulated;
                        # the outer loop's cancel checkpoint will
                        # short-circuit on the next iteration.
                        break
                    await _exec_one(tc)

        emit_task_event(
            "fanout.end",
            {
                "turn": turn,
                "total_duration_ms": int(
                    (time.monotonic() - t_start) * 1000
                ),
                "executed": len(prefetched),
                "requested": len(tool_calls),
            },
        )
        return prefetched

    async def _send_chat_completion(
        self,
        *,
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        """Send one /chat/completions request, routing through the
        provider pool if configured.

        The pool path retries on transient errors (429/5xx/timeout) and
        falls over to secondary providers if the primary exhausts its
        retry budget. The direct path is preserved for tests and for
        deployments that haven't opted into the pool yet.

        Raises the same exception classes as ``httpx.AsyncClient.post``
        plus ``AllProvidersExhaustedError`` when the pool gives up; the
        caller has to catch both because the pool's last-error may
        itself be a subclass of ``httpx.HTTPStatusError``.
        """
        if self._provider_pool is not None:
            # When the pool is installed, the adapter's base_url /
            # api_key are ignored — the pool owns provider selection
            # and builds its own client per attempt. Ownership of the
            # httpx client lifetime moves to the pool so we don't leak
            # connections when failing over mid-call.
            async def _send(provider, client):  # type: ignore[no-untyped-def]
                # Transport branch: pool only builds the httpx client
                # for ``openai_http`` providers; for Bedrock the client
                # is ``None`` and the transport module owns its own
                # AsyncAnthropicBedrock instance per attempt.
                if provider.transport == "anthropic_bedrock":
                    from feishu_agent.providers.bedrock_transport import (
                        send_bedrock_chat_completion,
                    )

                    return await send_bedrock_chat_completion(
                        provider, payload=payload, timeout=timeout
                    )
                resp = await client.post(
                    "/chat/completions",
                    json={**payload, "model": payload.get("model") or provider.model},
                    timeout=timeout,
                )
                resp.raise_for_status()
                return resp.json()

            data, summary = await self._provider_pool.execute_with_failover(
                send=_send,
            )
            if summary.retries_used() > 0 or len(summary.providers_tried()) > 1:
                logger.info(
                    "llm provider pool recovered: provider=%s retries=%d providers_tried=%s",
                    summary.final_provider_name,
                    summary.retries_used(),
                    summary.providers_tried(),
                )
            return data

        if self._http is None:
            raise RuntimeError(
                "LlmAgentAdapter not connected. Call connect() first."
            )
        resp = await self._http.post(
            "/chat/completions", json=payload, timeout=timeout
        )
        resp.raise_for_status()
        return resp.json()

    async def spawn_sub_agent(
        self,
        role_name: str,
        task: str,
        system_prompt: str,
        tools_allow: list[str] | None = None,
        model: str | None = None,
        timeout: int | None = None,
    ) -> LlmSessionResult:
        agent = await self.create_agent(
            agent_id=f"role-{role_name}",
            system_prompt=system_prompt,
            llm_model=model,
        )
        return await self.execute_agent(agent, task, timeout_seconds=timeout)

    async def spawn_sub_agent_with_tools(
        self,
        role_name: str,
        task: str,
        system_prompt: str,
        tool_executor: AgentToolExecutor,
        *,
        model: str | None = None,
        timeout: int | None = None,
        on_tool_call: ToolCallObserver | None = None,
        hook_bus: HookBus | None = None,
        cancel_token: CancelToken | None = None,
        trace_id: str | None = None,
    ) -> LlmSessionResult:
        """Spawn a sub-agent that runs its own tool-calling loop.

        Unlike ``spawn_sub_agent`` (single-shot LLM call), this method
        gives the sub-agent a real tool loop bounded by MAX_TOOL_TURNS
        and the supplied timeout. Use ``AllowListedToolExecutor`` to
        restrict the sub-agent to a subset of the underlying executor's
        tools.

        ``hook_bus`` / ``cancel_token`` / ``trace_id`` are forwarded to
        ``execute_with_tools`` so sub-agent sessions emit the same
        lifecycle events (with their child ``trace_id``) and honor the
        parent's cancel request — you don't want a user's "取消" to
        stop the parent but leave a child still grinding.
        """
        agent = await self.create_agent(
            agent_id=f"role-{role_name}",
            system_prompt=system_prompt,
            llm_model=model,
        )
        return await self.execute_with_tools(
            agent,
            task,
            tool_executor,
            timeout_seconds=timeout,
            on_tool_call=on_tool_call,
            hook_bus=hook_bus,
            cancel_token=cancel_token,
            trace_id=trace_id,
        )
