"""Unit tests for :class:`DeployEngineerExecutor`.

The executor is a thin dispatcher around :class:`DeployService`. These
tests stub the service so we can assert:

1. ``describe_deploy_project`` surfaces metadata and maps service
   errors to ``{"ok": false, "error": ...}`` payloads.
2. ``deploy_project`` routes arguments / timeout into ``DeployService.run``,
   returns ``log_path`` + ``elapsed_human``, and converts ``DeployError``
   subclasses into structured error returns the caller can classify.

We deliberately do NOT exercise the real subprocess path here — that
lives in ``test_deploy_service.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from feishu_agent.roles.role_executors.deploy_engineer import (
    DeployEngineerExecutor,
)
from feishu_agent.tools.deploy_service import (
    DeployArgRejectedError,
    DeployResult,
)


@dataclass
class _RunCall:
    agent_name: str
    project_id: str
    args: list[str]
    timeout_seconds: int | None


class _StubDeployService:
    """Minimal stand-in for :class:`DeployService`.

    Only implements the three methods the executor touches:
    ``describe``, ``run``, and (implicitly, via absence) ``is_deployable``.
    """

    def __init__(
        self,
        *,
        describe_payload: dict[str, Any] | None = None,
        describe_exception: Exception | None = None,
        run_result: DeployResult | None = None,
        run_exception: Exception | None = None,
    ) -> None:
        self._describe_payload = describe_payload
        self._describe_exception = describe_exception
        self._run_result = run_result
        self._run_exception = run_exception
        self.run_calls: list[_RunCall] = []
        self.describe_calls: list[str] = []

    def describe(self, project_id: str) -> dict[str, Any]:
        self.describe_calls.append(project_id)
        if self._describe_exception is not None:
            raise self._describe_exception
        return dict(self._describe_payload or {})

    def run(
        self,
        *,
        agent_name: str,
        project_id: str,
        args: list[str],
        timeout_seconds: int | None,
    ) -> DeployResult:
        self.run_calls.append(
            _RunCall(
                agent_name=agent_name,
                project_id=project_id,
                args=list(args),
                timeout_seconds=timeout_seconds,
            )
        )
        if self._run_exception is not None:
            raise self._run_exception
        assert self._run_result is not None
        return self._run_result


# ---------------------------------------------------------------------------
# describe_deploy_project
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_deploy_project_returns_metadata() -> None:
    stub = _StubDeployService(
        describe_payload={
            "project_id": "exampleapp",
            "script_path": "deploy/deploy.sh",
            "script_absolute": "/repos/exampleapp/deploy/deploy.sh",
            "script_exists": True,
            "default_args": [],
            "supported_flags": [
                {"flag": "--server-only", "description": "只发后端"}
            ],
            "default_timeout_seconds": 1800,
            "max_timeout_seconds": 3600,
            "notes": "",
        },
    )
    executor = DeployEngineerExecutor(
        deploy_service=stub, project_id="exampleapp"
    )

    result = await executor.execute_tool("describe_deploy_project", {})

    assert isinstance(result, dict)
    assert result["ok"] is True
    assert result["project_id"] == "exampleapp"
    assert result["supported_flags"][0]["flag"] == "--server-only"
    assert stub.describe_calls == ["exampleapp"]


@pytest.mark.asyncio
async def test_describe_deploy_project_without_service_returns_disabled() -> None:
    executor = DeployEngineerExecutor(
        deploy_service=None, project_id="exampleapp"
    )

    result = await executor.execute_tool("describe_deploy_project", {})

    assert isinstance(result, dict)
    assert result["ok"] is False
    assert result["error"] == "DEPLOY_SERVICE_DISABLED"


@pytest.mark.asyncio
async def test_describe_deploy_project_without_project_id_returns_unknown() -> None:
    executor = DeployEngineerExecutor(
        deploy_service=_StubDeployService(), project_id=""
    )

    result = await executor.execute_tool("describe_deploy_project", {})

    assert isinstance(result, dict)
    assert result["ok"] is False
    assert result["error"] == "UNKNOWN_PROJECT"


# ---------------------------------------------------------------------------
# deploy_project
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_project_returns_verdict_and_log_path() -> None:
    stub = _StubDeployService(
        run_result=DeployResult(
            project_id="exampleapp",
            argv=("--server-only",),
            exit_code=0,
            success=True,
            stdout_tail="done",
            stderr_tail="",
            elapsed_ms=134_000,
            log_path=".larkagent/logs/deploy/exampleapp-20260420.log",
            command="/bin/bash deploy/deploy.sh --server-only",
            script_path="deploy/deploy.sh",
        )
    )
    executor = DeployEngineerExecutor(
        deploy_service=stub,
        project_id="exampleapp",
        role_name="deploy_engineer",
    )

    result = await executor.execute_tool(
        "deploy_project",
        {"args": ["--server-only"], "timeout_seconds": 900},
    )

    assert isinstance(result, dict)
    assert result["ok"] is True
    assert result["success"] is True
    assert result["exit_code"] == 0
    assert result["log_path"].endswith("exampleapp-20260420.log")
    assert result["elapsed_human"] == "2m 14s"
    # The executor must forward argv, agent_name, and timeout unchanged.
    assert len(stub.run_calls) == 1
    call = stub.run_calls[0]
    assert call.project_id == "exampleapp"
    assert call.agent_name == "deploy_engineer"
    assert call.args == ["--server-only"]
    assert call.timeout_seconds == 900


@pytest.mark.asyncio
async def test_deploy_project_surfaces_deploy_error_as_structured_failure() -> None:
    stub = _StubDeployService(
        run_exception=DeployArgRejectedError(
            "deploy argv rejected: '--server-only;rm'"
        )
    )
    executor = DeployEngineerExecutor(
        deploy_service=stub, project_id="exampleapp"
    )

    result = await executor.execute_tool(
        "deploy_project",
        {"args": ["--server-only;rm"]},
    )

    assert isinstance(result, dict)
    assert result["ok"] is False
    assert result["error"] == "DEPLOY_ARG_REJECTED"
    assert result["project_id"] == "exampleapp"
    assert len(stub.run_calls) == 1


@pytest.mark.asyncio
async def test_deploy_project_without_project_id_returns_unknown_project() -> None:
    stub = _StubDeployService()
    executor = DeployEngineerExecutor(deploy_service=stub, project_id="")

    result = await executor.execute_tool("deploy_project", {"args": []})

    assert isinstance(result, dict)
    assert result["ok"] is False
    assert result["error"] == "UNKNOWN_PROJECT"
    assert stub.run_calls == []


@pytest.mark.asyncio
async def test_unsupported_tool_raises_runtime_error() -> None:
    executor = DeployEngineerExecutor(
        deploy_service=_StubDeployService(), project_id="exampleapp"
    )

    with pytest.raises(RuntimeError, match="Unsupported tool"):
        await executor.execute_tool("bogus_tool", {})


# ---------------------------------------------------------------------------
# _format_duration_ms — boundary cases for the copy-pasted helper
# ---------------------------------------------------------------------------


def test_format_duration_ms_seconds_only() -> None:
    from feishu_agent.roles.role_executors.deploy_engineer import (
        _format_duration_ms,
    )

    assert _format_duration_ms(0) == "0s"
    assert _format_duration_ms(500) == "1s"  # rounds up
    assert _format_duration_ms(59_999) == "1m 0s"  # rounds to 60s → enters minutes branch
    assert _format_duration_ms(45_000) == "45s"


def test_format_duration_ms_minutes_and_seconds() -> None:
    from feishu_agent.roles.role_executors.deploy_engineer import (
        _format_duration_ms,
    )

    assert _format_duration_ms(60_000) == "1m 0s"
    assert _format_duration_ms(134_000) == "2m 14s"
    assert _format_duration_ms(3_600_000) == "60m 0s"


def test_format_duration_ms_negative_clamped_to_zero() -> None:
    from feishu_agent.roles.role_executors.deploy_engineer import (
        _format_duration_ms,
    )

    assert _format_duration_ms(-500) == "0s"
