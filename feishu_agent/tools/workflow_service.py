"""Workflow command registry and execution service.

Exposes speckit / BMAD workflow commands as tool-callable operations for
FeishuOPC agents. Each workflow has:

- A methodology instruction file (read-only) inside the FeishuOPC repo
  (``.cursor/commands/*.md`` for speckit, ``_bmad/bmm/workflows/**`` for BMAD).
- An artifact subdir (``specs``, ``stories``, ``reviews``, ...) where outputs
  are written inside the *target project* repo.
- A whitelist of agents allowed to invoke it.

The service enforces:

- Agent-level permission: PM cannot invoke ``speckit.plan`` etc.
- Path containment: reads/writes must stay within the allowed roots.
- Project-aware artifact roots: resolved via project_id. Each known
  project is registered in the ``ProjectRegistry`` (see
  ``project_registry.py``) and supplies its own ``project_repo_root``.
  Projects whose repo root isn't configured fall back to a sandbox
  inside ``{app_repo_root}/project_knowledge/<project_id>/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from feishu_agent.tools import secret_scanner
from feishu_agent.tools.code_write_service import (
    _HARDCODED_DENIED_SEGMENTS,
)

# ---------------------------------------------------------------------------
# Workflow descriptor + registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkflowDescriptor:
    workflow_id: str
    instruction_path: str  # relative to app_repo_root
    artifact_subdir: str  # relative to project repo root
    allowed_agents: tuple[str, ...]
    description: str
    guidance: str = ""


_PM = "product_manager"
_TL = "tech_lead"


WORKFLOW_REGISTRY: dict[str, WorkflowDescriptor] = {
    # ------------------------------------------------------------------ PM
    "speckit.specify": WorkflowDescriptor(
        workflow_id="speckit.specify",
        instruction_path=".cursor/commands/speckit.specify.md",
        artifact_subdir="specs",
        allowed_agents=(_PM,),
        description="Create a new feature specification (spec.md) from a natural-language description.",
        guidance="Follow the instructions in the speckit.specify command file. Output lands at specs/NNN-feature-slug/spec.md.",
    ),
    "speckit.clarify": WorkflowDescriptor(
        workflow_id="speckit.clarify",
        instruction_path=".cursor/commands/speckit.clarify.md",
        artifact_subdir="specs",
        allowed_agents=(_PM,),
        description="Identify and resolve ambiguities in an existing spec.md via structured questions.",
    ),
    "speckit.checklist": WorkflowDescriptor(
        workflow_id="speckit.checklist",
        instruction_path=".cursor/commands/speckit.checklist.md",
        artifact_subdir="specs",
        allowed_agents=(_PM, _TL),
        description="Generate or run a requirements-side checklist for an existing spec.",
    ),
    # ------------------------------------------------------------------ TL
    "speckit.plan": WorkflowDescriptor(
        workflow_id="speckit.plan",
        instruction_path=".cursor/commands/speckit.plan.md",
        artifact_subdir="specs",
        allowed_agents=(_TL,),
        description="Generate plan.md, research.md, data-model.md, contracts/, quickstart.md for a feature.",
    ),
    "speckit.tasks": WorkflowDescriptor(
        workflow_id="speckit.tasks",
        instruction_path=".cursor/commands/speckit.tasks.md",
        artifact_subdir="specs",
        allowed_agents=(_TL,),
        description="Break down plan.md into a dependency-ordered tasks.md.",
    ),
    "speckit.analyze": WorkflowDescriptor(
        workflow_id="speckit.analyze",
        instruction_path=".cursor/commands/speckit.analyze.md",
        artifact_subdir="specs",
        allowed_agents=(_TL,),
        description="Cross-artifact consistency analysis across spec.md / plan.md / tasks.md.",
    ),
    "bmad:create-story": WorkflowDescriptor(
        workflow_id="bmad:create-story",
        instruction_path="_bmad/bmm/workflows/4-implementation/create-story/instructions.xml",
        artifact_subdir="stories",
        allowed_agents=(_TL,),
        description="Create a BMAD implementation story from an epic / sprint goal.",
    ),
    "bmad:dev-story": WorkflowDescriptor(
        workflow_id="bmad:dev-story",
        instruction_path="_bmad/bmm/workflows/4-implementation/dev-story/instructions.xml",
        artifact_subdir="stories",
        allowed_agents=(_TL,),
        description="Execute a BMAD dev-story: implement tasks, mark completion, update story file.",
    ),
    "bmad:code-review": WorkflowDescriptor(
        workflow_id="bmad:code-review",
        instruction_path="_bmad/bmm/workflows/4-implementation/code-review/instructions.xml",
        artifact_subdir="reviews",
        allowed_agents=(_TL,),
        description="Senior code review on a completed story — produce findings + recommendations.",
    ),
    # Additional 4-implementation entries for sub-agent READ-ONLY
    # consumption. Sub-agents (reviewer/developer/bug_fixer/
    # sprint_planner/ux_designer/researcher) are not in
    # ``allowed_agents`` — the read path bypasses that gate via
    # ``enforce_agent=False`` when the mixin is in readonly mode. Writes
    # still go through tech_lead / prd_writer only.
    "bmad:correct-course": WorkflowDescriptor(
        workflow_id="bmad:correct-course",
        instruction_path="_bmad/bmm/workflows/4-implementation/correct-course/instructions.md",
        artifact_subdir="reviews",
        allowed_agents=(_TL,),
        description="Bug-fixer methodology: triage a blocked review and correct course without scope creep.",
    ),
    "bmad:sprint-planning": WorkflowDescriptor(
        workflow_id="bmad:sprint-planning",
        instruction_path="_bmad/bmm/workflows/4-implementation/sprint-planning/instructions.md",
        artifact_subdir="stories",
        allowed_agents=(_TL,),
        description="Sprint-planner methodology: compose a sprint plan from epics, stories, and risks.",
    ),
    "bmad:sprint-status": WorkflowDescriptor(
        workflow_id="bmad:sprint-status",
        instruction_path="_bmad/bmm/workflows/4-implementation/sprint-status/instructions.md",
        artifact_subdir="stories",
        allowed_agents=(_TL,),
        description="Sprint-status methodology: report current sprint state and blockers.",
    ),
    "bmad:retrospective": WorkflowDescriptor(
        workflow_id="bmad:retrospective",
        instruction_path="_bmad/bmm/workflows/4-implementation/retrospective/instructions.md",
        artifact_subdir="reviews",
        allowed_agents=(_TL,),
        description="Retrospective methodology: run a structured post-sprint reflection.",
    ),
    "bmad:create-ux-design": WorkflowDescriptor(
        workflow_id="bmad:create-ux-design",
        instruction_path="_bmad/bmm/workflows/2-plan-workflows/create-ux-design/workflow.md",
        artifact_subdir="specs",
        allowed_agents=(_PM,),
        description="UX-designer methodology: produce a UX design artifact from a PRD / brief.",
    ),
    "bmad:research": WorkflowDescriptor(
        workflow_id="bmad:research",
        instruction_path="_bmad/bmm/workflows/1-analysis/research/workflow-domain-research.md",
        artifact_subdir="specs",
        allowed_agents=(_PM,),
        description="Researcher methodology: structured domain / market / technical research.",
    ),
    "bmad:create-product-brief": WorkflowDescriptor(
        workflow_id="bmad:create-product-brief",
        instruction_path="_bmad/bmm/workflows/1-analysis/create-product-brief/workflow.md",
        artifact_subdir="specs",
        allowed_agents=(_PM,),
        description="Product-brief methodology for researcher / PM collaboration.",
    ),
}


# Read-only directories each agent is allowed to browse beyond workflow
# instruction files (used by ``read_repo_file``). Kept narrow on purpose —
# we never expose ``.env``, ``.larkagent/secrets``, or ``.git`` paths.
_ALLOWED_READ_ROOTS: tuple[str, ...] = (
    "specs",
    "stories",
    "reviews",
    "project_knowledge",
    "_bmad",
    ".cursor/commands",
    ".specify",
    "skills",
    "docs",
)

# Single source of truth: reuse ``code_write_service._HARDCODED_DENIED_SEGMENTS``.
# Workflow artifacts (specs, reviews, stories) and code writes share the
# same fail-closed denylist — if an operator wants to broaden what may
# be written here, they can relax only ``code_write_service``'s
# per-project policy; these baseline segments never open up.
_DENIED_PATH_SEGMENTS: tuple[str, ...] = _HARDCODED_DENIED_SEGMENTS


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class WorkflowError(Exception):
    """Base class for workflow-service errors. Always carries a stable code."""

    code: str = "WORKFLOW_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnknownWorkflowError(WorkflowError):
    code = "UNKNOWN_WORKFLOW"


class WorkflowPermissionError(WorkflowError):
    code = "WORKFLOW_NOT_ALLOWED_FOR_AGENT"


class WorkflowPathError(WorkflowError):
    code = "PATH_OUT_OF_BOUNDS"


class InstructionNotFoundError(WorkflowError):
    code = "INSTRUCTION_FILE_MISSING"


class WorkflowSecretError(WorkflowError):
    """Artifact write refused because content matched a secret pattern."""

    code = "ARTIFACT_SECRET_DETECTED"

    def __init__(self, message: str, *, findings: list[secret_scanner.SecretFinding]) -> None:
        super().__init__(message)
        self.findings = findings


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectRoots:
    """Resolved paths for a single project_id."""

    app_repo_root: Path  # FeishuOPC repo (methodology source)
    project_repo_root: Path  # Target project repo (artifacts land here)


class WorkflowService:
    def __init__(
        self,
        *,
        app_repo_root: Path,
        project_roots: dict[str, Path],
    ) -> None:
        self.app_repo_root = app_repo_root.resolve()
        self._project_roots = {
            pid: root.resolve() for pid, root in project_roots.items()
        }

    # -- discovery ------------------------------------------------------

    def list_for_agent(self, agent_name: str) -> list[WorkflowDescriptor]:
        return [
            w for w in WORKFLOW_REGISTRY.values() if agent_name in w.allowed_agents
        ]

    def get_descriptor(
        self,
        workflow_id: str,
        agent_name: str,
        *,
        enforce_agent: bool = True,
    ) -> WorkflowDescriptor:
        """Return the descriptor for ``workflow_id``.

        When ``enforce_agent`` is ``True`` (default), the call also
        asserts that ``agent_name`` is listed in ``desc.allowed_agents``
        — this is the gate for *writing* workflow artifacts and keeps
        product_manager / tech_lead in their swim lanes.

        When ``enforce_agent`` is ``False``, the agent check is
        skipped. This is used by sub-agent roles (reviewer / developer
        / bug_fixer / sprint_planner / ux_designer / researcher) that
        need to LOAD a methodology instruction (e.g.
        ``bmad:code-review``) without being listed in the descriptor's
        ``allowed_agents``. Writes are still gated by the mixin's
        ``_workflow_readonly`` flag and by the write tools never being
        surfaced to those roles.
        """
        desc = WORKFLOW_REGISTRY.get(workflow_id)
        if desc is None:
            # Alias: workflows are physically stored under
            # ``_bmad/bmm/workflows/**`` so models frequently reach for a
            # ``bmm:<slug>`` prefix that mirrors the disk layout. Accept
            # it transparently (the canonical id in the registry is
            # still ``bmad:<slug>``) so we don't emit an UNKNOWN_WORKFLOW
            # just because the model used the more literal prefix.
            if workflow_id.startswith("bmm:"):
                desc = WORKFLOW_REGISTRY.get("bmad:" + workflow_id[len("bmm:") :])
        if desc is None:
            raise UnknownWorkflowError(
                f"Unknown workflow_id: {workflow_id!r}. "
                f"Available: {sorted(WORKFLOW_REGISTRY)}"
            )
        if enforce_agent and agent_name not in desc.allowed_agents:
            raise WorkflowPermissionError(
                f"Workflow {workflow_id!r} is not allowed for agent {agent_name!r}. "
                f"Allowed agents: {list(desc.allowed_agents)}"
            )
        return desc

    def resolve_project_root(self, project_id: str) -> Path:
        root = self._project_roots.get(project_id)
        if root is None:
            raise WorkflowError(
                f"No repo root configured for project_id={project_id!r}."
            )
        return root

    # -- instruction read ----------------------------------------------

    def read_instruction(
        self,
        workflow_id: str,
        agent_name: str,
        *,
        enforce_agent: bool = True,
    ) -> dict[str, Any]:
        desc = self.get_descriptor(
            workflow_id, agent_name, enforce_agent=enforce_agent
        )
        path = (self.app_repo_root / desc.instruction_path).resolve()
        self._ensure_within(path, self.app_repo_root)
        if not path.is_file():
            raise InstructionNotFoundError(
                f"Instruction file missing for {workflow_id}: {desc.instruction_path}"
            )
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise WorkflowError(f"Failed to read instruction: {exc}") from exc
        return {
            "workflow_id": workflow_id,
            "instruction_path": desc.instruction_path,
            "artifact_subdir": desc.artifact_subdir,
            "description": desc.description,
            "guidance": desc.guidance,
            "instruction": content,
        }

    # -- artifact write ------------------------------------------------

    def write_artifact(
        self,
        *,
        workflow_id: str,
        agent_name: str,
        project_id: str,
        relative_path: str,
        content: str,
    ) -> dict[str, Any]:
        desc = self.get_descriptor(workflow_id, agent_name)
        project_root = self.resolve_project_root(project_id)
        artifact_root = (project_root / desc.artifact_subdir).resolve()
        target = (artifact_root / relative_path).resolve()
        self._ensure_within(target, artifact_root)
        self._ensure_safe_segments(target)
        self._ensure_no_secret(content, rel_display=str(target.relative_to(project_root)))

        parents_existed = target.parent.exists()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {
            "workflow_id": workflow_id,
            "project_id": project_id,
            "artifact_root": str(artifact_root),
            "path": str(target.relative_to(project_root)),
            "bytes_written": len(content.encode("utf-8")),
            "created_parents": not parents_existed,
        }

    def write_artifacts_batch(
        self,
        *,
        workflow_id: str,
        agent_name: str,
        project_id: str,
        files: list[dict[str, str]],
        max_files: int = 20,
    ) -> dict[str, Any]:
        """Write multiple workflow artifacts in one call.

        ``files`` is ``[{relative_path, content}, ...]``. Returns a summary
        with per-file results. On any validation error we **stop before
        writing anything** so either everything is accepted or nothing
        lands (simple all-or-nothing semantics; retry-safe).
        """
        if not isinstance(files, list) or not files:
            raise WorkflowError("files must be a non-empty list.")
        if len(files) > max_files:
            raise WorkflowError(
                f"Too many files: {len(files)} > max_files={max_files}."
            )

        desc = self.get_descriptor(workflow_id, agent_name)
        project_root = self.resolve_project_root(project_id)
        artifact_root = (project_root / desc.artifact_subdir).resolve()

        planned: list[tuple[Path, str]] = []
        for idx, item in enumerate(files):
            if not isinstance(item, dict):
                raise WorkflowError(f"files[{idx}] is not an object.")
            relative_path = item.get("relative_path") or item.get("path")
            content = item.get("content")
            if not relative_path or not isinstance(relative_path, str):
                raise WorkflowError(f"files[{idx}].relative_path required.")
            if content is None or not isinstance(content, str):
                raise WorkflowError(f"files[{idx}].content must be a string.")
            target = (artifact_root / relative_path).resolve()
            self._ensure_within(target, artifact_root)
            self._ensure_safe_segments(target)
            self._ensure_no_secret(
                content, rel_display=str(target.relative_to(project_root))
            )
            planned.append((target, content))

        written: list[dict[str, Any]] = []
        for target, content in planned:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            written.append(
                {
                    "path": str(target.relative_to(project_root)),
                    "bytes_written": len(content.encode("utf-8")),
                }
            )
        return {
            "workflow_id": workflow_id,
            "project_id": project_id,
            "artifact_root": str(artifact_root),
            "count": len(written),
            "files": written,
        }

    # -- artifact list -------------------------------------------------

    def list_artifacts(
        self,
        *,
        workflow_id: str,
        agent_name: str,
        project_id: str,
        sub_path: str = "",
        enforce_agent: bool = True,
    ) -> dict[str, Any]:
        desc = self.get_descriptor(
            workflow_id, agent_name, enforce_agent=enforce_agent
        )
        project_root = self.resolve_project_root(project_id)
        artifact_root = (project_root / desc.artifact_subdir).resolve()
        target_dir = (artifact_root / sub_path).resolve() if sub_path else artifact_root
        self._ensure_within(target_dir, artifact_root)

        if not target_dir.exists():
            return {
                "workflow_id": workflow_id,
                "project_id": project_id,
                "root": str(artifact_root.relative_to(project_root)),
                "sub_path": sub_path,
                "exists": False,
                "entries": [],
            }

        entries: list[dict[str, Any]] = []
        for entry in sorted(target_dir.iterdir()):
            entries.append(
                {
                    "name": entry.name,
                    "type": "dir" if entry.is_dir() else "file",
                    "rel_path": str(entry.relative_to(project_root)),
                    "size": entry.stat().st_size if entry.is_file() else None,
                }
            )
        return {
            "workflow_id": workflow_id,
            "project_id": project_id,
            "root": str(artifact_root.relative_to(project_root)),
            "sub_path": sub_path,
            "exists": True,
            "entries": entries,
        }

    # -- project-repo read --------------------------------------------

    def read_repo_file(
        self,
        *,
        project_id: str,
        relative_path: str,
        max_bytes: int = 256 * 1024,
    ) -> dict[str, Any]:
        project_root = self.resolve_project_root(project_id)
        target = (project_root / relative_path).resolve()
        self._ensure_within(target, project_root)
        self._ensure_safe_segments(target)
        self._ensure_under_allowed_read_root(target, project_root)

        if not target.is_file():
            raise WorkflowError(f"Not a file or does not exist: {relative_path}")

        size = target.stat().st_size
        truncated = size > max_bytes
        with target.open("r", encoding="utf-8", errors="replace") as fh:
            content = fh.read(max_bytes)
        return {
            "project_id": project_id,
            "path": str(target.relative_to(project_root)),
            "bytes": size,
            "truncated": truncated,
            "content": content,
        }

    # -- helpers -------------------------------------------------------

    @staticmethod
    def _ensure_within(candidate: Path, root: Path) -> None:
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise WorkflowPathError(
                f"Path {candidate} escapes allowed root {root}"
            ) from exc

    @staticmethod
    def _ensure_safe_segments(target: Path) -> None:
        low = str(target).lower()
        for needle in _DENIED_PATH_SEGMENTS:
            if needle in low:
                raise WorkflowPathError(
                    f"Refusing to touch sensitive path segment '{needle}' in {target}"
                )

    @staticmethod
    def _ensure_no_secret(content: str, *, rel_display: str) -> None:
        """Refuse artifact writes whose content contains secret material.

        Even though workflow artifacts live under ``specs/stories/reviews``
        (not executable code), they end up in git and could leak creds just
        as easily as source files. Same rules as ``CodeWriteService``.
        """
        try:
            secret_scanner.ensure_clean(content, path=rel_display)
        except secret_scanner.SecretDetectedError as exc:
            raise WorkflowSecretError(str(exc), findings=list(exc.findings)) from exc

    @staticmethod
    def _ensure_under_allowed_read_root(target: Path, project_root: Path) -> None:
        rel = target.relative_to(project_root)
        rel_str = str(rel).replace("\\", "/")
        for allowed in _ALLOWED_READ_ROOTS:
            if rel_str == allowed or rel_str.startswith(f"{allowed}/"):
                return
        raise WorkflowPathError(
            f"Read not permitted outside {_ALLOWED_READ_ROOTS}: {rel_str}"
        )
