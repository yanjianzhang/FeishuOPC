"""B-2 partitioner contract tests.

The partitioner is a pure function so these tests lean on a small
helper that builds fake OpenAI-style ``tool_calls`` and an
``AgentToolSpec`` index, then asserts the three invariants spelled
out in the module docstring (order preservation, world-isolation,
concurrency-group uniqueness per concurrent group).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from feishu_agent.core.agent_types import AgentToolSpec
from feishu_agent.core.tool_fanout import (
    PartitionGroup,
    partition_by_effect,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec(name: str, effect: str) -> AgentToolSpec:
    return AgentToolSpec(
        name=name,
        description="",
        input_schema={"type": "object"},
        effect=effect,
    )


def _tc(name: str, cid: str | None = None) -> dict:
    return {
        "id": cid or f"call-{name}",
        "function": {"name": name, "arguments": "{}"},
    }


def _run(
    calls: list[dict],
    specs: dict[str, AgentToolSpec],
    cg: Callable[[dict], str | None] | None = None,
) -> list[PartitionGroup]:
    return partition_by_effect(
        calls,
        specs=specs,
        concurrency_group_of=cg or (lambda _tc: None),
    )


# ---------------------------------------------------------------------------
# Invariant helpers (reused across cases)
# ---------------------------------------------------------------------------


def _assert_order_preserved(
    calls: list[dict], groups: list[PartitionGroup]
) -> None:
    flattened = [tc for g in groups for tc in g.calls]
    assert flattened == calls, "order preservation violated"


def _assert_world_isolated(groups: list[PartitionGroup]) -> None:
    for g in groups:
        world_like = [tc for tc in g.calls if tc["function"]["name"] == "world"]
        if world_like:
            assert g.size == 1, "world-effect call was grouped"
            assert g.mode == "serial"


# ---------------------------------------------------------------------------
# Table-driven happy-path cases
# ---------------------------------------------------------------------------


def test_two_reads_collapse_into_one_concurrent_group() -> None:
    specs = {"read": _spec("read", "read")}
    calls = [_tc("read", "a"), _tc("read", "b")]
    groups = _run(calls, specs)
    assert len(groups) == 1
    assert groups[0].mode == "concurrent"
    assert groups[0].size == 2
    _assert_order_preserved(calls, groups)


def test_single_world_yields_serial_group() -> None:
    specs = {"world": _spec("world", "world")}
    calls = [_tc("world")]
    groups = _run(calls, specs)
    assert len(groups) == 1
    assert groups[0].mode == "serial"
    _assert_world_isolated(groups)


def test_read_then_world_splits_into_two_groups() -> None:
    specs = {
        "read": _spec("read", "read"),
        "world": _spec("world", "world"),
    }
    calls = [_tc("read"), _tc("world")]
    groups = _run(calls, specs)
    assert [g.mode for g in groups] == ["concurrent", "serial"]
    _assert_order_preserved(calls, groups)


def test_world_then_read_splits_into_two_groups() -> None:
    specs = {
        "read": _spec("read", "read"),
        "world": _spec("world", "world"),
    }
    calls = [_tc("world"), _tc("read")]
    groups = _run(calls, specs)
    assert [g.mode for g in groups] == ["serial", "concurrent"]
    _assert_order_preserved(calls, groups)


def test_mixed_sequence_preserves_order_and_isolates_world() -> None:
    specs = {
        "read": _spec("read", "read"),
        "world": _spec("world", "world"),
    }
    calls = [
        _tc("read", "r1"),
        _tc("read", "r2"),
        _tc("world", "w1"),
        _tc("read", "r3"),
    ]
    groups = _run(calls, specs)
    assert [g.mode for g in groups] == ["concurrent", "serial", "concurrent"]
    assert [g.size for g in groups] == [2, 1, 1]
    _assert_order_preserved(calls, groups)
    _assert_world_isolated(groups)


# ---------------------------------------------------------------------------
# Concurrency-group semantics
# ---------------------------------------------------------------------------


def test_distinct_dispatch_groups_stay_concurrent() -> None:
    specs = {"dispatch": _spec("dispatch", "read")}
    calls = [_tc("dispatch", "d1"), _tc("dispatch", "d2")]
    groups_by_id = {"d1": "roleA", "d2": "roleB"}
    groups = _run(
        calls, specs, cg=lambda tc: groups_by_id[tc["id"]]
    )
    assert len(groups) == 1
    assert groups[0].mode == "concurrent"
    assert groups[0].size == 2


def test_colliding_dispatch_groups_serialize_the_second() -> None:
    specs = {"dispatch": _spec("dispatch", "read")}
    calls = [_tc("dispatch", "d1"), _tc("dispatch", "d2")]
    groups = _run(
        calls, specs, cg=lambda _tc: "roleX"
    )
    # d1 enters the concurrent buffer claiming roleX; d2 collides →
    # flush buffer as concurrent (with d1 alone), then d2 goes serial.
    assert [g.mode for g in groups] == ["concurrent", "serial"]
    assert [[tc["id"] for tc in g.calls] for g in groups] == [["d1"], ["d2"]]
    _assert_order_preserved(calls, groups)


def test_colliding_group_allows_third_with_different_group() -> None:
    """After the collision flush, a new concurrent buffer starts
    fresh — a later call with a distinct group can still parallelize
    with subsequent safe calls."""
    specs = {"dispatch": _spec("dispatch", "read")}
    calls = [
        _tc("dispatch", "d1"),
        _tc("dispatch", "d2"),
        _tc("dispatch", "d3"),
    ]
    per_id = {"d1": "X", "d2": "X", "d3": "Y"}
    groups = _run(calls, specs, cg=lambda tc: per_id[tc["id"]])
    # d1 → buffer. d2 collides with X → flush [d1] concurrent, emit
    # [d2] serial. d3 starts new buffer with Y → flushed at end as
    # concurrent.
    assert [g.mode for g in groups] == [
        "concurrent",
        "serial",
        "concurrent",
    ]
    assert [[tc["id"] for tc in g.calls] for g in groups] == [
        ["d1"],
        ["d2"],
        ["d3"],
    ]


# ---------------------------------------------------------------------------
# Conservative defaults
# ---------------------------------------------------------------------------


def test_unknown_tool_defaults_to_world() -> None:
    """A call whose name isn't in the spec index must be treated as
    world-effecting (safe default). Prevents an accidentally unknown
    mutating tool from being parallelized."""
    calls = [_tc("read"), _tc("mystery"), _tc("read")]
    specs = {"read": _spec("read", "read")}
    groups = _run(calls, specs)
    assert [g.mode for g in groups] == [
        "concurrent",
        "serial",
        "concurrent",
    ]
    assert [[tc["function"]["name"] for tc in g.calls] for g in groups] == [
        ["read"],
        ["mystery"],
        ["read"],
    ]


def test_self_effect_is_concurrent_with_reads() -> None:
    specs = {
        "plan": _spec("plan", "self"),
        "read": _spec("read", "read"),
    }
    calls = [_tc("plan"), _tc("read"), _tc("plan")]
    groups = _run(calls, specs)
    assert len(groups) == 1
    assert groups[0].size == 3
    assert groups[0].mode == "concurrent"


def test_empty_input_returns_empty_list() -> None:
    assert _run([], {}) == []


# ---------------------------------------------------------------------------
# Alternate call shapes
# ---------------------------------------------------------------------------


def test_non_dict_call_uses_attribute_tool_name() -> None:
    class FakeCall:
        def __init__(self, name: str) -> None:
            self.tool_name = name

    specs = {"read": _spec("read", "read")}
    calls = [FakeCall("read"), FakeCall("read")]
    groups = partition_by_effect(
        calls,
        specs=specs,
        concurrency_group_of=lambda _c: None,
    )
    assert len(groups) == 1
    assert groups[0].mode == "concurrent"
    assert groups[0].size == 2


def test_override_tool_name_extractor() -> None:
    specs = {"read": _spec("read", "read")}

    def _name(tc: dict) -> str:
        return tc["custom_name"]

    calls = [{"custom_name": "read", "id": "a"}]
    groups = partition_by_effect(
        calls,
        specs=specs,
        concurrency_group_of=lambda _tc: None,
        tool_name_of=_name,
    )
    assert groups[0].mode == "concurrent"


# ---------------------------------------------------------------------------
# Parametrised invariant sweep
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "effects",
    [
        ["read", "read", "read", "read"],
        ["world", "world", "world"],
        ["read", "world", "read", "world"],
        ["self", "read", "world", "read", "self"],
    ],
)
def test_invariants_hold_for_random_sequences(
    effects: list[str],
) -> None:
    specs = {name: _spec(name, name) for name in set(effects)}
    calls = [_tc(e, f"id-{i}") for i, e in enumerate(effects)]
    groups = _run(calls, specs)
    _assert_order_preserved(calls, groups)
    _assert_world_isolated(groups)
