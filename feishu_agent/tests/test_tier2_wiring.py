"""Tests for ``tier2_wiring`` — the glue between per-message Feishu
sessions and the tier-2 services.

The helpers here are small, but they're on the hot path of every
Feishu message. A subtle bug (wrong cancel key, swallowed lineage
event, half-connected MCP adapter on boot failure) would silently
degrade the runtime.

These tests cover each helper in isolation with in-memory fixtures,
so we catch wiring regressions without spinning up a full bot.
"""

from __future__ import annotations

import pytest

from feishu_agent.core.cancel_token import (
    CancelTokenRegistry,
    SessionCancelledError,
)
from feishu_agent.team.audit_service import AuditService
from feishu_agent.team.tier2_wiring import (
    McpServerSpec,
    allocate_runtime_context,
    attach_lineage_audit,
    build_mcp_adapters,
    cancel_key_for,
    close_mcp_adapters,
    is_live_cancel_command,
    load_mcp_server_specs,
    release_runtime_context,
)

# ---------------------------------------------------------------------------
# Cancel command detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("取消", True),
        ("停", True),
        ("停下", True),
        ("stop", True),
        ("STOP", True),  # case-insensitive
        ("Cancel!", True),  # trailing punctuation ok
        ("@bot 取消", True),  # mention prefix stripped
        ("@tech_lead 停", True),
        ("@bot @other 取消", True),  # multiple mentions
        ("取消！", True),
        # Negatives — these would be over-matches if we did substring:
        ("不取消", False),  # "not cancel"
        ("我想取消任务", False),  # embedded
        ("取消一下上次的计划", False),  # discussion, not command
        ("", False),
        ("   ", False),
    ],
)
def test_is_live_cancel_command(text, expected):
    """Cancel-command detection is deliberately strict to avoid false
    positives. Over-matching would silently kill an active session
    just because the user mentioned the word."""
    assert is_live_cancel_command(text) is expected


# ---------------------------------------------------------------------------
# Cancel key construction
# ---------------------------------------------------------------------------


def test_cancel_key_for_normalizes_none_fields():
    """Missing thread_id / chat_id must not produce a ``None`` in the
    key — the registry compares by equality, and ``None != ""`` would
    silently miss the match between cancel request and running
    session."""
    k = cancel_key_for(bot_name="tl", chat_id=None, thread_id=None)
    assert k.chat_id == ""
    assert k.thread_id == ""
    assert k.bot_name == "tl"


def test_cancel_key_distinguishes_threads():
    a = cancel_key_for(bot_name="tl", chat_id="c", thread_id="t1")
    b = cancel_key_for(bot_name="tl", chat_id="c", thread_id="t2")
    assert a != b


# ---------------------------------------------------------------------------
# allocate / release runtime context
# ---------------------------------------------------------------------------


def test_allocate_registers_cancel_token():
    reg = CancelTokenRegistry()
    ctx = allocate_runtime_context(
        bot_name="tl",
        chat_id="c1",
        thread_id="t1",
        trace_id="tracefoo",
        registry=reg,
    )
    # Cancel via registry flows through to the token we got back.
    assert reg.cancel(ctx.cancel_key, reason="user_cancel") is True
    with pytest.raises(SessionCancelledError):
        ctx.cancel_token.check()


def test_release_clears_registration():
    reg = CancelTokenRegistry()
    ctx = allocate_runtime_context(
        bot_name="tl",
        chat_id="c1",
        thread_id="t1",
        trace_id="tracefoo",
        registry=reg,
    )
    release_runtime_context(ctx, registry=reg)
    # After release, cancel should report "no session" (False).
    assert reg.cancel(ctx.cancel_key) is False


def test_release_is_idempotent():
    """Calling ``release_runtime_context`` twice (from success +
    exception paths both hitting the cleanup) must not raise — that
    would mask the original error."""
    reg = CancelTokenRegistry()
    ctx = allocate_runtime_context(
        bot_name="tl", chat_id="c", thread_id="t", trace_id="tid", registry=reg
    )
    release_runtime_context(ctx, registry=reg)
    release_runtime_context(ctx, registry=reg)  # must not raise


def test_allocate_primes_lineage_tracker_with_root():
    ctx = allocate_runtime_context(
        bot_name="tl",
        chat_id="c",
        thread_id="t",
        trace_id="root123",
        root_role="tech_lead",
        registry=CancelTokenRegistry(),
    )
    root = ctx.lineage.get("root123")
    assert root is not None
    assert root.role == "tech_lead"
    assert root.parent_trace_id is None


# ---------------------------------------------------------------------------
# Lineage audit subscriber
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attach_lineage_audit_writes_on_root_end(tmp_path):
    """Only the root session's ``on_session_end`` triggers a write.
    Sub-agent session ends must NOT write, or every message would
    produce many near-identical audit files."""
    ctx = allocate_runtime_context(
        bot_name="tl",
        chat_id="c",
        thread_id="t",
        trace_id="rootend",
        registry=CancelTokenRegistry(),
    )
    ctx.lineage.spawn_child(
        parent_trace_id="rootend", child_trace_id="childA", role="reviewer"
    )
    audit = AuditService(tmp_path)
    attach_lineage_audit(
        bus=ctx.hook_bus,
        tracker=ctx.lineage,
        audit_service=audit,
        root_trace_id="rootend",
    )

    # Fire a NON-root end — no audit file should appear.
    await ctx.hook_bus.afire("on_session_end", {"trace_id": "childA", "ok": True})
    assert not (tmp_path / "rootend-lineage.json").exists()

    # Now fire the root end — file appears with both nodes listed.
    await ctx.hook_bus.afire(
        "on_session_end", {"trace_id": "rootend", "ok": True}
    )
    persisted = audit.read("rootend-lineage")
    assert persisted is not None
    assert persisted["root_trace_id"] == "rootend"
    trace_ids = {n["trace_id"] for n in persisted["nodes"]}
    assert {"rootend", "childA"} <= trace_ids


# ---------------------------------------------------------------------------
# MCP server spec loader
# ---------------------------------------------------------------------------


def test_load_mcp_server_specs_missing_file_returns_empty(tmp_path):
    """Missing config is indistinguishable from "feature off" — the
    caller treats both as no-op, which keeps rollout safe."""
    assert load_mcp_server_specs(tmp_path / "nope.jsonl") == []


def test_load_mcp_server_specs_parses_valid_entries(tmp_path):
    path = tmp_path / "mcp.jsonl"
    path.write_text(
        "\n".join(
            [
                "# comment line",
                '{"name": "notes", "command": ["npx", "notes-mcp"]}',
                "",
                '{"name": "lark", "command": ["lark-cli", "mcp"], "env": {"K": "V"}, "cwd": "/tmp"}',
                '{"name": "disabled", "command": ["x"], "enabled": false}',
            ]
        ),
        encoding="utf-8",
    )
    specs = load_mcp_server_specs(path)
    assert [s.name for s in specs] == ["notes", "lark", "disabled"]
    assert specs[1].env == {"K": "V"}
    assert specs[1].cwd == "/tmp"
    assert specs[2].enabled is False


def test_load_mcp_server_specs_skips_bad_lines(tmp_path, caplog):
    """One bad line must not nuke the rest of the config — operators
    shouldn't lose all MCP servers because of a stray typo."""
    path = tmp_path / "mcp.jsonl"
    path.write_text(
        "\n".join(
            [
                "not-json",
                '{"name": 5, "command": ["x"]}',  # wrong type
                '{"name": "nocmd"}',  # missing command
                '{"name": "dup", "command": ["a"]}',
                '{"name": "dup", "command": ["b"]}',  # duplicate ignored
                '{"name": "ok", "command": ["y"]}',
            ]
        ),
        encoding="utf-8",
    )
    specs = load_mcp_server_specs(path)
    assert [s.name for s in specs] == ["dup", "ok"]


@pytest.mark.asyncio
async def test_build_mcp_adapters_skips_failing_factory(caplog):
    """A factory raising for one server must not abort the whole batch —
    the user should still get every server that did start."""

    async def factory(spec: McpServerSpec, timeout: float):
        if spec.name == "bad":
            raise RuntimeError("boom")
        return {"server_name": spec.name}

    specs = [
        McpServerSpec(name="good1", command=["x"]),
        McpServerSpec(name="bad", command=["y"]),
        McpServerSpec(name="good2", command=["z"]),
        McpServerSpec(name="off", command=["w"], enabled=False),
    ]
    adapters = await build_mcp_adapters(
        specs, factory=factory, call_timeout_seconds=1.0
    )
    names = [a["server_name"] for a in adapters]
    assert names == ["good1", "good2"]


@pytest.mark.asyncio
async def test_close_mcp_adapters_tolerates_sync_and_async_closes():
    """``close`` may be either sync or async on different adapter types
    (real MCP adapters are async; test doubles are often sync). The
    helper must handle both without caller branching."""
    closed: list[str] = []

    class AsyncClose:
        server_name = "a"

        async def close(self):
            closed.append("a")

    class SyncClose:
        server_name = "b"

        def close(self):
            closed.append("b")

    class NoClose:
        server_name = "c"

    await close_mcp_adapters([AsyncClose(), SyncClose(), NoClose()])
    assert closed == ["a", "b"]
