"""Unit tests for CIWatchService.

We mock the ``gh`` subprocess via monkeypatch on
``feishu_agent.tools.ci_watch_service.subprocess.run`` so the tests are
hermetic — no real network, no real ``gh`` install required. Each test
controls (a) the exit code of the ``--watch`` invocation and (b) the JSON
returned by the follow-up ``--json`` call.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from feishu_agent.tools import ci_watch_service as ci_mod
from feishu_agent.tools.ci_watch_service import (
    CIWatchError,
    CIWatchProjectError,
    CIWatchService,
)


def _make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path)


@pytest.fixture
def service(repo: Path) -> CIWatchService:
    return CIWatchService(project_roots={"proj": repo})


# ---------------------------------------------------------------------------
# Test harness: a ``run`` stub keyed off whether ``--watch`` is in the argv.
# ---------------------------------------------------------------------------


def _stub_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    watch_exit: int,
    watch_stdout: str = "",
    watch_stderr: str = "",
    json_payload: list[dict[str, Any]] | None = None,
    json_exit: int = 0,
    raise_for_watch: type[BaseException] | None = None,
    raise_for_watch_exc: BaseException | None = None,
) -> list[list[str]]:
    """Patch ``ci_mod.subprocess.run`` and return the list of recorded argvs."""
    invocations: list[list[str]] = []

    class _Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(args, **kwargs):
        invocations.append(list(args))
        if "--watch" in args:
            if raise_for_watch_exc is not None:
                raise raise_for_watch_exc
            if raise_for_watch is not None:
                raise raise_for_watch("boom")
            return _Result(watch_exit, watch_stdout, watch_stderr)
        if "--json" in args:
            return _Result(
                json_exit, json.dumps(json_payload or []), ""
            )
        raise AssertionError(f"unexpected gh invocation: {args}")

    monkeypatch.setattr(ci_mod.subprocess, "run", fake_run)
    return invocations


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_watch_success(monkeypatch: pytest.MonkeyPatch, service: CIWatchService):
    invocations = _stub_run(monkeypatch, watch_exit=0)
    res = service.watch(project_id="proj", pr_number=42)
    assert res.status == "success"
    assert res.pr_number == 42
    assert res.failing_jobs == []
    assert "all CI checks passed" in res.summary
    # Single ``--watch`` call; no follow-up ``--json`` needed.
    assert sum(1 for a in invocations if "--watch" in a) == 1
    assert all("--json" not in a for a in invocations)


def test_watch_failure_returns_failing_jobs(
    monkeypatch: pytest.MonkeyPatch, service: CIWatchService
):
    invocations = _stub_run(
        monkeypatch,
        watch_exit=1,
        watch_stdout="X failed",
        json_payload=[
            {
                "name": "miniapp-typecheck",
                "workflow": "miniapp.yml",
                "state": "failure",
                "bucket": "fail",
                "link": "https://github.com/o/r/actions/runs/1",
                "description": "",
            },
            {
                # Passing job MUST be filtered out by bucket=fail.
                "name": "lint",
                "workflow": "lint.yml",
                "state": "success",
                "bucket": "pass",
                "link": "https://github.com/o/r/actions/runs/2",
                "description": "",
            },
        ],
    )
    res = service.watch(project_id="proj", pr_number=7)
    assert res.status == "failure"
    assert len(res.failing_jobs) == 1
    job = res.failing_jobs[0]
    assert job.name == "miniapp-typecheck"
    assert job.workflow == "miniapp.yml"
    assert job.link.endswith("/runs/1")
    # Both invocations recorded: --watch then --json.
    assert sum(1 for a in invocations if "--watch" in a) == 1
    assert sum(1 for a in invocations if "--json" in a) == 1


def test_watch_failure_with_no_parsed_failing_jobs(
    monkeypatch: pytest.MonkeyPatch, service: CIWatchService
):
    _stub_run(
        monkeypatch,
        watch_exit=2,  # genuinely unexpected non-zero
        watch_stderr="error: unauthorized",
        json_payload=[],  # follow-up returns nothing
    )
    res = service.watch(project_id="proj", pr_number=11)
    assert res.status == "failure"
    assert res.failing_jobs == []
    assert "no failing jobs were parsed" in res.summary
    assert res.reason and "exit code 2" in res.reason


# ---------------------------------------------------------------------------
# Timeout / pending paths
# ---------------------------------------------------------------------------


def test_watch_timeout_returns_timeout_status(
    monkeypatch: pytest.MonkeyPatch, service: CIWatchService
):
    _stub_run(
        monkeypatch,
        watch_exit=0,  # never used; we raise instead
        raise_for_watch_exc=subprocess.TimeoutExpired(cmd=["gh"], timeout=60),
        json_payload=[
            {
                "name": "still-pending",
                "workflow": "ci.yml",
                "state": "pending",
                "bucket": "pending",
                "link": "https://x/y/z",
                "description": "",
            }
        ],
    )
    res = service.watch(
        project_id="proj", pr_number=8, timeout_seconds=60
    )
    assert res.status == "timeout"
    assert res.failing_jobs == []
    # Helpful summary — operator wants to know what's still pending.
    assert "1 checks tracked" in res.summary
    assert res.reason and "timeout" in res.reason


def test_watch_exit_code_8_pending_maps_to_timeout(
    monkeypatch: pytest.MonkeyPatch, service: CIWatchService
):
    _stub_run(monkeypatch, watch_exit=8)
    res = service.watch(project_id="proj", pr_number=9)
    assert res.status == "timeout"
    assert res.reason and "exit code 8" in res.reason


# ---------------------------------------------------------------------------
# Unavailable (gh missing / unauthenticated — environmental degradation,
# NOT a programming error). Contract: skill tells the LLM to branch on
# ``result.status == "unavailable"``; if we raised here the branch would
# be dead.
# ---------------------------------------------------------------------------


def test_watch_gh_missing_returns_unavailable(
    monkeypatch: pytest.MonkeyPatch, service: CIWatchService
):
    _stub_run(
        monkeypatch,
        watch_exit=0,
        raise_for_watch=FileNotFoundError,
    )
    res = service.watch(project_id="proj", pr_number=1)
    assert res.status == "unavailable"
    assert res.pr_number == 1
    assert res.failing_jobs == []
    assert res.reason is not None
    # The reason must actually tell the operator how to fix it — a bare
    # "gh not found" with no remediation hint has bitten us before.
    assert "gh" in res.reason.lower()
    assert (
        "install" in res.reason.lower()
        or "token" in res.reason.lower()
    )


def test_watch_gh_unauthenticated_returns_unavailable(
    monkeypatch: pytest.MonkeyPatch, service: CIWatchService
):
    """``gh`` binary present but user not logged in: exit non-zero + a
    recognizable auth-error string on stderr. Must route to ``unavailable``
    so TL doesn't dispatch ``bug_fixer`` against a phantom code bug."""
    _stub_run(
        monkeypatch,
        watch_exit=4,
        watch_stderr="error: Not logged in. Try: gh auth login",
    )
    res = service.watch(project_id="proj", pr_number=1)
    assert res.status == "unavailable"
    assert res.failing_jobs == []
    assert res.reason is not None
    assert "auth" in res.reason.lower()


# ---------------------------------------------------------------------------
# Project / argument validation
# ---------------------------------------------------------------------------


def test_watch_unknown_project_raises_project_error(service: CIWatchService):
    with pytest.raises(CIWatchProjectError):
        service.watch(project_id="nope", pr_number=1)


def test_watch_zero_pr_number_raises_error(service: CIWatchService):
    with pytest.raises(CIWatchError):
        service.watch(project_id="proj", pr_number=0)


def test_non_git_project_raises_error(tmp_path: Path):
    not_a_repo = tmp_path / "nogit"
    not_a_repo.mkdir()
    svc = CIWatchService(project_roots={"proj": not_a_repo})
    with pytest.raises(CIWatchError):
        svc.watch(project_id="proj", pr_number=1)


# ---------------------------------------------------------------------------
# Result dataclass payload
# ---------------------------------------------------------------------------


def test_result_to_dict_has_stable_shape(
    monkeypatch: pytest.MonkeyPatch, service: CIWatchService
):
    _stub_run(monkeypatch, watch_exit=0)
    res = service.watch(project_id="proj", pr_number=42)
    payload = res.to_dict()
    # Stable contract for the LLM tool surface.
    assert set(payload.keys()) == {
        "status",
        "pr_number",
        "failing_jobs",
        "summary",
        "watched_seconds",
        "reason",
    }
    assert payload["status"] == "success"
    assert payload["pr_number"] == 42
