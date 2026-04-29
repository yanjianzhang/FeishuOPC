"""Role-scoped artifact writer.

Rationale
---------
Tech lead was shouldering too much: every review, test plan, research
note and UX spec had to flow through TL → ``write_project_code`` → PR.
That's wasteful (TL doesn't need to own the content of a review) and
bottlenecks throughput.

So each specialist role (reviewer / qa_tester / repo_inspector /
researcher / spec_linker / ux_designer) gets a **narrowly-scoped**
write tool:

- one single ``allowed_write_root`` per role, per project
- UTF-8 text only (artifacts are markdown/plain-text docs, never code)
- hard size cap (256KB default — a review report shouldn't need more)
- path-containment check (``is_relative_to``)
- secret scanner runs BEFORE write (same scanner TL's code-write uses)
- optional audit log

The writer never escalates privileges. It cannot write source code;
it cannot run git; it cannot push. It can only produce markdown-ish
artifacts inside its allocated subdir. The tech lead then picks those
artifacts up on his next PR.

This keeps "only tech_lead can push" intact while dropping TL's
write-through load for pure-text specialist output.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field

from feishu_agent.core.agent_types import AgentToolSpec
from feishu_agent.tools.code_write_service import CodeWriteAuditLog
from feishu_agent.tools.feishu_agent_tools import _tool_spec
from feishu_agent.tools.secret_scanner import (
    SecretDetectedError,
    ensure_clean,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RoleArtifactError(Exception):
    code: str = "ROLE_ARTIFACT_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class RoleArtifactDisabledError(RoleArtifactError):
    code = "ROLE_ARTIFACT_DISABLED"


class RoleArtifactPathError(RoleArtifactError):
    code = "ROLE_ARTIFACT_PATH_INVALID"


class RoleArtifactOversizeError(RoleArtifactError):
    code = "ROLE_ARTIFACT_OVERSIZE"


class RoleArtifactSecretError(RoleArtifactError):
    code = "ROLE_ARTIFACT_SECRET_DETECTED"


class RoleArtifactEmptyError(RoleArtifactError):
    code = "ROLE_ARTIFACT_EMPTY"


# ---------------------------------------------------------------------------
# Tool spec
# ---------------------------------------------------------------------------


class WriteRoleArtifactArgs(BaseModel):
    path: str = Field(
        description=(
            "Relative path of the artifact INSIDE your allowed write "
            "root (e.g. '3-1-review.md' or 'subdir/notes.md'). Must "
            "not escape via '..'. Creates parent dirs as needed."
        )
    )
    content: str = Field(
        description=(
            "Full UTF-8 text content of the artifact. Markdown is "
            "preferred. Don't paste source code here — TL is the only "
            "one allowed to write source; you write the narrative / "
            "analysis / plan that lives next to it."
        )
    )
    summary: str = Field(
        description=(
            "One-sentence summary of what this artifact covers, so the "
            "tech lead can scan-pick it. Shows up in audit log + the "
            "in-thread write notification."
        )
    )


ROLE_ARTIFACT_TOOL_SPECS: list[AgentToolSpec] = [
    _tool_spec(
        "write_role_artifact",
        "Persist your role's output as a UTF-8 text artifact into your "
        "dedicated subdir of the project repo. Use this at the END of "
        "your task so the tech lead can read your report from disk "
        "instead of scrolling chat. You cannot write source code or "
        "escape your subdir; that's by design.",
        WriteRoleArtifactArgs,
    ),
]


@dataclass
class RoleArtifactResult:
    role: str
    project_id: str
    path: str
    bytes_written: int
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "project_id": self.project_id,
            "path": self.path,
            "bytes_written": self.bytes_written,
            "summary": self.summary,
        }


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


ThreadUpdateFn = Callable[[str], Awaitable[None] | None]


# ---------------------------------------------------------------------------
# Mixin — keeps the 6 role executors DRY
# ---------------------------------------------------------------------------


class RoleArtifactToolsMixin:
    """Add this mixin to a role executor that wants ``write_role_artifact``.

    Requirements on the host class:

    - has ``self._role_artifact_writer: RoleArtifactWriter | None``

    The mixin handles tool-spec exposure (only when the writer is
    configured → fail-closed on mis-wired deployments) and tool
    dispatch for ``write_role_artifact``.
    """

    _role_artifact_writer: "RoleArtifactWriter | None"

    def role_artifact_tool_specs(self) -> list[AgentToolSpec]:
        if self._role_artifact_writer is None:
            return []
        return list(ROLE_ARTIFACT_TOOL_SPECS)

    async def handle_role_artifact_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any] | None:
        if tool_name != "write_role_artifact":
            return None
        if self._role_artifact_writer is None:
            return {
                "error": RoleArtifactDisabledError.code,
                "message": (
                    "write_role_artifact is not configured for this "
                    "role in the current deployment (no allowed_write_root)."
                ),
            }
        try:
            parsed = WriteRoleArtifactArgs.model_validate(arguments)
            result = self._role_artifact_writer.write(
                path=parsed.path,
                content=parsed.content,
                summary=parsed.summary,
            )
            return result.to_dict()
        except RoleArtifactError as exc:
            return {"error": exc.code, "message": exc.message}


class RoleArtifactWriter:
    """One instance per (role, project). Can only write under
    ``allowed_write_root``."""

    DEFAULT_MAX_BYTES = 256 * 1024

    def __init__(
        self,
        *,
        role_name: str,
        project_id: str,
        allowed_write_root: Path,
        audit_log: CodeWriteAuditLog | None = None,
        thread_update: ThreadUpdateFn | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.role_name = role_name
        self.project_id = project_id
        self._root = Path(allowed_write_root).resolve()
        self._audit = audit_log
        self._thread_update = thread_update
        self._max_bytes = max_bytes

    @property
    def allowed_write_root(self) -> Path:
        return self._root

    def write(
        self,
        *,
        path: str,
        content: str,
        summary: str,
    ) -> RoleArtifactResult:
        if not path or not path.strip():
            raise RoleArtifactPathError("path must be non-empty.")
        if not content:
            raise RoleArtifactEmptyError("content must be non-empty.")
        summary = (summary or "").strip()
        if not summary:
            raise RoleArtifactError("summary must be non-empty (one sentence).")

        rel = Path(path.strip())
        if rel.is_absolute():
            raise RoleArtifactPathError(
                f"path must be relative to the role's allowed_write_root "
                f"({self._root}); got absolute path."
            )
        target = (self._root / rel).resolve()
        try:
            target.relative_to(self._root)
        except ValueError as exc:
            raise RoleArtifactPathError(
                f"path {path!r} escapes the role root {self._root}."
            ) from exc

        # Defense-in-depth: reject any path segment that looks like a
        # secret container even within docs/. ``.env.example`` /
        # ``.env.template`` / ``.env.sample`` are common, legitimate
        # onboarding / developer-docs artifacts so we explicitly allow
        # those three exact segment names — bare ``.env``, ``.envrc``,
        # ``.env.local``, etc. remain blocked.
        _ALLOWED_ENV_TEMPLATE_NAMES = (
            ".env.example",
            ".env.template",
            ".env.sample",
        )
        for part in rel.parts:
            lower = part.lower()
            if lower.startswith(".env") and lower not in _ALLOWED_ENV_TEMPLATE_NAMES:
                raise RoleArtifactPathError(
                    f"path segment {part!r} is not allowed in a role artifact."
                )
            if (
                lower == "secrets"
                or lower.startswith(".git")
                or lower.endswith(".pem")
                or lower.endswith(".key")
            ):
                raise RoleArtifactPathError(
                    f"path segment {part!r} is not allowed in a role artifact."
                )

        encoded = content.encode("utf-8")
        if len(encoded) > self._max_bytes:
            raise RoleArtifactOversizeError(
                f"Artifact is {len(encoded)} bytes; hard cap is "
                f"{self._max_bytes} bytes. Split into multiple files or "
                f"summarize."
            )

        try:
            ensure_clean(content, path=str(target))
        except SecretDetectedError as exc:
            raise RoleArtifactSecretError(
                f"Artifact at {rel} contains secret-shaped content: "
                f"{exc}. Replace the value with an env-var reference "
                f"or a clearly-fake placeholder."
            ) from exc

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

        rel_str = str(target.relative_to(self._root))
        result = RoleArtifactResult(
            role=self.role_name,
            project_id=self.project_id,
            path=rel_str,
            bytes_written=len(encoded),
            summary=summary,
        )
        self._audit_append(
            {
                "event": "role_artifact_write",
                **result.to_dict(),
            }
        )
        self._push_thread_line(result)
        return result

    # -- internal -------------------------------------------------------

    def _audit_append(self, record: dict[str, Any]) -> None:
        if self._audit is None:
            return
        try:
            self._audit.append({"ts": time.time(), **record})
        except Exception:  # pragma: no cover
            logger.warning("role artifact audit append failed", exc_info=True)

    # -- executor glue --------------------------------------------------

    def tool_specs(self) -> list[AgentToolSpec]:
        """Convenience for role executors: return the tool spec list
        when a writer is configured."""
        return list(ROLE_ARTIFACT_TOOL_SPECS)

    def try_handle(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Dispatch helper: returns the tool result dict if
        ``tool_name == 'write_role_artifact'``, else ``None``.

        On any ``RoleArtifactError`` the error is turned into a
        ``{error, message}`` response shape consistent with other
        services, so the LLM can see why the write was refused without
        crashing the sub-agent loop.
        """
        if tool_name != "write_role_artifact":
            return None
        try:
            parsed = WriteRoleArtifactArgs.model_validate(arguments)
            result = self.write(
                path=parsed.path,
                content=parsed.content,
                summary=parsed.summary,
            )
            return result.to_dict()
        except RoleArtifactError as exc:
            return {"error": exc.code, "message": exc.message}

    def _push_thread_line(self, result: RoleArtifactResult) -> None:
        if self._thread_update is None:
            return
        try:
            fn = self._thread_update
            msg = (
                f"📝 {result.role} 写入 {result.path} "
                f"({result.bytes_written}B): {result.summary}"
            )
            ret = fn(msg)
            if ret is not None and hasattr(ret, "__await__"):
                # best-effort fire-and-forget of the coroutine; role
                # executors run inside async contexts where the caller
                # may prefer to await explicitly. If no loop is
                # running, we swallow — the write still succeeded.
                # ``get_event_loop`` was deprecated in 3.10 and removed
                # in 3.14 when there is no running loop; use
                # ``get_running_loop`` which is the canonical way to
                # grab the active loop or raise.
                import asyncio

                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:  # pragma: no cover - sync caller
                    loop = None
                if loop is not None:
                    loop.create_task(ret)  # type: ignore[arg-type]
        except Exception:  # pragma: no cover
            logger.debug("role artifact thread_update failed", exc_info=True)
