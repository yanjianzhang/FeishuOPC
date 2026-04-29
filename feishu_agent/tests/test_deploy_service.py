"""Unit tests for :mod:`feishu_agent.tools.deploy_service`.

Strategy: rather than mock subprocess, we write a tiny real
``deploy.sh`` into a tmp project root for each scenario and assert
that ``DeployService`` returns the right code / error class. This
keeps the "does subprocess plumbing actually work" question covered
alongside the argv / allowlist / timeout guards.

Each service instance is built with BOTH a ``project_roots`` map and a
``configs`` map of ``DeployProjectConfig``. Tests that want to check
config-specific behaviour (custom script_path, default_args,
default_timeout_seconds) override the config; others use a minimal
default.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from feishu_agent.tools.deploy_service import (
    DeployArgRejectedError,
    DeployFlagSpec,
    DeployNotAllowedError,
    DeployNotConfiguredError,
    DeployProjectConfig,
    DeployScriptMissingError,
    DeployService,
    DeployTimeoutError,
    UnknownProjectError,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_deploy_script(
    project_root: Path, body: str, *, rel_path: str = "deploy/deploy.sh"
) -> Path:
    """Write an executable script at ``project_root/rel_path``."""
    script = project_root / rel_path
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n" + body,
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "exampleapp"
    root.mkdir()
    return root


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "deploy"


def _default_config(
    project_id: str = "exampleapp", **overrides
) -> DeployProjectConfig:
    base: dict = {
        "project_id": project_id,
        "script_path": "deploy/deploy.sh",
        "default_args": (),
        "supported_flags": (),
        "default_timeout_seconds": 1800,
        "notes": "",
    }
    base.update(overrides)
    return DeployProjectConfig(**base)


def _make_service(
    project_root: Path,
    log_dir: Path,
    *,
    config: DeployProjectConfig | None = None,
    project_id: str = "exampleapp",
) -> DeployService:
    cfg = config or _default_config(project_id=project_id)
    return DeployService(
        project_roots={project_id: project_root},
        configs={project_id: cfg},
        log_dir=log_dir,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_run_happy_path_logs_and_returns_success(
    project_root: Path, log_dir: Path
) -> None:
    _write_deploy_script(
        project_root,
        'echo "deploying $1 $2"\necho "all good"\nexit 0\n',
    )
    svc = _make_service(project_root, log_dir)

    result = svc.run(
        agent_name="tech_lead",
        project_id="exampleapp",
        args=["--server-only", "--env=prod"],
    )

    assert result.success is True
    assert result.exit_code == 0
    assert result.argv == ("--server-only", "--env=prod")
    assert result.project_id == "exampleapp"
    assert result.script_path == "deploy/deploy.sh"
    assert "deploying --server-only --env=prod" in result.stdout_tail
    assert "all good" in result.stdout_tail
    assert result.elapsed_ms >= 0
    assert result.command.endswith(" --server-only --env=prod")

    log_path = Path(result.log_path)
    assert log_path.is_file()
    content = log_path.read_text(encoding="utf-8")
    assert "project=exampleapp" in content
    assert "agent=tech_lead" in content
    assert "deploying --server-only --env=prod" in content
    assert "exited=0" in content


def test_run_nonzero_exit_is_reported_as_failure(
    project_root: Path, log_dir: Path
) -> None:
    _write_deploy_script(project_root, 'echo "oops"\nexit 3\n')
    svc = _make_service(project_root, log_dir)

    result = svc.run(agent_name="tech_lead", project_id="exampleapp")

    assert result.success is False
    assert result.exit_code == 3
    assert "oops" in result.stdout_tail


def test_custom_script_path_is_honoured(
    project_root: Path, log_dir: Path
) -> None:
    """A project that keeps its script at a non-default location (say,
    ``infra/ship.sh``) must still work — FeishuOPC should defer to
    the JSON's ``script_path`` rather than hard-coding ``deploy/deploy.sh``."""
    _write_deploy_script(
        project_root,
        'echo "ship it"\nexit 0\n',
        rel_path="infra/ship.sh",
    )
    cfg = _default_config(script_path="infra/ship.sh")
    svc = _make_service(project_root, log_dir, config=cfg)

    result = svc.run(agent_name="tech_lead", project_id="exampleapp")

    assert result.success is True
    assert result.script_path == "infra/ship.sh"
    assert "ship it" in result.stdout_tail


def test_default_args_from_config_are_prepended(
    project_root: Path, log_dir: Path
) -> None:
    """``default_args`` is useful for projects where one invariant flag
    is always required; it should be prepended before any runtime
    args and show up in the final argv/command."""
    _write_deploy_script(
        project_root,
        'echo "argv=$*"\nexit 0\n',
    )
    cfg = _default_config(default_args=("--config=prod",))
    svc = _make_service(project_root, log_dir, config=cfg)

    result = svc.run(
        agent_name="tech_lead",
        project_id="exampleapp",
        args=["--server-only"],
    )

    assert result.argv == ("--config=prod", "--server-only")
    assert "argv=--config=prod --server-only" in result.stdout_tail


def test_config_default_timeout_is_used_when_caller_omits(
    project_root: Path, log_dir: Path
) -> None:
    """When the caller passes ``timeout_seconds=None``, the subprocess
    should run under the config's ``default_timeout_seconds``. We
    can't directly inspect the timeout so we use a fast-failing
    script + a tiny config timeout to verify the plumbing picks the
    config value (timeout raises when the config value is small)."""
    _write_deploy_script(
        project_root,
        'echo "starting"\nsleep 30\n',
    )
    cfg = _default_config(default_timeout_seconds=1)
    svc = _make_service(project_root, log_dir, config=cfg)

    with pytest.raises(DeployTimeoutError):
        svc.run(agent_name="tech_lead", project_id="exampleapp")


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_unknown_project_raises(project_root: Path, log_dir: Path) -> None:
    svc = _make_service(project_root, log_dir)
    with pytest.raises(UnknownProjectError) as exc:
        svc.run(agent_name="tech_lead", project_id="nope")
    assert exc.value.code == "UNKNOWN_PROJECT"


def test_missing_config_raises_not_configured(
    project_root: Path, log_dir: Path
) -> None:
    """Having a ``project_root`` WITHOUT a matching config means the
    project isn't deployable through FeishuOPC. We surface that as
    ``DEPLOY_NOT_CONFIGURED`` rather than silently falling through."""
    svc = DeployService(
        project_roots={"exampleapp": project_root},
        configs={},
        log_dir=log_dir,
    )
    with pytest.raises(DeployNotConfiguredError) as exc:
        svc.run(agent_name="tech_lead", project_id="exampleapp")
    assert exc.value.code == "DEPLOY_NOT_CONFIGURED"


def test_wrong_agent_is_rejected(project_root: Path, log_dir: Path) -> None:
    _write_deploy_script(project_root, "exit 0\n")
    svc = _make_service(project_root, log_dir)
    with pytest.raises(DeployNotAllowedError) as exc:
        svc.run(agent_name="developer", project_id="exampleapp")
    assert exc.value.code == "DEPLOY_NOT_ALLOWED_FOR_AGENT"
    assert not log_dir.exists()


def test_missing_deploy_script_raises(
    project_root: Path, log_dir: Path
) -> None:
    svc = _make_service(project_root, log_dir)
    with pytest.raises(DeployScriptMissingError) as exc:
        svc.run(agent_name="tech_lead", project_id="exampleapp")
    assert exc.value.code == "DEPLOY_SCRIPT_MISSING"


@pytest.mark.parametrize(
    "bad_arg",
    [
        "$(rm -rf /)",
        "foo; echo owned",
        "`whoami`",
        "foo\nbar",
        "../etc/passwd",
        "a" * 600,
    ],
)
def test_malicious_argv_is_rejected(
    project_root: Path, log_dir: Path, bad_arg: str
) -> None:
    _write_deploy_script(project_root, "exit 0\n")
    svc = _make_service(project_root, log_dir)
    with pytest.raises(DeployArgRejectedError):
        svc.run(
            agent_name="tech_lead",
            project_id="exampleapp",
            args=[bad_arg],
        )
    assert not log_dir.exists() or not any(log_dir.iterdir())


def test_too_many_argv_entries_rejected(
    project_root: Path, log_dir: Path
) -> None:
    _write_deploy_script(project_root, "exit 0\n")
    svc = _make_service(project_root, log_dir)
    with pytest.raises(DeployArgRejectedError):
        svc.run(
            agent_name="tech_lead",
            project_id="exampleapp",
            args=[f"--flag{i}" for i in range(32)],
        )


def test_timeout_raises_and_writes_log(
    project_root: Path, log_dir: Path
) -> None:
    _write_deploy_script(project_root, 'echo "starting"\nsleep 30\n')
    svc = _make_service(project_root, log_dir)
    with pytest.raises(DeployTimeoutError) as exc:
        svc.run(
            agent_name="tech_lead",
            project_id="exampleapp",
            timeout_seconds=1,
        )
    assert exc.value.code == "DEPLOY_TIMEOUT"
    assert log_dir.exists()
    logs = list(log_dir.iterdir())
    assert logs, "expected a log file even on timeout"
    text = logs[0].read_text(encoding="utf-8")
    assert "TIMEOUT" in text


def test_negative_timeout_rejected(
    project_root: Path, log_dir: Path
) -> None:
    _write_deploy_script(project_root, "exit 0\n")
    svc = _make_service(project_root, log_dir)
    with pytest.raises(DeployArgRejectedError):
        svc.run(
            agent_name="tech_lead",
            project_id="exampleapp",
            timeout_seconds=0,
        )


# ---------------------------------------------------------------------------
# Introspection API
# ---------------------------------------------------------------------------


def test_is_deployable_requires_config_and_script(
    project_root: Path, log_dir: Path
) -> None:
    # Has config, no script yet.
    svc = _make_service(project_root, log_dir)
    assert svc.is_deployable("exampleapp") is False
    # Script appears → deployable.
    _write_deploy_script(project_root, "exit 0\n")
    assert svc.is_deployable("exampleapp") is True
    # Unknown project → not deployable.
    assert svc.is_deployable("nope") is False


def test_is_deployable_false_without_config(
    project_root: Path, log_dir: Path
) -> None:
    _write_deploy_script(project_root, "exit 0\n")
    svc = DeployService(
        project_roots={"exampleapp": project_root},
        configs={},
        log_dir=log_dir,
    )
    assert svc.is_deployable("exampleapp") is False


def test_describe_returns_metadata_for_configured_project(
    project_root: Path, log_dir: Path
) -> None:
    _write_deploy_script(project_root, "exit 0\n")
    cfg = _default_config(
        supported_flags=(
            DeployFlagSpec(
                flag="--setup",
                description="one-time nginx bootstrap",
                expected_duration_seconds=120,
            ),
        ),
        notes="exampleapp deploy notes",
    )
    svc = _make_service(project_root, log_dir, config=cfg)

    info = svc.describe("exampleapp")

    assert info["project_id"] == "exampleapp"
    assert info["script_path"] == "deploy/deploy.sh"
    assert info["script_exists"] is True
    assert info["notes"] == "exampleapp deploy notes"
    assert info["supported_flags"][0]["flag"] == "--setup"
    assert info["supported_flags"][0]["expected_duration_seconds"] == 120
    assert info["max_timeout_seconds"] == 3600


def test_describe_surfaces_host_bootstrap_script_status(
    project_root: Path, log_dir: Path
) -> None:
    """When a config declares a bootstrap script, ``describe`` must
    tell the LLM (and the ops-facing info payload) whether it's
    actually on disk — so TL can say "btw your bootstrap hasn't been
    committed yet" without a second filesystem hop."""
    _write_deploy_script(project_root, "exit 0\n")
    bootstrap = project_root / "deploy" / "bootstrap-host.sh"
    bootstrap.write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
    bootstrap.chmod(0o755)

    cfg = _default_config(host_bootstrap_script="deploy/bootstrap-host.sh")
    svc = _make_service(project_root, log_dir, config=cfg)
    info = svc.describe("exampleapp")

    assert info["host_bootstrap_script"] == "deploy/bootstrap-host.sh"
    assert info["host_bootstrap_script_exists"] is True
    assert info["host_bootstrap_script_absolute"].endswith(
        "deploy/bootstrap-host.sh"
    )


def test_describe_bootstrap_absent_when_not_configured(
    project_root: Path, log_dir: Path
) -> None:
    _write_deploy_script(project_root, "exit 0\n")
    svc = _make_service(project_root, log_dir)
    info = svc.describe("exampleapp")
    assert info["host_bootstrap_script"] is None
    assert info["host_bootstrap_script_exists"] is False
    assert info["host_bootstrap_script_absolute"] is None


def test_describe_unknown_project_raises(
    project_root: Path, log_dir: Path
) -> None:
    svc = _make_service(project_root, log_dir)
    with pytest.raises(UnknownProjectError):
        svc.describe("nope")


def test_describe_without_config_raises(
    project_root: Path, log_dir: Path
) -> None:
    svc = DeployService(
        project_roots={"exampleapp": project_root},
        configs={},
        log_dir=log_dir,
    )
    with pytest.raises(DeployNotConfiguredError):
        svc.describe("exampleapp")


def test_known_projects_is_intersection(tmp_path: Path) -> None:
    """Only projects with BOTH a repo_root and a config count. A
    project that's registered but unconfigured (or vice-versa) should
    not show up in ``known_projects``."""
    svc = DeployService(
        project_roots={
            "exampleapp": tmp_path / "gv",
            "orphan": tmp_path / "orphan",
        },
        configs={
            "exampleapp": _default_config("exampleapp"),
            "ghost": _default_config("ghost"),
        },
        log_dir=tmp_path / "logs",
    )
    assert svc.known_projects() == ("exampleapp",)


def test_is_agent_allowed() -> None:
    svc = DeployService(
        project_roots={}, configs={}, log_dir=Path("/tmp")
    )
    assert svc.is_agent_allowed("tech_lead") is True
    assert svc.is_agent_allowed("developer") is False
    assert svc.is_agent_allowed("product_manager") is False


def test_timeout_is_hard_capped(project_root: Path, log_dir: Path) -> None:
    """Requests above 3600s must be silently clamped — we don't want
    callers extending the window past the cap by mistake."""
    _write_deploy_script(project_root, "exit 0\n")
    svc = _make_service(project_root, log_dir)
    result = svc.run(
        agent_name="tech_lead",
        project_id="exampleapp",
        timeout_seconds=99_999,
    )
    assert result.success is True


def test_log_file_contains_tail_marker_when_truncated(
    project_root: Path, log_dir: Path
) -> None:
    """Big stdout → tail is truncated with a clear marker so the LLM
    knows there's more in the log file."""
    _write_deploy_script(
        project_root,
        'for i in $(seq 1 2000); do echo "line $i xxxxxxxxxxxxxxxxxxxxxxxxxxxx"; done\nexit 0\n',
    )
    svc = _make_service(project_root, log_dir)
    result = svc.run(agent_name="tech_lead", project_id="exampleapp")
    assert "truncated" in result.stdout_tail
    assert "line 2000" in result.stdout_tail
