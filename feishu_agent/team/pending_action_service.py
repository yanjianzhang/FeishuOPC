"""Pending-action persistence.

On-disk layout (spec 004 / A-3):

* **Legacy** (pre-004): ``{run_log_dir}/pending/{trace}.json`` —
  one flat directory per runtime install.
* **Team-scoped** (post-004): ``{run_log_dir}/teams/{root_trace_id}/
  pending/{trace}.json``. Each top-level session gets its own
  subtree; pending confirmations co-locate with the artifacts,
  transcript, and inbox for that team.

This service keeps both layouts readable for the duration of one
sprint cycle so mixed-deployment rollouts don't orphan in-flight
confirmations. Writes go to the new location when the caller can
supply a ``root_trace_id``; callers that still run in the legacy
hot path (``git_sync_preflight``) fall back to the flat layout
transparently.

The API is deliberately additive — every existing call site
(``save(action)``, ``load(trace)``, ``delete(trace)``,
``load_by_chat_id(chat)``) keeps its old shape and semantics.
New A-3 call sites pass the optional keyword-only argument
``root_trace_id=`` or construct the service with a
``teams_pending_root=`` so the team-scoped path kicks in.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class PendingAction:
    trace_id: str
    chat_id: str
    role_name: str
    action_type: str
    action_args: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    confirmation_message_id: str | None = None

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PendingAction:
        return cls(
            trace_id=str(data.get("trace_id") or ""),
            chat_id=str(data.get("chat_id") or ""),
            role_name=str(data.get("role_name") or ""),
            action_type=str(data.get("action_type") or ""),
            action_args=data.get("action_args") or {},
            created_at=str(data.get("created_at") or ""),
            confirmation_message_id=data.get("confirmation_message_id"),
        )


_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")


class PendingActionService:
    """Read/write pending-action JSON files.

    Construction shapes:

    * ``PendingActionService(pending_dir)`` — legacy flat layout.
      This is what every caller before A-3 wires up; their
      ``pending_dir`` is ``<run_log_dir>/pending``. Writes without
      a ``root_trace_id`` land here; reads check here last.

    * ``PendingActionService(pending_dir, teams_pending_root=...)``
      — A-3 team-scoped layout. ``teams_pending_root`` should be
      ``<run_log_dir>/teams``. Writes with a ``root_trace_id``
      land in ``<teams_pending_root>/<root>/pending/``; reads
      scan every ``teams/*/pending/`` dir first, falling back to
      the legacy flat dir.

    The bifurcation exists because:

    1. Not every caller knows a ``root_trace_id`` at save time —
       ``git_sync_preflight`` currently builds actions before a TL
       session has taken ownership. These still write to the
       legacy dir.
    2. The load path must tolerate either layout for one sprint
       cycle while old records age out (see
       ``A-3-artifact-envelope.md`` §PendingActionService).
    """

    def __init__(
        self,
        pending_dir: Path,
        *,
        teams_pending_root: Path | None = None,
    ) -> None:
        self._dir = pending_dir
        self._teams_root = teams_pending_root

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _team_pending_dir(self, root_trace_id: str) -> Path | None:
        """Resolve ``<teams_pending_root>/<root>/pending/`` or
        ``None`` if this service wasn't constructed with a
        team-aware root. ``_SAFE_ID_RE`` on the trace id guards
        against path traversal; non-matching ids fall back to the
        flat dir via :meth:`_target_dir_for_save`."""
        if self._teams_root is None or not root_trace_id:
            return None
        if not _SAFE_ID_RE.match(root_trace_id):
            return None
        return self._teams_root / root_trace_id / "pending"

    def _iter_team_pending_dirs(self) -> Iterator[Path]:
        """Iterate every ``<teams_pending_root>/*/pending/`` that
        exists. No-op when ``teams_pending_root`` is unset or
        hasn't been materialised yet (first run). Read paths
        collect candidates from this iterator, sort by mtime, and
        return the most-recent match — so ordering across teams
        is deterministic per-read."""
        if self._teams_root is None or not self._teams_root.exists():
            return
        for team_dir in self._teams_root.iterdir():
            p = team_dir / "pending"
            if p.is_dir():
                yield p

    def _target_dir_for_save(self, root_trace_id: str | None) -> Path:
        team_dir = self._team_pending_dir(root_trace_id or "")
        return team_dir if team_dir is not None else self._dir

    def _candidate_paths(self, trace_id: str) -> list[Path]:
        """Every on-disk location where ``<trace_id>.json`` might
        live, in lookup priority order (team dirs first, legacy
        last)."""
        out: list[Path] = []
        for team_pending in self._iter_team_pending_dirs():
            out.append(team_pending / f"{trace_id}.json")
        out.append(self._dir / f"{trace_id}.json")
        return out

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save(
        self,
        action: PendingAction,
        *,
        root_trace_id: str | None = None,
    ) -> Path:
        """Persist ``action`` and return its on-disk path.

        When ``root_trace_id`` is supplied AND the service was
        constructed with a ``teams_pending_root``, the write lands
        in the team-scoped directory. Otherwise the write uses
        the legacy flat dir — this is the path that existing
        callers (``git_sync_preflight``, pre-A-3 tests) still take.
        """
        if not _SAFE_ID_RE.match(action.trace_id):
            raise ValueError(f"Unsafe trace_id: {action.trace_id!r}")
        target_dir = self._target_dir_for_save(root_trace_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{action.trace_id}.json"
        path.write_text(
            json.dumps(action.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def load(self, trace_id: str) -> PendingAction | None:
        """Read the first pending action matching ``trace_id``.

        Priority: team-scoped dirs first (most-recent mtime wins
        across teams, though collisions are essentially impossible
        given UUID-ish trace ids), then the legacy flat dir.
        """
        if not _SAFE_ID_RE.match(trace_id):
            return None
        matches = [p for p in self._candidate_paths(trace_id) if p.exists()]
        if not matches:
            return None
        # Tie-break by mtime desc; near-instant for small dirs and
        # correct even when a stale legacy file happens to share
        # the id with a freshly-written team one (should not occur
        # but we stay defensive — the newer file wins).
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return _parse_path(matches[0])

    def delete(self, trace_id: str) -> bool:
        """Remove every on-disk copy of ``trace_id``.

        Returns ``True`` when at least one file was deleted.
        Idempotent — a second call with no surviving copies
        returns ``False`` without raising.
        """
        if not _SAFE_ID_RE.match(trace_id):
            return False
        deleted = False
        for path in self._candidate_paths(trace_id):
            if path.exists():
                try:
                    path.unlink()
                    deleted = True
                except OSError:
                    # Leave as-is; delete is best-effort, the
                    # caller already handled whatever pending flow
                    # prompted the removal.
                    continue
        return deleted

    def load_by_chat_id(self, chat_id: str) -> PendingAction | None:
        """Most-recent pending action for ``chat_id`` across every
        dir this service knows about. Used by the Feishu confirm/
        cancel flow where the user reply only carries a chat id."""
        candidates: list[Path] = []
        for team_pending in self._iter_team_pending_dirs():
            candidates.extend(team_pending.glob("*.json"))
        if self._dir.exists():
            candidates.extend(self._dir.glob("*.json"))
        if not candidates:
            return None
        for path in sorted(
            candidates, key=lambda p: p.stat().st_mtime, reverse=True
        ):
            data = _read_json(path)
            if isinstance(data, dict) and data.get("chat_id") == chat_id:
                return PendingAction.from_dict(data)
        return None


# ---------------------------------------------------------------------------
# Internals — tiny helpers kept module-private
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _parse_path(path: Path) -> PendingAction | None:
    data = _read_json(path)
    if not isinstance(data, dict):
        return None
    return PendingAction.from_dict(data)
