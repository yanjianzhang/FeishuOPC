"""Wiring helpers that bolt tier-2 services onto the Feishu runtime.

Why a separate module?
----------------------
``feishu_runtime_service.py`` is already ~1700 lines and carries the
top-level Feishu message flow. Tier-2 adds four optional behaviors:

1. Per-message ``HookBus`` + ``CancelToken`` allocation.
2. Cancel keyword detection at the front of the message handler.
3. Session lineage tracker attachment + audit persistence.
4. MCP server loading + composition into the TL tool executor.

Keeping the wiring in its own module:
- Makes the on/off logic testable in isolation.
- Avoids adding yet more conditional branching to the main handler.
- Lets us add tier-3 (e.g. HTTP-SSE MCP transport, multi-host cancel
  registry) without rewriting the same file again.

Everything here is opt-in: if the relevant setting isn't configured,
the helpers return ``None`` / no-op objects so production behavior is
unchanged until the operator flips a knob.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from feishu_agent.core.cancel_token import (
    GLOBAL_REGISTRY,
    CancelKey,
    CancelToken,
    CancelTokenRegistry,
)
from feishu_agent.core.hook_bus import HookBus
from feishu_agent.core.session_lineage import SessionLineageTracker
from feishu_agent.team.audit_service import AuditService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cancel keyword routing
# ---------------------------------------------------------------------------

# Why a distinct set from ``_CANCEL_KEYWORDS`` in ``feishu_runtime_service``:
# that set handles cancellation of a pending-action prompt (a different
# state). These keywords cancel a *live* tool loop. Overlap is fine —
# the runtime checks the pending-action path FIRST, so a "取消" that's
# resolving a pending confirmation never reaches this logic.
_LIVE_CANCEL_KEYWORDS: frozenset[str] = frozenset(
    {
        "取消",
        "停",
        "停一下",
        "停下",
        "打断",
        "abort",
        "cancel",
        "halt",
        "stop",
    }
)

# Match "@bot 取消" / "@bot cancel" after stripping leading mentions.
# We'd rather over-match slightly (e.g., strip any "@xxx" prefix) than
# require a specific bot mention — the bot layer has already resolved
# which bot this message targets.
_MENTION_PREFIX_RE = re.compile(r"^(?:@\S+\s+)+")


def is_live_cancel_command(text: str) -> bool:
    """Return True iff the message text means "stop the running session".

    Stripped mentions + lowercased + punctuation-trimmed comparison
    against the keyword set. Deliberately strict — matching "取消一下"
    or "我想要取消" could accidentally kill an active session when the
    user is only discussing cancellation. False positives here are
    worse than false negatives (user can just re-issue).
    """
    if not text:
        return False
    stripped = _MENTION_PREFIX_RE.sub("", text).strip()
    # Trim trailing punctuation so "取消！" still matches.
    stripped = stripped.rstrip("!?.,。！？")
    return stripped.lower() in _LIVE_CANCEL_KEYWORDS


def cancel_key_for(
    *, bot_name: str, chat_id: str | None, thread_id: str | None
) -> CancelKey:
    """Build the registry key that uniquely identifies a live session.

    Missing ``chat_id`` / ``thread_id`` are normalized to empty strings
    so the key is still comparable — but callers should avoid leaning
    on those cases because two such sessions would collide. The
    Feishu runtime always has at least ``chat_id`` in practice; the
    defaults exist so unit tests don't have to fabricate them.
    """
    return CancelKey(
        bot_name=bot_name,
        chat_id=chat_id or "",
        thread_id=thread_id or "",
    )


# ---------------------------------------------------------------------------
# Per-message hook/cancel allocation
# ---------------------------------------------------------------------------


@dataclass
class Tier2RuntimeContext:
    """Bundle of tier-2 services allocated for a single Feishu message.

    One per message. Holds everything downstream wiring (TL executor,
    adapter, lineage subscribers) needs to plug in. Kept as a plain
    dataclass so tests can instantiate it without a real registry.
    """

    hook_bus: HookBus
    cancel_token: CancelToken
    cancel_key: CancelKey
    lineage: SessionLineageTracker


def allocate_runtime_context(
    *,
    bot_name: str,
    chat_id: str | None,
    thread_id: str | None,
    trace_id: str,
    root_role: str = "tech_lead",
    registry: CancelTokenRegistry = GLOBAL_REGISTRY,
) -> Tier2RuntimeContext:
    """Create a fresh bus / cancel token / lineage tracker for a message.

    Also registers the cancel token with ``registry`` so an incoming
    cancel command on the same key can find it. Caller is responsible
    for calling ``release_runtime_context`` (see below) once the
    message finishes, to avoid leaking stale tokens.
    """
    bus = HookBus()
    key = cancel_key_for(
        bot_name=bot_name, chat_id=chat_id, thread_id=thread_id
    )
    token = registry.register(key)

    tracker = SessionLineageTracker()
    tracker.attach_to(bus, root_trace_id=trace_id, root_role=root_role)

    return Tier2RuntimeContext(
        hook_bus=bus,
        cancel_token=token,
        cancel_key=key,
        lineage=tracker,
    )


def release_runtime_context(
    ctx: Tier2RuntimeContext,
    *,
    registry: CancelTokenRegistry = GLOBAL_REGISTRY,
) -> None:
    """Tear down the cancel registration for a finished session.

    Idempotent — safe to call from a ``finally`` even if the session
    never entered the tool loop. Doesn't touch the hook bus / lineage
    tracker; those are expected to be garbage-collected with the
    ``Tier2RuntimeContext`` instance.
    """
    registry.clear(ctx.cancel_key)


# ---------------------------------------------------------------------------
# Lineage → audit persistence
# ---------------------------------------------------------------------------


def attach_lineage_audit(
    *,
    bus: HookBus,
    tracker: SessionLineageTracker,
    audit_service: AuditService,
    root_trace_id: str,
) -> None:
    """Persist the lineage tree when the root session ends.

    Subscribes to ``on_session_end``; when the event's ``trace_id``
    matches the root, writes ``{root}-lineage.json`` with the full
    tree + breadcrumb + per-node timings. This gives us post-hoc
    debugging without bloating every regular audit record.

    We intentionally use ``{root}-lineage`` (not ``{root}/lineage``)
    because the audit service validates trace_ids to be flat
    alphanumeric — nested paths would be rejected at validation.
    """

    async def _on_end(event: str, payload: dict) -> None:
        if event != "on_session_end":
            return
        if payload.get("trace_id") != root_trace_id:
            return  # only the root's end triggers persistence
        try:
            nodes = tracker.all_nodes()
            audit_service.write(
                f"{root_trace_id}-lineage",
                {
                    "root_trace_id": root_trace_id,
                    "tree": tracker.render_tree(),
                    "nodes": [
                        {
                            "trace_id": n.trace_id,
                            "parent_trace_id": n.parent_trace_id,
                            "role": n.role,
                            "started_at": n.started_at,
                            "ended_at": n.ended_at,
                            "duration_ms": n.duration_ms,
                            "ok": n.ok,
                            "stop_reason": n.stop_reason,
                        }
                        for n in nodes
                    ],
                },
            )
        except Exception:
            logger.warning(
                "lineage audit write failed for trace=%s",
                root_trace_id,
                exc_info=True,
            )

    bus.subscribe("on_session_end", _on_end)


# ---------------------------------------------------------------------------
# MCP server config loading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class McpServerSpec:
    """A single MCP server entry from the JSONL config.

    Fields:
        name: short identifier, becomes the ``mcp__<name>__<tool>``
              namespace. Must be unique across the config.
        command: argv to launch the server (e.g. ``["npx", "-y", "foo-mcp"]``).
        env: extra environment variables merged on top of the parent
             process env (``None`` = no overrides).
        cwd: working directory for the subprocess.
        enabled: soft switch so operators can comment out a server by
                 setting ``"enabled": false`` without removing the line.
    """

    name: str
    command: list[str]
    env: dict[str, str] | None = None
    cwd: str | None = None
    enabled: bool = True


def load_mcp_server_specs(path: str | Path) -> list[McpServerSpec]:
    """Parse a JSONL of MCP server specs.

    One JSON object per line; blank lines and ``#`` comments are
    ignored so the file is still human-editable. Malformed lines are
    logged and skipped — we don't want one bad line to kill MCP
    support entirely.

    Returns an empty list when the path doesn't exist. Makes the
    common "feature off" case indistinguishable from "feature on but
    no servers declared" from the caller's perspective, which is
    what we want.
    """
    p = Path(path)
    if not p.exists():
        return []

    specs: list[McpServerSpec] = []
    seen: set[str] = set()
    for lineno, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning(
                "mcp config %s:%d invalid JSON: %s", p, lineno, exc
            )
            continue

        if not isinstance(obj, dict):
            logger.warning("mcp config %s:%d not an object", p, lineno)
            continue
        name = obj.get("name")
        command = obj.get("command")
        if not isinstance(name, str) or not name:
            logger.warning(
                "mcp config %s:%d missing/empty 'name'", p, lineno
            )
            continue
        if not isinstance(command, list) or not all(
            isinstance(x, str) for x in command
        ):
            logger.warning(
                "mcp config %s:%d 'command' must be list[str]", p, lineno
            )
            continue
        if name in seen:
            logger.warning(
                "mcp config %s:%d duplicate server name %r; ignoring",
                p,
                lineno,
                name,
            )
            continue
        seen.add(name)

        env_raw = obj.get("env")
        env: dict[str, str] | None = None
        if isinstance(env_raw, dict):
            env = {str(k): str(v) for k, v in env_raw.items()}

        cwd_raw = obj.get("cwd")
        cwd = str(cwd_raw) if isinstance(cwd_raw, str) and cwd_raw else None

        specs.append(
            McpServerSpec(
                name=name,
                command=list(command),
                env=env,
                cwd=cwd,
                enabled=bool(obj.get("enabled", True)),
            )
        )
    return specs


# ``McpAdapterFactory`` is injected to decouple the wiring code from
# the concrete ``StdioMcpTransport`` — tests can substitute an
# in-memory factory, and a future HTTP-SSE transport can be added
# without touching this module.
McpAdapterFactory = Callable[
    [McpServerSpec, float],  # (spec, call_timeout) → adapter
    Awaitable[object],
]


async def build_mcp_adapters(
    specs: list[McpServerSpec],
    *,
    factory: McpAdapterFactory,
    call_timeout_seconds: float,
) -> list[object]:
    """Instantiate and ``connect()`` each enabled MCP adapter.

    Failed adapters are logged and skipped rather than aborting the
    whole boot. One MCP server being down shouldn't stop the bot from
    answering regular questions — the per-tool error the agent gets
    when calling that server will surface the issue instead.

    Returns a list of ``McpToolAdapter``-shaped objects (typed as
    ``object`` to keep this module importable without pulling in
    ``mcp_tool_adapter`` unless actually used). Call
    ``close_mcp_adapters`` when the session finishes.
    """
    adapters: list[object] = []
    for spec in specs:
        if not spec.enabled:
            continue
        try:
            adapter = await factory(spec, call_timeout_seconds)
        except Exception:
            logger.warning(
                "failed to build MCP adapter %r; skipping", spec.name, exc_info=True
            )
            continue
        adapters.append(adapter)
    return adapters


async def close_mcp_adapters(adapters: list[object]) -> None:
    """Best-effort shutdown of a list of MCP adapters."""
    for adapter in adapters:
        close = getattr(adapter, "close", None)
        if close is None:
            continue
        try:
            result = close()
            if hasattr(result, "__await__"):
                await result
        except Exception:
            logger.warning(
                "failed to close MCP adapter %r",
                getattr(adapter, "server_name", "?"),
                exc_info=True,
            )
