"""Project registry — single source of truth for *which* projects this
FeishuOPC instance serves and where their repos live.

FeishuOPC is a multi-project agent platform. Each installation (e.g. the
SV server) declares the projects it serves via a config file — there are
**no project identifiers hardcoded in source code**.

Discovery precedence:

1. ``{app_repo_root}/.larkagent/secrets/projects/projects.jsonl``
   — one JSON object per line (see ``projects.example.jsonl``). This is
   authoritative.
2. ``{app_repo_root}/project-adapters/<project_id>-progress.json``
   — auto-discovered as a fallback (progress-sync adapter files already
   enumerate every known project's ``project_id`` + ``display_name``).
   Used only if step 1 is empty/missing.

Either way, the caller gets a list of ``Project`` records with enough
information to resolve repo roots for workflow / code-write services.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ProjectRegistryError(Exception):
    """Raised on malformed ``projects.jsonl``."""


@dataclass(frozen=True)
class Project:
    project_id: str
    display_name: str
    project_repo_root: Path | None
    is_default: bool = False
    extra: dict[str, Any] | None = None


class ProjectRegistry:
    """Immutable registry of projects known to this instance."""

    def __init__(self, projects: list[Project]) -> None:
        seen: dict[str, Project] = {}
        for p in projects:
            if p.project_id in seen:
                logger.warning(
                    "project_registry: duplicate project_id=%s, "
                    "later entry wins.",
                    p.project_id,
                )
            seen[p.project_id] = p
        self._projects = seen

        explicit_default = [p for p in self._projects.values() if p.is_default]
        if len(explicit_default) > 1:
            logger.warning(
                "project_registry: multiple projects marked is_default=true "
                "(%s). First wins.",
                [p.project_id for p in explicit_default],
            )
        self._default_id = explicit_default[0].project_id if explicit_default else None

    # -- accessors ------------------------------------------------------

    def __len__(self) -> int:
        return len(self._projects)

    def __contains__(self, project_id: object) -> bool:
        return project_id in self._projects

    def list(self) -> list[Project]:
        return list(self._projects.values())

    def get(self, project_id: str) -> Project | None:
        return self._projects.get(project_id)

    def default_project_id(self) -> str | None:
        return self._default_id

    def project_roots(self) -> dict[str, Path]:
        return {
            p.project_id: p.project_repo_root
            for p in self._projects.values()
            if p.project_repo_root is not None
        }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _parse_project_entry(entry: dict[str, Any], *, lineno: int, path: Path) -> Project:
    pid = entry.get("project_id")
    if not isinstance(pid, str) or not pid:
        raise ProjectRegistryError(
            f"{path}:{lineno}: project_id required (non-empty string)."
        )
    display = entry.get("display_name") or pid
    if not isinstance(display, str):
        raise ProjectRegistryError(
            f"{path}:{lineno}: display_name must be a string."
        )
    root_raw = entry.get("project_repo_root")
    repo_root: Path | None = None
    if root_raw is not None:
        if not isinstance(root_raw, str) or not root_raw:
            raise ProjectRegistryError(
                f"{path}:{lineno}: project_repo_root must be a non-empty string."
            )
        repo_root = Path(root_raw).expanduser()
    is_default = bool(entry.get("is_default", False))
    extra = {
        k: v
        for k, v in entry.items()
        if k
        not in ("project_id", "display_name", "project_repo_root", "is_default")
    }
    return Project(
        project_id=pid,
        display_name=display,
        project_repo_root=repo_root,
        is_default=is_default,
        extra=extra or None,
    )


def load_projects_jsonl(path: Path) -> list[Project]:
    """Parse a ``projects.jsonl`` file. Returns ``[]`` if missing.

    Fail-closed: a malformed line aborts the whole load (we refuse to
    half-load an identity config).
    """
    if not path.is_file():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProjectRegistryError(f"Failed to read {path}: {exc}") from exc

    out: list[Project] = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ProjectRegistryError(
                f"{path}:{lineno}: invalid JSON: {exc.msg}"
            ) from exc
        if not isinstance(entry, dict):
            raise ProjectRegistryError(
                f"{path}:{lineno}: entry must be an object."
            )
        out.append(_parse_project_entry(entry, lineno=lineno, path=path))
    return out


def discover_from_adapters(project_adapters_dir: Path) -> list[Project]:
    """Fallback discovery: enumerate ``*-progress.json`` files.

    Each adapter's ``project_id`` + ``display_name`` yields a ``Project``
    with ``project_repo_root=None`` (the adapter file doesn't carry it).
    Callers can still resolve repo_root from elsewhere (env, overrides).
    """
    if not project_adapters_dir.is_dir():
        return []
    projects: list[Project] = []
    for adapter_path in sorted(project_adapters_dir.glob("*-progress.json")):
        try:
            data = json.loads(adapter_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning(
                "project_registry: skipping unreadable adapter %s",
                adapter_path,
            )
            continue
        if not isinstance(data, dict):
            continue
        pid = data.get("project_id")
        if not isinstance(pid, str) or not pid:
            continue
        display = data.get("display_name") or pid
        projects.append(
            Project(
                project_id=pid,
                display_name=str(display),
                project_repo_root=None,
                is_default=False,
            )
        )
    return projects


def build_project_registry(
    *,
    app_repo_root: Path | None,
    default_project_id_override: str | None = None,
) -> ProjectRegistry:
    """Build a registry for a given FeishuOPC checkout.

    - Reads ``projects.jsonl`` if present (authoritative).
    - Otherwise falls back to scanning ``project-adapters/``.
    - If the user supplied ``default_project_id_override`` (e.g. from
      ``settings.default_project_id``) and that project exists in the
      loaded set but is not marked ``is_default``, the override wins.

    A registry with zero projects is legal — workflow/code-write services
    will simply report "no projects configured".
    """
    projects: list[Project] = []
    if app_repo_root is not None:
        jsonl_path = (
            app_repo_root
            / ".larkagent"
            / "secrets"
            / "projects"
            / "projects.jsonl"
        )
        projects = load_projects_jsonl(jsonl_path)
        if not projects:
            projects = discover_from_adapters(app_repo_root / "project-adapters")

    # Apply default_project_id_override if set
    if default_project_id_override:
        projects = [
            Project(
                project_id=p.project_id,
                display_name=p.display_name,
                project_repo_root=p.project_repo_root,
                is_default=(p.project_id == default_project_id_override),
                extra=p.extra,
            )
            for p in projects
        ]
        if not any(p.project_id == default_project_id_override for p in projects):
            logger.warning(
                "default_project_id=%r not found in registry; ignoring.",
                default_project_id_override,
            )

    return ProjectRegistry(projects)
