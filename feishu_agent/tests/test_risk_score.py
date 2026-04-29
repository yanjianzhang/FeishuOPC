"""Unit tests for :func:`compute_risk_score` (A-3 Wave 1, T042).

Scoring contract (from ``data-model.md`` §4):

* base ``= 0.08 × min(# world calls, 7.5) ≤ 0.6``
* external_bonus ``= 0.15 × min(# external calls, 2) ≤ 0.3``
* error_bonus ``= 0.2`` when ``success is False``
* final ``= min(base + external_bonus + error_bonus, 1.0)``

These tests pin each component independently and then verify
composition + clamping.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from feishu_agent.team.artifact_store import (
    RoleArtifact,
    ToolCallRecord,
    compute_risk_score,
)


@dataclass(frozen=True)
class _FakeSpec:
    """Minimal AgentToolSpec-shaped object so we don't have to
    import the real dataclass (keeps the test independent of the
    core/agent_types.py module position). ``compute_risk_score``
    only reads ``.effect`` and ``.target`` via ``getattr``."""

    effect: str
    target: str


def _artifact(
    *,
    tool_names: list[str],
    success: bool = True,
) -> RoleArtifact:
    return RoleArtifact(
        artifact_id="aid",
        parent_trace_id="p",
        root_trace_id="p",
        role_name="qa",
        task="",
        acceptance_criteria="",
        started_at=0,
        completed_at=1,
        duration_ms=1,
        success=success,
        stop_reason="complete" if success else "error",
        tool_calls=[
            ToolCallRecord(
                tool_name=n,
                arguments_preview="",
                result_preview="",
                duration_ms=0,
                is_error=False,
                started_at=0,
            )
            for n in tool_names
        ],
        token_usage={},
        output_text="",
        error_message=None,
        concurrency_group="",
    )


# ---------------------------------------------------------------------------
# Component isolation
# ---------------------------------------------------------------------------


def test_read_only_calls_score_zero() -> None:
    art = _artifact(tool_names=["read_a", "read_b", "read_c"])
    specs = {
        "read_a": _FakeSpec(effect="read", target="read.sprint"),
        "read_b": _FakeSpec(effect="read", target="read.bitable"),
        "read_c": _FakeSpec(effect="read", target="read.fs"),
    }
    assert compute_risk_score(art, specs_by_name=specs) == 0.0


def test_all_world_calls_hit_base_cap() -> None:
    """20 world calls saturate the base-cap at 0.6; no external /
    error bonus yet so total == 0.6."""
    art = _artifact(tool_names=[f"w{i}" for i in range(20)])
    specs = {
        f"w{i}": _FakeSpec(effect="world", target="world.fs.code")
        for i in range(20)
    }
    assert compute_risk_score(art, specs_by_name=specs) == pytest.approx(0.6)


def test_external_calls_add_bonus_up_to_cap() -> None:
    """Four world calls = 0.32 base. Four of those are also external
    => +0.3 (external cap). Success so no error bonus."""
    art = _artifact(tool_names=["git_push", "git_push", "bt_w", "feishu_notify"])
    specs = {
        "git_push": _FakeSpec(effect="world", target="world.git.remote.origin"),
        "bt_w": _FakeSpec(effect="world", target="world.bitable.write"),
        "feishu_notify": _FakeSpec(effect="world", target="world.feishu"),
    }
    score = compute_risk_score(art, specs_by_name=specs)
    # base = 0.08 * 4 = 0.32. external_bonus = min(0.15*4, 0.3) = 0.3.
    assert score == pytest.approx(0.32 + 0.3)


def test_failure_adds_twenty_points() -> None:
    art = _artifact(tool_names=["read_a"], success=False)
    specs = {"read_a": _FakeSpec(effect="read", target="read.sprint")}
    # base = 0, external = 0, error = 0.2.
    assert compute_risk_score(art, specs_by_name=specs) == pytest.approx(0.2)


def test_clamps_at_one() -> None:
    """20 world + 10 external + failure should push the raw sum
    above 1.0; the clamp must bring it back."""
    art = _artifact(
        tool_names=[f"w{i}" for i in range(20)]
        + [f"g{i}" for i in range(10)],
        success=False,
    )
    specs: dict[str, _FakeSpec] = {
        f"w{i}": _FakeSpec(effect="world", target="world.fs.code")
        for i in range(20)
    }
    specs.update(
        {
            f"g{i}": _FakeSpec(
                effect="world", target="world.git.remote.origin"
            )
            for i in range(10)
        }
    )
    assert compute_risk_score(art, specs_by_name=specs) == 1.0


# ---------------------------------------------------------------------------
# Defaults when no spec registry is supplied
# ---------------------------------------------------------------------------


def test_missing_registry_treats_every_call_as_world() -> None:
    """Without a spec lookup, the scorer can't know effect → it must
    assume the worst case (world) so the score doesn't silently
    understate risk. Covers the A-2 Wave-3-style regression where a
    caller forgets to thread the specs map through."""
    art = _artifact(tool_names=["anything_a", "anything_b"])
    # Each unknown call scores as world → base = 0.16; no external
    # targets since ``*`` doesn't match the prefixes; success so
    # no error bonus.
    assert compute_risk_score(art, specs_by_name=None) == pytest.approx(0.16)


def test_unknown_tool_in_registry_is_conservative() -> None:
    """If the map exists but the tool isn't in it (e.g. role
    references a freshly-registered bundle the registry fixture
    hasn't seen), treat it as world. Matches the ``None``-map
    default for consistency."""
    art = _artifact(tool_names=["mystery_tool"])
    assert (
        compute_risk_score(art, specs_by_name={"known": _FakeSpec("read", "*")})
        == pytest.approx(0.08)
    )


# ---------------------------------------------------------------------------
# Boundary: empty tool_calls on a successful run
# ---------------------------------------------------------------------------


def test_empty_tool_calls_successful_run_is_zero() -> None:
    """A dispatch that finished without invoking any tool (e.g. the
    LLM returned text only) should score 0.0 — nothing moved, no
    error."""
    art = _artifact(tool_names=[])
    assert compute_risk_score(art, specs_by_name={}) == 0.0


def test_empty_tool_calls_failed_run_scores_error_bonus() -> None:
    art = _artifact(tool_names=[], success=False)
    assert compute_risk_score(art, specs_by_name={}) == pytest.approx(0.2)
