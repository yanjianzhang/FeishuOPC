"""A-3 Role Artifact Envelope.

Every ``dispatch_role_agent`` call produces one JSON file on disk
summarising what the child agent did. The file is the shared
substrate for:

* Human review & audit — a single ``cat`` reveals the full
  tool-call sequence, timing, token usage, and outcome.
* Replay — :mod:`feishu_agent.team.task_replay` reconstructs a
  team trace from the set of artifacts + the event transcript.
* Risk scoring — :func:`compute_risk_score` assigns a 0.0 – 1.0
  risk number per artifact; downstream review gates (future spec
  005) threshold on it.
* Team context — the ``teams/{root_trace_id}/`` directory mirrors
  Claude Code's mailbox concept at a coarser grain.

This module is deliberately dependency-free beyond stdlib. Writing
an artifact must NEVER block or crash a dispatch — callers swallow
errors to ``logger.warning`` (see ``tech_lead_executor`` in Wave 2).
Atomic writes via ``.tmp + rename`` mean readers never see partial
JSON even if the process is killed mid-write.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public truncation constants — exposed so tests can set their own bounds
# without monkey-patching module-private names.
# ---------------------------------------------------------------------------

ARGS_PREVIEW_MAX = 1000
RESULT_PREVIEW_MAX = 1500
OUTPUT_TEXT_MAX = 2048


class ArtifactStoreError(Exception):
    """Base class for all artifact-store errors."""


class ArtifactNotFoundError(ArtifactStoreError):
    """Raised when :meth:`ArtifactStore.read` can't find the artifact."""

    def __init__(self, root_trace_id: str, artifact_id: str) -> None:
        super().__init__(
            f"No artifact with id {artifact_id!r} under team "
            f"{root_trace_id!r}"
        )
        self.root_trace_id = root_trace_id
        self.artifact_id = artifact_id


# ---------------------------------------------------------------------------
# Record types
# ---------------------------------------------------------------------------


@dataclass
class ToolCallRecord:
    """One entry in :attr:`RoleArtifact.tool_calls`.

    ``arguments_preview`` / ``result_preview`` are already
    JSON-serialised + truncated strings. Keeping the previews as
    strings (not dicts) guarantees the envelope serialises the same
    way in every Python version and lets us apply a hard byte cap
    without structural surprises.
    """

    tool_name: str
    arguments_preview: str
    result_preview: str
    duration_ms: int
    is_error: bool
    started_at: int


@dataclass
class FileTouch:
    """Stub for full diff capture (OQ-004-5).

    Emitted once per invocation of ``write_project_code`` /
    ``write_project_code_batch`` / ``write_role_artifact`` /
    ``delete_project_code``. ``bytes_written`` is ``None`` for
    deletes.
    """

    path: str
    kind: str  # "write" | "batch_write" | "delete"
    bytes_written: int | None = None


@dataclass
class RoleArtifact:
    """The envelope. One per dispatch; written to
    ``teams/{root_trace_id}/artifacts/{role_name}-{artifact_id}.json``.

    Fields mirror :mod:`data-model` §4 with a single schema version
    field so future additions can stay backward-compatible: readers
    ignore unknown keys via dataclass ``__init__`` filtering (see
    :meth:`ArtifactStore.read`).
    """

    artifact_id: str  # == child_trace_id
    parent_trace_id: str
    root_trace_id: str  # top-most session; == parent_trace_id at depth 1
    role_name: str
    task: str
    acceptance_criteria: str
    started_at: int  # unix ms
    completed_at: int  # unix ms
    duration_ms: int
    success: bool
    stop_reason: str  # complete | timeout | tool_arg_loop | error | cancelled | max_turns
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    files_touched: list[FileTouch] = field(default_factory=list)
    risk_score: float = 0.0  # 0.0 – 1.0; filled by ``compute_risk_score``
    token_usage: dict[str, int] = field(default_factory=dict)
    output_text: str = ""  # truncated to ``OUTPUT_TEXT_MAX``
    error_message: str | None = None
    worktree_fallback: bool = False  # filled by B-3
    concurrency_group: str = ""  # resolved at dispatch time
    schema_version: int = 1


# ---------------------------------------------------------------------------
# Risk scoring
# ---------------------------------------------------------------------------


def _tool_effect(
    tool_name: str, specs_by_name: Mapping[str, Any] | None
) -> str:
    if not specs_by_name:
        # No lookup map → treat every tool as world (worst case).
        # Keeps the score conservative for callers that don't wire a
        # registry, which is the test-friendly default.
        return "world"
    spec = specs_by_name.get(tool_name)
    return getattr(spec, "effect", "world") if spec is not None else "world"


def _tool_target(
    tool_name: str, specs_by_name: Mapping[str, Any] | None
) -> str:
    if not specs_by_name:
        return "*"
    spec = specs_by_name.get(tool_name)
    return getattr(spec, "target", "*") if spec is not None else "*"


_EXTERNAL_TARGET_PREFIXES = (
    "world.git.remote",
    "world.bitable.write",
    "world.feishu",
)


def compute_risk_score(
    artifact: RoleArtifact,
    *,
    specs_by_name: Mapping[str, Any] | None = None,
) -> float:
    """Heuristic 0.0–1.0 score for a completed dispatch.

    Components:

    * ``base`` — ``0.08 × (# of world-effect tool calls)`` capped at
      ``0.6``. Purely read / self calls contribute nothing.
    * ``external_bonus`` — ``0.15 × (# of remote-git / bitable-write
      / feishu calls)`` capped at ``0.3``. This is the blast-radius
      dial: the same number of world calls is riskier when they
      leave the repo.
    * ``error_bonus`` — ``0.2`` when the dispatch failed. A failed
      read-only dispatch therefore scores ``0.2`` even though no
      world calls happened, which is deliberate: a failed run still
      warrants a review eyeball to understand why.

    The final sum is clamped to ``1.0``.

    ``specs_by_name`` — optional mapping ``{tool_name ->
    AgentToolSpec-like}`` so the helper can resolve each call's
    ``effect`` / ``target``. When omitted, every tool counts as
    ``effect="world"`` (conservative default — overestimates risk
    rather than underestimating it).
    """

    world_calls = sum(
        1
        for tc in artifact.tool_calls
        if _tool_effect(tc.tool_name, specs_by_name) == "world"
    )
    external_calls = sum(
        1
        for tc in artifact.tool_calls
        if _tool_target(tc.tool_name, specs_by_name).startswith(
            _EXTERNAL_TARGET_PREFIXES
        )
    )

    base = min(world_calls * 0.08, 0.6)
    external_bonus = min(external_calls * 0.15, 0.3)
    error_bonus = 0.2 if not artifact.success else 0.0
    return min(base + external_bonus + error_bonus, 1.0)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class ArtifactStore:
    """Read/write JSON artifacts under ``teams/{trace}/artifacts/``.

    Writes are atomic (tmp + rename), unique by artifact_id (UUID),
    so concurrent writes to different artifacts need no lock. Reads
    tolerate extra top-level keys (future forward-compat) and drop
    unknown keys silently.
    """

    def __init__(self, base_dir: Path) -> None:
        """``base_dir`` should be ``Path(settings.techbot_run_log_dir)``.

        The store lazily creates subdirectories on demand — we never
        ``mkdir`` at construction time so passing a non-existent or
        read-only path to tests is harmless until a write actually
        happens.
        """

        self._base_dir = Path(base_dir)

    # --- Path helpers ---------------------------------------------------

    def team_dir(self, root_trace_id: str) -> Path:
        """``{base}/teams/{root_trace_id}/`` — created on demand.

        Used as the umbrella for ``artifacts/``, ``pending/``,
        ``inbox/``, and ``transcript.jsonl``. All of those are
        created by their respective services, not by the store.
        """
        p = self._base_dir / "teams" / root_trace_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def artifacts_dir(self, root_trace_id: str) -> Path:
        p = self.team_dir(root_trace_id) / "artifacts"
        p.mkdir(exist_ok=True)
        return p

    def inbox_dir(self, root_trace_id: str) -> Path:
        """``{team_dir}/inbox/`` — reserved for future peer
        messaging. Created at first artifact write (T046) so even
        empty traces get the directory, making downstream tooling
        simpler."""
        p = self.team_dir(root_trace_id) / "inbox"
        p.mkdir(exist_ok=True)
        return p

    # --- Read / write ---------------------------------------------------

    def write(self, artifact: RoleArtifact) -> Path:
        """Serialise the artifact and write it atomically. Also
        creates the team's ``inbox/`` as a side-effect (T046) so
        there's always one canonical "touch" that materialises the
        full team dir.

        Returns the absolute file path on success. Raises any
        underlying ``OSError`` to the caller — we never swallow
        here so ``logger.warning`` at the TL level can capture the
        real reason.
        """

        target = self.artifacts_dir(artifact.root_trace_id) / (
            f"{artifact.role_name}-{artifact.artifact_id}.json"
        )
        tmp = target.with_suffix(".tmp")
        payload = json.dumps(
            asdict(artifact), ensure_ascii=False, indent=2
        )
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(target)  # atomic on POSIX

        # Eagerly create inbox/ so `ls teams/{trace}/` shows all four
        # expected subdirs even before the first inbox message is
        # written. Cheap (idempotent mkdir), and keeps the layout
        # predictable for operators.
        self.inbox_dir(artifact.root_trace_id)
        return target

    def read(
        self, root_trace_id: str, artifact_id: str
    ) -> RoleArtifact:
        """Look up an artifact by id under ``root_trace_id``.

        The role name is part of the filename but callers usually
        don't know it at read time, so we glob ``*-{artifact_id}.json``.
        """

        matches = list(
            self.artifacts_dir(root_trace_id).glob(
                f"*-{artifact_id}.json"
            )
        )
        if not matches:
            raise ArtifactNotFoundError(root_trace_id, artifact_id)
        return self._load(matches[0])

    def list(self, root_trace_id: str) -> list[RoleArtifact]:
        """Every artifact under ``root_trace_id`` in filename order.

        Filename order == role_name-artifact_id, which is not
        strictly chronological but stable across reads, so
        downstream tooling (replay) can diff two runs cleanly.
        """

        out: list[RoleArtifact] = []
        for p in sorted(self.artifacts_dir(root_trace_id).glob("*.json")):
            try:
                out.append(self._load(p))
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                # A partial/corrupt file should not take down the
                # whole list call. Record the incident and continue.
                logger.warning(
                    "artifact_store: skipping unreadable %s: %s", p, exc
                )
        return out

    # --- Internal -------------------------------------------------------

    def _load(self, path: Path) -> RoleArtifact:
        data = json.loads(path.read_text(encoding="utf-8"))
        return _role_artifact_from_dict(data)


# ---------------------------------------------------------------------------
# Forward-compat loader — keeps readers from crashing on newer schemas
# ---------------------------------------------------------------------------


def _role_artifact_from_dict(data: dict[str, Any]) -> RoleArtifact:
    """Construct a :class:`RoleArtifact` from an on-disk dict.

    Two nontrivial things:

    1. Future-proof: any extra top-level keys are dropped (filtered
       by ``RoleArtifact`` field names) so a reader running old code
       against a newer file doesn't explode.
    2. Nested dataclasses: ``tool_calls`` / ``files_touched`` are
       lists of dicts on disk; we rehydrate them into the right
       dataclass type here.
    """

    known_fields = {f for f in RoleArtifact.__dataclass_fields__}
    kwargs: dict[str, Any] = {
        k: v for k, v in data.items() if k in known_fields
    }
    kwargs["tool_calls"] = [
        _tool_call_from_dict(tc)
        for tc in data.get("tool_calls") or []
    ]
    kwargs["files_touched"] = [
        _file_touch_from_dict(ft)
        for ft in data.get("files_touched") or []
    ]
    return RoleArtifact(**kwargs)


def _tool_call_from_dict(data: dict[str, Any]) -> ToolCallRecord:
    known = {f for f in ToolCallRecord.__dataclass_fields__}
    return ToolCallRecord(**{k: v for k, v in data.items() if k in known})


def _file_touch_from_dict(data: dict[str, Any]) -> FileTouch:
    known = {f for f in FileTouch.__dataclass_fields__}
    return FileTouch(**{k: v for k, v in data.items() if k in known})


# ---------------------------------------------------------------------------
# Small serialisation helpers used by TL wiring (kept here so callers
# don't duplicate the truncation logic)
# ---------------------------------------------------------------------------


def truncate_preview(payload: Any, limit: int) -> str:
    """Serialise ``payload`` to JSON and truncate to ``limit`` chars.

    The envelope stores previews (not full args/results) because
    full payloads can balloon token-level logs — a single
    ``write_project_code_batch`` call can easily be >1MB of file
    content. Truncation happens at serialise time; downstream
    tooling is expected to find the full data via ``task_replay``
    against the event transcript if it needs it.

    Non-JSON-serialisable payloads fall back to ``repr()`` to avoid
    losing the record entirely.
    """

    try:
        serialised = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        serialised = repr(payload)
    if len(serialised) <= limit:
        return serialised
    return serialised[: max(limit - 3, 0)] + "..."


__all__ = [
    "ArtifactNotFoundError",
    "ArtifactStore",
    "ArtifactStoreError",
    "ARGS_PREVIEW_MAX",
    "RESULT_PREVIEW_MAX",
    "OUTPUT_TEXT_MAX",
    "FileTouch",
    "RoleArtifact",
    "ToolCallRecord",
    "compute_risk_score",
    "truncate_preview",
]
