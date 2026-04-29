from __future__ import annotations

from pathlib import Path

import pytest

from feishu_agent.tools.workflow_service import (
    WORKFLOW_REGISTRY,
    InstructionNotFoundError,
    UnknownWorkflowError,
    WorkflowError,
    WorkflowPathError,
    WorkflowPermissionError,
    WorkflowSecretError,
    WorkflowService,
)


@pytest.fixture
def svc(tmp_path: Path) -> WorkflowService:
    app_repo = tmp_path / "feishuopc"
    project_repo = tmp_path / "exampleapp"
    (app_repo / ".cursor/commands").mkdir(parents=True)
    (app_repo / "_bmad/bmm/workflows/4-implementation/create-story").mkdir(parents=True)
    (app_repo / ".cursor/commands/speckit.plan.md").write_text("PLAN-INSTRUCTIONS", encoding="utf-8")
    (app_repo / ".cursor/commands/speckit.specify.md").write_text("SPEC-INSTRUCTIONS", encoding="utf-8")
    (app_repo / "_bmad/bmm/workflows/4-implementation/create-story/instructions.xml").write_text(
        "<story/>", encoding="utf-8"
    )
    project_repo.mkdir()
    (project_repo / "specs").mkdir()
    (project_repo / "specs/003-existing").mkdir()
    (project_repo / "specs/003-existing/spec.md").write_text("existing spec", encoding="utf-8")
    return WorkflowService(
        app_repo_root=app_repo,
        project_roots={"exampleapp": project_repo},
    )


# ---------------------------------------------------------------------------
# Registry + permissions
# ---------------------------------------------------------------------------


def test_registry_covers_expected_commands():
    ids = set(WORKFLOW_REGISTRY)
    assert {
        "speckit.specify",
        "speckit.clarify",
        "speckit.checklist",
        "speckit.plan",
        "speckit.tasks",
        "speckit.analyze",
        "bmad:create-story",
        "bmad:dev-story",
        "bmad:code-review",
    }.issubset(ids)


def test_list_for_agent_filters(svc: WorkflowService):
    pm = {w.workflow_id for w in svc.list_for_agent("product_manager")}
    tl = {w.workflow_id for w in svc.list_for_agent("tech_lead")}
    assert "speckit.specify" in pm and "speckit.plan" not in pm
    assert "speckit.plan" in tl and "speckit.specify" not in tl
    assert "speckit.checklist" in pm and "speckit.checklist" in tl


def test_get_descriptor_permission_denied(svc: WorkflowService):
    with pytest.raises(WorkflowPermissionError):
        svc.get_descriptor("speckit.plan", "product_manager")


def test_get_descriptor_unknown(svc: WorkflowService):
    with pytest.raises(UnknownWorkflowError):
        svc.get_descriptor("speckit.nonsense", "tech_lead")


def test_get_descriptor_accepts_bmm_alias(svc: WorkflowService):
    """`bmm:<slug>` should resolve to the canonical `bmad:<slug>` entry.

    Workflows live on disk under ``_bmad/bmm/workflows/**``, so models
    routinely reach for the ``bmm:`` prefix. We register the canonical
    ``bmad:`` id but accept the alias transparently instead of firing
    an ``UNKNOWN_WORKFLOW`` that the model would have to retry.
    """
    # Direct alias lookup (writer path).
    desc = svc.get_descriptor("bmm:create-story", "tech_lead")
    assert desc.workflow_id == "bmad:create-story"

    # Readonly path (sub-agents that load methodology without being
    # listed in ``allowed_agents``) must also honor the alias.
    desc2 = svc.get_descriptor(
        "bmm:dev-story", "developer", enforce_agent=False
    )
    assert desc2.workflow_id == "bmad:dev-story"


# ---------------------------------------------------------------------------
# read_instruction
# ---------------------------------------------------------------------------


def test_read_instruction_happy(svc: WorkflowService):
    out = svc.read_instruction("speckit.plan", "tech_lead")
    assert out["workflow_id"] == "speckit.plan"
    assert out["instruction"] == "PLAN-INSTRUCTIONS"
    assert out["artifact_subdir"] == "specs"


def test_read_instruction_denied_for_pm(svc: WorkflowService):
    with pytest.raises(WorkflowPermissionError):
        svc.read_instruction("speckit.plan", "product_manager")


def test_read_instruction_missing_file(svc: WorkflowService, tmp_path: Path):
    # speckit.tasks is not created in fixture
    with pytest.raises(InstructionNotFoundError):
        svc.read_instruction("speckit.tasks", "tech_lead")


# ---------------------------------------------------------------------------
# write_artifact
# ---------------------------------------------------------------------------


def test_write_artifact_happy(svc: WorkflowService, tmp_path: Path):
    res = svc.write_artifact(
        workflow_id="speckit.plan",
        agent_name="tech_lead",
        project_id="exampleapp",
        relative_path="003-existing/plan.md",
        content="# plan",
    )
    project_repo = tmp_path / "exampleapp"
    assert (project_repo / "specs/003-existing/plan.md").read_text(encoding="utf-8") == "# plan"
    assert res["path"] == "specs/003-existing/plan.md"
    assert res["bytes_written"] == len("# plan".encode("utf-8"))


def test_write_artifact_creates_parents(svc: WorkflowService, tmp_path: Path):
    svc.write_artifact(
        workflow_id="bmad:create-story",
        agent_name="tech_lead",
        project_id="exampleapp",
        relative_path="3-1-brand-new/story.md",
        content="story",
    )
    project_repo = tmp_path / "exampleapp"
    assert (project_repo / "stories/3-1-brand-new/story.md").is_file()


def test_write_artifact_escape_blocked(svc: WorkflowService):
    with pytest.raises(WorkflowPathError):
        svc.write_artifact(
            workflow_id="speckit.plan",
            agent_name="tech_lead",
            project_id="exampleapp",
            relative_path="../../etc/passwd",
            content="x",
        )


def test_write_artifact_denied_for_wrong_agent(svc: WorkflowService):
    with pytest.raises(WorkflowPermissionError):
        svc.write_artifact(
            workflow_id="speckit.plan",
            agent_name="product_manager",
            project_id="exampleapp",
            relative_path="003-existing/plan.md",
            content="x",
        )


def test_write_artifact_refused_when_content_contains_secret(
    svc: WorkflowService, tmp_path: Path
):
    body = (
        "# plan\n"
        "The server uses this key:\n"
        "```\n"
        "AKIAIOSFODNN7EXAMPLE\n"
        "```\n"
    )
    with pytest.raises(WorkflowSecretError) as ei:
        svc.write_artifact(
            workflow_id="speckit.plan",
            agent_name="tech_lead",
            project_id="exampleapp",
            relative_path="003-existing/plan.md",
            content=body,
        )
    assert ei.value.code == "ARTIFACT_SECRET_DETECTED"
    assert any(f.rule_id == "aws_access_key_id" for f in ei.value.findings)
    # file must not exist on disk
    assert not (tmp_path / "exampleapp/specs/003-existing/plan.md").exists()


def test_write_artifacts_batch_refused_atomically_when_any_secret(
    svc: WorkflowService, tmp_path: Path
):
    files = [
        {"relative_path": "003-existing/plan.md", "content": "# clean plan\n"},
        {
            "relative_path": "003-existing/notes.md",
            "content": "token = ghp_" + "x" * 40 + "\n",
        },
    ]
    with pytest.raises(WorkflowSecretError):
        svc.write_artifacts_batch(
            workflow_id="speckit.plan",
            agent_name="tech_lead",
            project_id="exampleapp",
            files=files,
        )
    # Neither file should have landed.
    assert not (tmp_path / "exampleapp/specs/003-existing/plan.md").exists()
    assert not (tmp_path / "exampleapp/specs/003-existing/notes.md").exists()


# ---------------------------------------------------------------------------
# list_artifacts
# ---------------------------------------------------------------------------


def test_list_artifacts_existing(svc: WorkflowService):
    res = svc.list_artifacts(
        workflow_id="speckit.plan",
        agent_name="tech_lead",
        project_id="exampleapp",
    )
    assert res["exists"] is True
    names = {e["name"] for e in res["entries"]}
    assert "003-existing" in names


def test_list_artifacts_missing(svc: WorkflowService):
    res = svc.list_artifacts(
        workflow_id="bmad:code-review",
        agent_name="tech_lead",
        project_id="exampleapp",
    )
    assert res["exists"] is False
    assert res["entries"] == []


def test_list_artifacts_sub_path_escape_blocked(svc: WorkflowService):
    with pytest.raises(WorkflowPathError):
        svc.list_artifacts(
            workflow_id="speckit.plan",
            agent_name="tech_lead",
            project_id="exampleapp",
            sub_path="../",
        )


# ---------------------------------------------------------------------------
# read_repo_file
# ---------------------------------------------------------------------------


def test_read_repo_file_happy(svc: WorkflowService):
    res = svc.read_repo_file(
        project_id="exampleapp", relative_path="specs/003-existing/spec.md"
    )
    assert res["content"] == "existing spec"
    assert res["path"] == "specs/003-existing/spec.md"


def test_read_repo_file_denied_outside_whitelist(svc: WorkflowService, tmp_path: Path):
    project_repo = tmp_path / "exampleapp"
    (project_repo / "random").mkdir()
    (project_repo / "random/foo.txt").write_text("x", encoding="utf-8")
    with pytest.raises(WorkflowPathError):
        svc.read_repo_file(project_id="exampleapp", relative_path="random/foo.txt")


def test_read_repo_file_denied_on_secrets(svc: WorkflowService, tmp_path: Path):
    project_repo = tmp_path / "exampleapp"
    (project_repo / "specs/003-existing/.env").write_text("SECRET=1", encoding="utf-8")
    with pytest.raises(WorkflowPathError):
        svc.read_repo_file(
            project_id="exampleapp", relative_path="specs/003-existing/.env"
        )


def test_read_repo_file_escape_blocked(svc: WorkflowService):
    with pytest.raises(WorkflowPathError):
        svc.read_repo_file(project_id="exampleapp", relative_path="../feishuopc/.env")


# ---------------------------------------------------------------------------
# write_artifacts_batch
# ---------------------------------------------------------------------------


def test_write_artifacts_batch_happy(svc: WorkflowService, tmp_path: Path):
    res = svc.write_artifacts_batch(
        workflow_id="speckit.plan",
        agent_name="tech_lead",
        project_id="exampleapp",
        files=[
            {"relative_path": "004-batch/plan.md", "content": "# plan"},
            {"relative_path": "004-batch/research.md", "content": "# research"},
            {"relative_path": "004-batch/data-model.md", "content": "# dm"},
        ],
    )
    project_repo = tmp_path / "exampleapp"
    assert res["count"] == 3
    assert (project_repo / "specs/004-batch/plan.md").is_file()
    assert (project_repo / "specs/004-batch/research.md").is_file()
    assert (project_repo / "specs/004-batch/data-model.md").is_file()


def test_write_artifacts_batch_all_or_nothing(
    svc: WorkflowService, tmp_path: Path
):
    project_repo = tmp_path / "exampleapp"
    with pytest.raises(WorkflowPathError):
        svc.write_artifacts_batch(
            workflow_id="speckit.plan",
            agent_name="tech_lead",
            project_id="exampleapp",
            files=[
                {"relative_path": "005-bad/plan.md", "content": "# plan"},
                {"relative_path": "../escape.md", "content": "x"},
            ],
        )
    assert not (project_repo / "specs/005-bad").exists()


def test_write_artifacts_batch_empty_refused(svc: WorkflowService):
    with pytest.raises(WorkflowError):
        svc.write_artifacts_batch(
            workflow_id="speckit.plan",
            agent_name="tech_lead",
            project_id="exampleapp",
            files=[],
        )


def test_write_artifacts_batch_permission_denied(svc: WorkflowService):
    with pytest.raises(WorkflowPermissionError):
        svc.write_artifacts_batch(
            workflow_id="speckit.plan",
            agent_name="product_manager",  # PM cannot plan
            project_id="exampleapp",
            files=[{"relative_path": "006/plan.md", "content": "x"}],
        )


def test_write_artifacts_batch_exceeds_max(svc: WorkflowService):
    files = [
        {"relative_path": f"007/f{i}.md", "content": "x"} for i in range(3)
    ]
    with pytest.raises(WorkflowError):
        svc.write_artifacts_batch(
            workflow_id="speckit.plan",
            agent_name="tech_lead",
            project_id="exampleapp",
            files=files,
            max_files=2,
        )
