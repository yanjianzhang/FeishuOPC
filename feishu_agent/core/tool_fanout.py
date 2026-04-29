"""B-2 effect-aware fan-out partitioner.

Groups a turn's ``tool_calls`` into a list of :class:`PartitionGroup`
segments, preserving the original request order. Each group carries a
``mode`` the orchestrator uses to decide whether to ``asyncio.gather``
the calls (``"concurrent"``) or run them one at a time (``"serial"``).

Partitioning rules — in priority order:

1. A call whose tool spec advertises ``effect == "world"`` is always
   serialized (its own group).
2. A call whose ``concurrency_group`` collides with one already queued
   in the current concurrent buffer is flushed out of the buffer and
   scheduled as a ``"serial"`` group by itself.
3. Everything else (effect in ``{"self", "read"}``) accumulates into a
   concurrent buffer that is emitted either when a world-effecting or
   colliding call arrives, or when the input is exhausted.

Correctness invariants (enforced by :func:`partition_by_effect` and
covered by :mod:`tests.test_partition_by_effect`):

-  Concatenating ``group.calls`` across groups reproduces the input
   exactly (length and order).
-  A ``"world"``-effect call is never grouped with any other call.
-  No two calls in a single concurrent group share the same
   non-``None`` concurrency_group.

The orchestrator is responsible for applying a semaphore to bound the
real gather width; the partitioner does NOT cap the group size. This
separation keeps the function pure and trivially testable: the
semaphore is an operational knob, the partition is a correctness
contract.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from feishu_agent.core.agent_types import AgentToolSpec

PartitionMode = Literal["concurrent", "serial"]


@dataclass(frozen=True)
class PartitionGroup:
    """A contiguous slice of tool calls sharing an execution mode.

    Groups of size 1 are allowed in both modes. The orchestrator
    typically treats a single-call concurrent group identically to a
    serial one (no gather overhead), but keeping the mode explicit
    lets tests assert the classification is correct.
    """

    calls: tuple[Any, ...]
    mode: PartitionMode = "concurrent"

    @property
    def size(self) -> int:
        return len(self.calls)


@dataclass
class _Builder:
    """Mutable accumulator used during partition construction.

    Extracted as a dataclass (rather than a closure-over-nonlocals) so
    the flush/emit logic is debuggable and so we can evolve it in a
    future change without rewriting the nonlocal-heavy original.
    """

    buf: list[Any] = field(default_factory=list)
    groups: list[PartitionGroup] = field(default_factory=list)
    seen_groups: set[str] = field(default_factory=set)

    def flush_concurrent(self) -> None:
        """Emit the pending concurrent buffer (if any) as a group.

        A single-call flush still emits a concurrent group — it's
        semantically equivalent to a serial one and keeps the
        order-preservation invariant simple.
        """
        if self.buf:
            self.groups.append(
                PartitionGroup(calls=tuple(self.buf), mode="concurrent")
            )
            self.buf = []

    def emit_serial(self, call: Any) -> None:
        self.groups.append(PartitionGroup(calls=(call,), mode="serial"))


def partition_by_effect(
    calls: Sequence[Any],
    specs: dict[str, AgentToolSpec],
    concurrency_group_of: Callable[[Any], str | None],
    tool_name_of: Callable[[Any], str] | None = None,
) -> list[PartitionGroup]:
    """Partition ``calls`` into fan-out groups.

    Parameters
    ----------
    calls:
        The raw tool-call dicts emitted by the LLM in a single turn.
        The partitioner does not assume a concrete shape; it extracts
        the tool name via ``tool_name_of`` so the same function works
        for both OpenAI chat-completions-style dicts
        (``{"function": {"name": ...}}``) and normalized
        :class:`AgentToolCall` objects.
    specs:
        Name-keyed mapping of the session's tool specs. A missing
        entry is treated as ``effect="world"`` — conservative because
        an unknown tool might be world-effecting and we'd rather lose
        a bit of parallelism than corrupt state.
    concurrency_group_of:
        Resolver that maps a tool call to its concurrency-group key
        (typically the role name, for ``dispatch_role_agent``).
        Return ``None`` for calls that can share a group with any
        other call.
    tool_name_of:
        Optional override for tool-name extraction. Defaults to the
        OpenAI chat-completions shape (``tc["function"]["name"]``)
        and falls back to an attribute on non-dict objects.

    Returns
    -------
    A list of :class:`PartitionGroup` segments covering every input
    call exactly once, in the original order.
    """
    b = _Builder()
    name_of = tool_name_of or _default_tool_name

    for tc in calls:
        name = name_of(tc) or ""
        spec = specs.get(name)
        effect = spec.effect if spec is not None else "world"
        cg = concurrency_group_of(tc)

        if effect in ("self", "read"):
            # Concurrency-group collision within the current concurrent
            # buffer → flush the buffer, run this call serially, and
            # DON'T re-add cg to seen_groups (it's already there).
            if cg is not None and cg in b.seen_groups:
                b.flush_concurrent()
                b.emit_serial(tc)
                continue

            if cg is not None:
                b.seen_groups.add(cg)
            b.buf.append(tc)
            continue

        # World-effecting call — break the concurrent run, emit alone.
        b.flush_concurrent()
        b.emit_serial(tc)
        if cg is not None:
            b.seen_groups.add(cg)

    b.flush_concurrent()
    return b.groups


def _default_tool_name(tc: Any) -> str:
    """Best-effort tool-name extraction for common call shapes.

    Kept intentionally tolerant: the partitioner is a pure function,
    and downstream code already validates tool-name presence before
    dispatch. Returning ``""`` here simply defaults the call to
    ``effect="world"`` treatment, which is the safe fallback.
    """
    if isinstance(tc, dict):
        fn = tc.get("function") or {}
        if isinstance(fn, dict):
            name = fn.get("name")
            if isinstance(name, str):
                return name
        raw = tc.get("name")
        if isinstance(raw, str):
            return raw
        return ""
    return getattr(tc, "tool_name", "") or ""
