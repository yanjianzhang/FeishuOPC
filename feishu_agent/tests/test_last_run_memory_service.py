"""Tests for :mod:`feishu_agent.team.last_run_memory_service`.

Covered behaviors:

- ``append`` + ``load_last`` round-trip for a single digest.
- ``append`` trims from the front once the history cap is reached.
- ``load_inject_target`` returns the last digest iff it is non-success
  ("clear on next success" policy).
- ``RunDigestCollector`` builds a digest from HookBus events and
  persists on ``on_session_end``.
- ``RunDigestCollector`` caps ``tool_calls`` at MAX_TOOL_CALLS_PER_DIGEST
  and bumps ``tool_calls_overflow``.
- ``flush_on_exception`` writes a failure record when the adapter
  raises before emitting ``on_session_end``.
- ``render_last_run_for_prompt`` renders the expected structural bits
  and is empty for ``None``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from feishu_agent.core.hook_bus import HookBus
from feishu_agent.team.last_run_memory_service import (
    LastRunMemoryService,
    RunDigest,
    RunDigestCollector,
    ToolCallSummary,
    render_last_run_for_prompt,
)


def _make_service(tmp_path: Path) -> LastRunMemoryService:
    return LastRunMemoryService(
        project_id="demo",
        project_root=tmp_path,
    )


def test_append_and_load_last_round_trip(tmp_path: Path):
    svc = _make_service(tmp_path)
    digest = RunDigest(
        trace_id="t1",
        started_at="2026-04-17T10:00:00+00:00",
        ended_at="2026-04-17T10:01:00+00:00",
        user_command="跑 story 3-1",
        stop_reason="end_turn",
        ok=True,
        tool_calls=[ToolCallSummary(name="spec_linker", ok=True, summary="linked 3-1")],
    )
    svc.append(digest)

    loaded = svc.load_last()
    assert loaded is not None
    assert loaded.trace_id == "t1"
    assert loaded.ok is True
    assert loaded.stop_reason == "end_turn"
    assert loaded.tool_calls[0].name == "spec_linker"
    assert loaded.tool_calls[0].summary == "linked 3-1"


def test_append_trims_to_history_cap(tmp_path: Path):
    svc = _make_service(tmp_path)
    svc.MAX_HISTORY_RECORDS = 3  # shrink for the test
    for i in range(5):
        svc.append(
            RunDigest(
                trace_id=f"t{i}",
                started_at="2026-04-17T10:00:00+00:00",
            )
        )
    lines = svc.history_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    # oldest (t0, t1) dropped; last one is newest.
    assert "t2" in lines[0]
    assert "t4" in lines[-1]


def test_load_inject_target_skips_last_success(tmp_path: Path):
    svc = _make_service(tmp_path)
    svc.append(
        RunDigest(
            trace_id="t-fail",
            started_at="x",
            stop_reason="error",
            ok=False,
            error_detail="timeout",
        )
    )
    # Still a failure on top → injected.
    target = svc.load_inject_target()
    assert target is not None
    assert target.trace_id == "t-fail"

    svc.append(
        RunDigest(
            trace_id="t-ok",
            started_at="y",
            stop_reason="end_turn",
            ok=True,
        )
    )
    # Success cleared the slate for the prompt.
    assert svc.load_inject_target() is None


def test_load_last_corrupt_line_is_skipped(tmp_path: Path):
    svc = _make_service(tmp_path)
    path = svc.history_path
    path.parent.mkdir(parents=True, exist_ok=True)
    good = RunDigest(trace_id="t-good", started_at="z", ok=True).to_json()
    path.write_text("not-json\n" + good + "\n", encoding="utf-8")
    last = svc.load_last()
    assert last is not None
    assert last.trace_id == "t-good"


@pytest.mark.asyncio
async def test_collector_persists_on_session_end(tmp_path: Path):
    svc = _make_service(tmp_path)
    bus = HookBus()
    collector = RunDigestCollector(
        service=svc,
        trace_id="t-collect",
        user_command="帮我跑 story 3-1",
    )
    collector.attach(bus)

    await bus.afire(
        "on_tool_call",
        {
            "trace_id": "t-collect",
            "tool_name": "spec_linker",
            "result": {"ok": True, "note": "linked 3-1"},
            "duration_ms": 42,
        },
    )
    await bus.afire(
        "on_tool_call",
        {
            "trace_id": "t-collect",
            "tool_name": "reviewer",
            "result": {"error": "LINT_FAILED", "detail": "3 errors"},
            "duration_ms": 120,
        },
    )
    await bus.afire(
        "on_session_end",
        {
            "trace_id": "t-collect",
            "model": "test",
            "ok": False,
            "stop_reason": "end_turn",  # success reason BUT ok=False → non-success
            "latency_ms": 2000,
        },
    )

    loaded = svc.load_last()
    assert loaded is not None
    assert loaded.trace_id == "t-collect"
    assert loaded.ok is False  # because ok=False in payload
    assert loaded.user_command == "帮我跑 story 3-1"
    assert len(loaded.tool_calls) == 2
    assert loaded.tool_calls[0].name == "spec_linker"
    assert loaded.tool_calls[0].ok is True
    assert loaded.tool_calls[1].name == "reviewer"
    assert loaded.tool_calls[1].ok is False
    # Error detail defaults from the last failing tool.
    assert loaded.error_detail is not None
    assert "LINT_FAILED" in loaded.error_detail


@pytest.mark.asyncio
async def test_collector_caps_tool_calls(tmp_path: Path):
    svc = _make_service(tmp_path)
    bus = HookBus()
    collector = RunDigestCollector(
        service=svc,
        trace_id="t-cap",
        user_command="x",
    )
    collector.attach(bus)

    n = LastRunMemoryService.MAX_TOOL_CALLS_PER_DIGEST + 4
    for i in range(n):
        await bus.afire(
            "on_tool_call",
            {
                "tool_name": f"tool_{i}",
                "result": {"ok": True, "note": "ok"},
                "duration_ms": 1,
            },
        )
    await bus.afire(
        "on_session_end",
        {"ok": True, "stop_reason": "end_turn", "latency_ms": 0},
    )

    loaded = svc.load_last()
    assert loaded is not None
    assert len(loaded.tool_calls) == LastRunMemoryService.MAX_TOOL_CALLS_PER_DIGEST
    assert loaded.tool_calls_overflow == 4


@pytest.mark.asyncio
async def test_collector_flush_on_exception_writes_failure_record(tmp_path: Path):
    svc = _make_service(tmp_path)
    bus = HookBus()
    collector = RunDigestCollector(
        service=svc,
        trace_id="t-exc",
        user_command="x",
    )
    collector.attach(bus)

    # A tool ran successfully, then the adapter died before on_session_end.
    await bus.afire(
        "on_tool_call",
        {
            "tool_name": "spec_linker",
            "result": {"ok": True, "note": "linked"},
            "duration_ms": 5,
        },
    )
    try:
        raise RuntimeError("simulated adapter failure")
    except RuntimeError as exc:
        collector.flush_on_exception(exc)

    loaded = svc.load_last()
    assert loaded is not None
    assert loaded.ok is False
    assert loaded.stop_reason == "exception"
    assert loaded.error_detail is not None
    assert (
        "simulated adapter failure" in loaded.error_detail
        or "spec_linker" in loaded.error_detail
    )


@pytest.mark.asyncio
async def test_collector_flush_on_exception_is_idempotent(tmp_path: Path):
    svc = _make_service(tmp_path)
    bus = HookBus()
    collector = RunDigestCollector(
        service=svc,
        trace_id="t-idem",
        user_command="x",
    )
    collector.attach(bus)

    await bus.afire(
        "on_session_end",
        {"ok": False, "stop_reason": "error", "latency_ms": 0},
    )
    # flush after on_session_end should be a no-op.
    collector.flush_on_exception(RuntimeError("should-not-record"))

    path = svc.history_path
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["stop_reason"] == "error"


def test_render_last_run_for_prompt_empty_for_none():
    assert render_last_run_for_prompt(None) == ""  # type: ignore[arg-type]


def test_render_last_run_for_prompt_has_key_lines():
    digest = RunDigest(
        trace_id="abc",
        started_at="2026-04-17T10:00:00+00:00",
        ended_at="2026-04-17T10:05:00+00:00",
        user_command="帮我跑 story 3-1",
        stop_reason="error",
        ok=False,
        error_detail="reviewer: 3 lint errors",
        tool_calls=[
            ToolCallSummary(name="spec_linker", ok=True, summary="linked 3-1"),
            ToolCallSummary(name="reviewer", ok=False, summary="3 errors"),
        ],
        tool_calls_overflow=2,
        git_state={"branch": "feature/3-1-seed", "head": "abc1234"},
    )
    text = render_last_run_for_prompt(digest)
    assert "## Last run context" in text
    assert "trace: `abc`" in text
    assert "帮我跑 story 3-1" in text
    assert "feature/3-1-seed" in text and "abc1234" in text
    assert "spec_linker" in text and "reviewer" in text
    assert "(+2 more" in text
    # The don't-redo-work instruction must be present.
    assert "不要重新执行" in text or "not re-execute" in text.lower()
