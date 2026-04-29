"""Canonical runtime context passed to tools via ``needs=(…)`` injection.

Every decorated tool (see :mod:`feishu_agent.tools.tool_registry`)
can declare which runtime values it needs — ``needs=("task_id",\n"chat_id")`` — without exposing those fields to the LLM. The adapter
assembles a :class:`RequestContext` for the current turn and hands the
registry executor a dict view via :meth:`RequestContext.as_dict`; the
executor then injects exactly the requested keys.

Key rules
---------
- **Nothing here ends up in the LLM schema.** These values are
  adapter-supplied, tool-private.
- **Keys are strings, not attribute lookups.** Tools declare what
  they need by string name so a new context key can be introduced
  without changing every existing tool module.
- **Missing values are ``None``, not errors.** A tool that declares
  ``needs=("project_id",)`` but runs in a context without
  ``project_id`` gets ``project_id=None``. That is on purpose: an
  optional context param should not fail a whole turn, and tools
  that strictly need a value can validate it inline.

Canonical keys
--------------
- ``task_id`` — str, the per-thread task id (``TaskKey.task_id()``).
- ``task_handle`` — :class:`TaskHandle`, the in-memory handle for
  appending events / reading snapshots.
- ``chat_id`` — str, Feishu chat id where the conversation lives.
- ``root_id`` — str | None, Feishu thread root message id if any.
- ``project_id`` — str | None, logical project identifier.
- ``trace_id`` — str | None, correlation id used across services.
- ``bot_name`` — str | None, identity of the bot running the turn.
- ``role_name`` — str | None, semantic role (developer / tech_lead).
- ``thread_update_fn`` — Callable | None, an async callable tools can
  use to push progress updates into the Feishu thread without
  knowing about the ``FeishuRuntimeService`` — passed in by callers.

Any adapter-side helper that builds a context should set only the
keys it actually knows about and leave the rest implicit (``None``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

# The canonical names tools may list in their ``needs`` tuple. Kept
# as a literal-ish set so we can type-check / lint-check for typos in
# decorated-tool definitions. New names should be added here before
# they are used — this doubles as documentation.
CANONICAL_CONTEXT_KEYS: frozenset[str] = frozenset(
    {
        "task_id",
        "task_handle",
        "chat_id",
        "root_id",
        "project_id",
        "trace_id",
        "bot_name",
        "role_name",
        "thread_update_fn",
        # File-scoped world tools (write_file, etc.) receive the
        # allowed root as context so schemas don't leak filesystem
        # details to the LLM. Always a ``Path`` or ``None``.
        "allowed_write_root",
    }
)


ThreadUpdateFn = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class RequestContext:
    """One-turn snapshot of the runtime context tools may ask for.

    ``extra`` carries additional keys some tool families may rely on
    in the future (e.g. MCP-specific handles); it is merged into
    :meth:`as_dict` so ``needs=("extra_key",)`` works without adding
    a dedicated field up top. Use sparingly — prefer promoting a
    widely-used key to a named field so it appears in the canonical
    set and is discoverable.
    """

    task_id: str | None = None
    task_handle: Any = None  # TaskHandle, kept as Any to avoid cyclic import
    chat_id: str | None = None
    root_id: str | None = None
    project_id: str | None = None
    trace_id: str | None = None
    bot_name: str | None = None
    role_name: str | None = None
    thread_update_fn: ThreadUpdateFn | None = None
    allowed_write_root: Any = None  # pathlib.Path, typed Any to avoid import
    extra: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        """Flatten to a dict suitable for ``registry.build_executor(context=…)``.

        ``extra`` wins over named fields — the field-level entries are
        defaults that callers can override without having to construct
        a new RequestContext subclass.
        """
        base: dict[str, Any] = {
            "task_id": self.task_id,
            "task_handle": self.task_handle,
            "chat_id": self.chat_id,
            "root_id": self.root_id,
            "project_id": self.project_id,
            "trace_id": self.trace_id,
            "bot_name": self.bot_name,
            "role_name": self.role_name,
            "thread_update_fn": self.thread_update_fn,
            "allowed_write_root": self.allowed_write_root,
        }
        if self.extra:
            base.update(self.extra)
        return base


def validate_needs(needs: Iterable[str]) -> list[str]:
    """Return names in ``needs`` not covered by the canonical set.

    The registry does NOT reject unknown ``needs`` values — it just
    resolves them to ``None`` when the context dict doesn't have the
    key. This helper lets callers (tests, lint, pre-deploy sanity
    checks) surface typos early.
    """
    return sorted(set(needs) - CANONICAL_CONTEXT_KEYS)


__all__ = [
    "CANONICAL_CONTEXT_KEYS",
    "RequestContext",
    "ThreadUpdateFn",
    "validate_needs",
]
