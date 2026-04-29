"""Tests for the deploy-project config loader.

Covers: parser happy path + defaults, validation errors, filesystem
loader's ability to ignore template / hidden / malformed files without
taking the whole runtime down. The loader's "skip bad, keep good"
behaviour is the non-obvious bit — operators routinely break ONE JSON
file while editing others, and we don't want that to disable deploys
for every other project.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from feishu_agent.tools.deploy_service import (
    DeployConfigError,
    DeployFlagSpec,
    DeployProjectConfig,
    load_deploy_project_configs,
    parse_deploy_project_config,
)

# ---------------------------------------------------------------------------
# parse_deploy_project_config
# ---------------------------------------------------------------------------


def test_minimal_config_parses_with_defaults() -> None:
    cfg = parse_deploy_project_config({"project_id": "foo"})
    assert cfg == DeployProjectConfig(project_id="foo")
    assert cfg.script_path == "deploy/deploy.sh"
    assert cfg.host_bootstrap_script is None
    assert cfg.default_args == ()
    assert cfg.supported_flags == ()
    assert cfg.default_timeout_seconds == 1800
    assert cfg.notes == ""


def test_host_bootstrap_script_parses_when_present() -> None:
    cfg = parse_deploy_project_config(
        {
            "project_id": "foo",
            "host_bootstrap_script": "deploy/bootstrap-host.sh",
        }
    )
    assert cfg.host_bootstrap_script == "deploy/bootstrap-host.sh"


@pytest.mark.parametrize(
    "bad_value, fragment",
    [
        ("/etc/passwd", "host_bootstrap_script"),
        ("../escape.sh", "host_bootstrap_script"),
        ("", "host_bootstrap_script"),
        (123, "host_bootstrap_script"),
    ],
)
def test_host_bootstrap_script_validation(bad_value, fragment) -> None:
    with pytest.raises(DeployConfigError) as exc:
        parse_deploy_project_config(
            {"project_id": "foo", "host_bootstrap_script": bad_value}
        )
    assert fragment in str(exc.value)


@pytest.mark.parametrize(
    "field, bad_path",
    [
        # Paths containing shell metacharacters — these would break
        # out of agent_deploy.sh's ``bash '<path>'`` wrapping if they
        # were accepted. The allowlist regex is the last line of
        # defense, so we test it directly.
        ("script_path", "deploy/foo';rm -rf /;echo'.sh"),
        ("script_path", "deploy/$(whoami).sh"),
        ("script_path", "deploy/foo`id`.sh"),
        ("script_path", "deploy/foo\nbar.sh"),
        ("script_path", "deploy/foo bar.sh"),  # space rejected
        ("host_bootstrap_script", "deploy/foo';rm -rf /;echo'.sh"),
        ("host_bootstrap_script", "deploy/$(curl attacker).sh"),
    ],
)
def test_script_path_rejects_shell_metachars(field: str, bad_path: str) -> None:
    raw = {"project_id": "foo", field: bad_path}
    with pytest.raises(DeployConfigError) as exc:
        parse_deploy_project_config(raw)
    assert field in str(exc.value)


@pytest.mark.parametrize(
    "good_path",
    [
        "deploy/deploy.sh",
        "deploy/bootstrap-host.sh",
        "scripts/ship.sh",
        "infra/v2/deploy_1.2.sh",
        "a.sh",
        "deep/nested/path/to/script.sh",
    ],
)
def test_script_path_accepts_reasonable_paths(good_path: str) -> None:
    cfg = parse_deploy_project_config(
        {"project_id": "foo", "script_path": good_path}
    )
    assert cfg.script_path == good_path


def test_full_config_parses_all_fields() -> None:
    raw = {
        "project_id": "exampleapp",
        "script_path": "infra/ship.sh",
        "host_bootstrap_script": "infra/bootstrap-host.sh",
        "default_args": ["--config=prod"],
        "supported_flags": [
            {
                "flag": "--server-only",
                "description": "backend only",
                "expected_duration_seconds": 180,
            },
            {
                "flag": "--web-only",
                "description": "frontend only",
            },
        ],
        "default_timeout_seconds": 900,
        "notes": "see docs",
    }
    cfg = parse_deploy_project_config(raw)
    assert cfg.project_id == "exampleapp"
    assert cfg.script_path == "infra/ship.sh"
    assert cfg.host_bootstrap_script == "infra/bootstrap-host.sh"
    assert cfg.default_args == ("--config=prod",)
    assert len(cfg.supported_flags) == 2
    assert cfg.supported_flags[0] == DeployFlagSpec(
        flag="--server-only",
        description="backend only",
        expected_duration_seconds=180,
    )
    assert cfg.supported_flags[1].expected_duration_seconds is None
    assert cfg.default_timeout_seconds == 900
    assert cfg.notes == "see docs"


def test_timeout_above_cap_is_clamped() -> None:
    cfg = parse_deploy_project_config(
        {"project_id": "foo", "default_timeout_seconds": 99_999}
    )
    assert cfg.default_timeout_seconds == 3600


@pytest.mark.parametrize(
    "raw, fragment",
    [
        ({}, "project_id"),
        ({"project_id": ""}, "project_id"),
        ({"project_id": "foo", "script_path": ""}, "script_path"),
        (
            {"project_id": "foo", "script_path": "/abs/path.sh"},
            "absolute",
        ),
        (
            {"project_id": "foo", "script_path": "../escape.sh"},
            "'..'",
        ),
        (
            {"project_id": "foo", "default_args": ["ok", 123]},
            "default_args",
        ),
        (
            {"project_id": "foo", "supported_flags": "nope"},
            "supported_flags",
        ),
        (
            {
                "project_id": "foo",
                "supported_flags": [{"flag": 1, "description": "x"}],
            },
            "flag",
        ),
        (
            {"project_id": "foo", "default_timeout_seconds": 0},
            "default_timeout_seconds",
        ),
        (
            {"project_id": "foo", "default_timeout_seconds": "600"},
            "default_timeout_seconds",
        ),
        ({"project_id": "foo", "notes": 123}, "notes"),
    ],
)
def test_malformed_configs_raise(raw: dict, fragment: str) -> None:
    with pytest.raises(DeployConfigError) as exc:
        parse_deploy_project_config(raw)
    assert fragment in str(exc.value)


# ---------------------------------------------------------------------------
# load_deploy_project_configs
# ---------------------------------------------------------------------------


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_loader_returns_empty_when_dir_missing(tmp_path: Path) -> None:
    assert load_deploy_project_configs(tmp_path / "nope") == {}


def test_loader_picks_up_valid_json_only(tmp_path: Path) -> None:
    _write(
        tmp_path / "exampleapp.json",
        json.dumps({"project_id": "exampleapp"}),
    )
    _write(
        tmp_path / "alpha.json",
        json.dumps({"project_id": "alpha"}),
    )
    # Template — must be ignored.
    _write(
        tmp_path / "exampleapp.json.example",
        json.dumps({"project_id": "exampleapp"}),
    )
    # Editor-turd hidden file — must be ignored even though it's .json.
    _write(tmp_path / ".exampleapp.json.swp", "nonsense")
    # Non-json — must be ignored.
    _write(tmp_path / "notes.md", "hello")

    configs = load_deploy_project_configs(tmp_path)

    assert set(configs) == {"exampleapp", "alpha"}
    assert configs["exampleapp"].project_id == "exampleapp"


def test_loader_skips_invalid_json_without_taking_down_peers(
    tmp_path: Path,
) -> None:
    """A single malformed file must not prevent the loader from
    returning the OTHER projects' configs — otherwise a typo in one
    JSON disables deploys company-wide."""
    _write(tmp_path / "good.json", json.dumps({"project_id": "good"}))
    _write(tmp_path / "busted.json", "{ this is not json")
    _write(
        tmp_path / "bad-schema.json",
        json.dumps({"project_id": ""}),
    )
    _write(tmp_path / "not-object.json", json.dumps([1, 2, 3]))

    configs = load_deploy_project_configs(tmp_path)

    assert set(configs) == {"good"}


def test_loader_lists_duplicate_project_id_takes_last(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "a-exampleapp.json",
        json.dumps({"project_id": "exampleapp", "notes": "first"}),
    )
    _write(
        tmp_path / "b-exampleapp.json",
        json.dumps({"project_id": "exampleapp", "notes": "second"}),
    )
    configs = load_deploy_project_configs(tmp_path)
    assert configs["exampleapp"].notes == "second"
