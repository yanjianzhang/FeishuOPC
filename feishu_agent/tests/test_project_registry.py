"""Tests for ProjectRegistry + loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from feishu_agent.tools.project_registry import (
    Project,
    ProjectRegistry,
    ProjectRegistryError,
    build_project_registry,
    discover_from_adapters,
    load_projects_jsonl,
)

# ---------------------------------------------------------------------------
# ProjectRegistry
# ---------------------------------------------------------------------------


def test_registry_empty():
    r = ProjectRegistry([])
    assert len(r) == 0
    assert r.default_project_id() is None
    assert r.list() == []
    assert r.project_roots() == {}
    assert "anything" not in r
    assert r.get("anything") is None


def test_registry_basic(tmp_path: Path):
    p1 = Project("alpha", "Alpha", tmp_path / "a", is_default=True)
    p2 = Project("beta", "Beta", tmp_path / "b")
    r = ProjectRegistry([p1, p2])

    assert len(r) == 2
    assert r.default_project_id() == "alpha"
    assert r.get("alpha") is p1
    assert "beta" in r
    roots = r.project_roots()
    assert roots == {"alpha": tmp_path / "a", "beta": tmp_path / "b"}


def test_registry_duplicate_project_id_warns_and_keeps_last(caplog):
    p1 = Project("x", "X1", None)
    p2 = Project("x", "X2", None)
    r = ProjectRegistry([p1, p2])
    assert len(r) == 1
    assert r.get("x") is p2


def test_registry_multiple_defaults_takes_first(caplog):
    p1 = Project("x", "X", None, is_default=True)
    p2 = Project("y", "Y", None, is_default=True)
    r = ProjectRegistry([p1, p2])
    assert r.default_project_id() == "x"


def test_registry_skips_none_repo_roots():
    p1 = Project("x", "X", None)
    p2 = Project("y", "Y", Path("/tmp/y"))
    r = ProjectRegistry([p1, p2])
    assert r.project_roots() == {"y": Path("/tmp/y")}


# ---------------------------------------------------------------------------
# load_projects_jsonl
# ---------------------------------------------------------------------------


def test_load_missing_file_returns_empty(tmp_path: Path):
    assert load_projects_jsonl(tmp_path / "nope.jsonl") == []


def test_load_happy_path(tmp_path: Path):
    f = tmp_path / "projects.jsonl"
    f.write_text(
        "# header comment\n"
        "\n"
        '{"project_id":"alpha","display_name":"Alpha","project_repo_root":"~/a","is_default":true}\n'
        '{"project_id":"beta","project_repo_root":"/abs/b"}\n',
        encoding="utf-8",
    )
    out = load_projects_jsonl(f)
    assert len(out) == 2
    alpha = out[0]
    assert alpha.project_id == "alpha"
    assert alpha.display_name == "Alpha"
    assert alpha.is_default is True
    assert str(alpha.project_repo_root).startswith(str(Path("~/a").expanduser().parent))
    beta = out[1]
    assert beta.display_name == "beta"  # defaulted from project_id
    assert beta.project_repo_root == Path("/abs/b")
    assert beta.is_default is False


def test_load_display_name_defaults_to_id(tmp_path: Path):
    f = tmp_path / "projects.jsonl"
    f.write_text('{"project_id":"alpha"}\n', encoding="utf-8")
    out = load_projects_jsonl(f)
    assert out[0].display_name == "alpha"
    assert out[0].project_repo_root is None


def test_load_extra_fields_preserved(tmp_path: Path):
    f = tmp_path / "projects.jsonl"
    f.write_text(
        '{"project_id":"alpha","custom_key":"custom_value"}\n',
        encoding="utf-8",
    )
    out = load_projects_jsonl(f)
    assert out[0].extra == {"custom_key": "custom_value"}


def test_load_bad_json_raises(tmp_path: Path):
    f = tmp_path / "projects.jsonl"
    f.write_text("{not json", encoding="utf-8")
    with pytest.raises(ProjectRegistryError, match="invalid JSON"):
        load_projects_jsonl(f)


def test_load_non_object_entry_raises(tmp_path: Path):
    f = tmp_path / "projects.jsonl"
    f.write_text("[1,2,3]\n", encoding="utf-8")
    with pytest.raises(ProjectRegistryError, match="must be an object"):
        load_projects_jsonl(f)


def test_load_missing_project_id_raises(tmp_path: Path):
    f = tmp_path / "projects.jsonl"
    f.write_text('{"display_name":"X"}\n', encoding="utf-8")
    with pytest.raises(ProjectRegistryError, match="project_id required"):
        load_projects_jsonl(f)


def test_load_empty_repo_root_raises(tmp_path: Path):
    f = tmp_path / "projects.jsonl"
    f.write_text('{"project_id":"alpha","project_repo_root":""}\n', encoding="utf-8")
    with pytest.raises(ProjectRegistryError, match="project_repo_root"):
        load_projects_jsonl(f)


def test_load_bad_project_id_type_raises(tmp_path: Path):
    f = tmp_path / "projects.jsonl"
    f.write_text('{"project_id":123}\n', encoding="utf-8")
    with pytest.raises(ProjectRegistryError, match="project_id required"):
        load_projects_jsonl(f)


def test_load_bad_display_name_raises(tmp_path: Path):
    f = tmp_path / "projects.jsonl"
    f.write_text(
        '{"project_id":"alpha","display_name":123}\n', encoding="utf-8"
    )
    with pytest.raises(ProjectRegistryError, match="display_name"):
        load_projects_jsonl(f)


# ---------------------------------------------------------------------------
# discover_from_adapters
# ---------------------------------------------------------------------------


def test_discover_missing_dir_returns_empty(tmp_path: Path):
    assert discover_from_adapters(tmp_path / "nope") == []


def test_discover_reads_adapter_files(tmp_path: Path):
    d = tmp_path / "project-adapters"
    d.mkdir()
    (d / "alpha-progress.json").write_text(
        json.dumps({"project_id": "alpha", "display_name": "Alpha"}),
        encoding="utf-8",
    )
    (d / "beta-progress.json").write_text(
        json.dumps({"project_id": "beta"}), encoding="utf-8"
    )
    (d / "bad-progress.json").write_text("not json", encoding="utf-8")
    (d / "noid-progress.json").write_text(
        json.dumps({"display_name": "NoId"}), encoding="utf-8"
    )

    out = discover_from_adapters(d)
    pids = {p.project_id for p in out}
    assert pids == {"alpha", "beta"}
    for p in out:
        assert p.project_repo_root is None
        assert p.is_default is False


# ---------------------------------------------------------------------------
# build_project_registry
# ---------------------------------------------------------------------------


def test_build_from_jsonl_when_present(tmp_path: Path):
    repo = tmp_path
    jsonl_dir = repo / ".larkagent" / "secrets" / "projects"
    jsonl_dir.mkdir(parents=True)
    (jsonl_dir / "projects.jsonl").write_text(
        '{"project_id":"alpha","is_default":true}\n', encoding="utf-8"
    )

    # Also put something in project-adapters to prove it's NOT consulted
    # when jsonl is present.
    adapters = repo / "project-adapters"
    adapters.mkdir()
    (adapters / "ghost-progress.json").write_text(
        '{"project_id":"ghost"}', encoding="utf-8"
    )

    r = build_project_registry(app_repo_root=repo)
    assert {p.project_id for p in r.list()} == {"alpha"}
    assert r.default_project_id() == "alpha"


def test_build_falls_back_to_adapters(tmp_path: Path):
    repo = tmp_path
    adapters = repo / "project-adapters"
    adapters.mkdir()
    (adapters / "alpha-progress.json").write_text(
        '{"project_id":"alpha","display_name":"A"}', encoding="utf-8"
    )
    r = build_project_registry(app_repo_root=repo)
    assert [p.project_id for p in r.list()] == ["alpha"]


def test_build_no_repo_root(tmp_path: Path):
    r = build_project_registry(app_repo_root=None)
    assert len(r) == 0


def test_build_default_override_applies(tmp_path: Path):
    repo = tmp_path
    adapters = repo / "project-adapters"
    adapters.mkdir()
    (adapters / "alpha-progress.json").write_text(
        '{"project_id":"alpha"}', encoding="utf-8"
    )
    (adapters / "beta-progress.json").write_text(
        '{"project_id":"beta"}', encoding="utf-8"
    )
    r = build_project_registry(
        app_repo_root=repo, default_project_id_override="beta"
    )
    assert r.default_project_id() == "beta"


def test_build_default_override_unknown_logs_and_ignored(tmp_path: Path, caplog):
    repo = tmp_path
    adapters = repo / "project-adapters"
    adapters.mkdir()
    (adapters / "alpha-progress.json").write_text(
        '{"project_id":"alpha"}', encoding="utf-8"
    )
    r = build_project_registry(
        app_repo_root=repo, default_project_id_override="nonexistent"
    )
    assert r.default_project_id() is None
