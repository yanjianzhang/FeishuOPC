"""Unit tests for the ⚠️ noise filter used by ``_build_tool_call_observer``.

The observer emits ⚠️ Feishu messages for genuine tool failures but
must stay silent for LLM-self-correctable fast-fails (missing pydantic
args, unknown workflow ids, tools outside the sub-agent allow-list).
Those errors still round-trip back to the model so it can retry, we
just don't pester the human user about them.
"""

from __future__ import annotations

from feishu_agent.roles.tech_lead_executor import _is_noisy_tool_error


def test_not_noisy_when_result_is_success():
    assert _is_noisy_tool_error({"ok": True, "path": "foo/bar"}) is False


def test_not_noisy_when_result_is_not_a_dict():
    assert _is_noisy_tool_error("literal string output") is False
    assert _is_noisy_tool_error([{"error": "x"}]) is False
    assert _is_noisy_tool_error(None) is False


def test_noisy_tool_call_arg_missing():
    assert (
        _is_noisy_tool_error(
            {
                "error": "TOOL_CALL_ARG_MISSING",
                "tool": "write_project_code",
                "missing_required_fields": ["content"],
            }
        )
        is True
    )


def test_noisy_tool_not_allowed_variants():
    assert (
        _is_noisy_tool_error(
            {"error": "TOOL_NOT_ALLOWED: dispatch_role_agent"}
        )
        is True
    )
    assert (
        _is_noisy_tool_error(
            {"error": "TOOL_NOT_ALLOWED_ON_ROLE: write_project_code"}
        )
        is True
    )


def test_noisy_unknown_workflow_variants():
    assert _is_noisy_tool_error({"error": "UNKNOWN_WORKFLOW"}) is True
    assert (
        _is_noisy_tool_error({"error": "Unknown workflow_id: 'bmad:typo'"})
        is True
    )


def test_noisy_unsupported_tool_runtime_error_wrap():
    # ``AgentToolExecutor.execute_tool`` raises ``RuntimeError("Unsupported
    # tool: X")`` when the tool name is unknown; the adapter wraps that
    # into ``{"error": str(exc)}``.
    assert (
        _is_noisy_tool_error({"error": "Unsupported tool: read_mystery"})
        is True
    )


def test_real_error_is_not_noisy():
    # Genuine, non-recoverable failures must still surface to Feishu.
    assert (
        _is_noisy_tool_error(
            {"error": "git push failed: permission denied (publickey)"}
        )
        is False
    )
    assert (
        _is_noisy_tool_error(
            {
                "error": "TOOL_VERIFICATION_FAILED",
                "tool": "write_project_code",
                "verification_error": "file did not change on disk",
            }
        )
        is False
    )
