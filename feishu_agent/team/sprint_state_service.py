from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

import yaml

from feishu_agent.schemas.tech_lead import TechLeadStateChange

try:  # POSIX only; Windows gets a best-effort no-op.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover
    _fcntl = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from feishu_agent.team.task_graph import Task

logger = logging.getLogger(__name__)

LIST_KEY_TO_STATUS = {
    "planned": "planned",
    "in_progress": "in-progress",
    "review": "review",
    "completed": "done",
    "blocked": "blocked",
}

STATUS_TO_LIST_KEY = {
    "planned": "planned",
    "in-progress": "in_progress",
    "review": "review",
    "done": "completed",
    "blocked": "blocked",
}

AUTO_ADVANCE_TARGET = {
    "planned": "in-progress",
    "in-progress": "review",
    "review": "done",
}

ALLOWED_TRANSITIONS = {
    "planned": {"in-progress", "blocked"},
    "in-progress": {"review", "done", "blocked"},
    "review": {"done", "in-progress", "blocked"},
    "blocked": {"planned", "in-progress"},
    "done": set(),
}


class SprintStateError(Exception):
    pass


@dataclass
class StoryLocation:
    story_key: str
    status: str
    path: list[str]
    kind: str
    list_parent_path: list[str] | None = None
    list_name: str | None = None

    @property
    def path_str(self) -> str:
        return ".".join(self.path)


class SprintStateService:
    def __init__(self, repo_root: Path, status_relative_path: str) -> None:
        self.repo_root = repo_root
        self.status_relative_path = status_relative_path

    @property
    def status_path(self) -> Path:
        return self.repo_root / self.status_relative_path

    def load_status_data(self) -> dict[str, Any]:
        if not self.status_path.exists():
            raise SprintStateError(f"Sprint status file missing: {self.status_relative_path}")
        return yaml.safe_load(self.status_path.read_text(encoding="utf-8")) or {}

    def save_status_data(self, data: dict[str, Any]) -> None:
        self.status_path.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    def collect_story_locations(self, data: dict[str, Any]) -> list[StoryLocation]:
        locations: list[StoryLocation] = []
        self._walk(data, path=[], locations=locations)
        return locations

    def _walk(self, node: Any, *, path: list[str], locations: list[StoryLocation]) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                next_path = [*path, str(key)]
                if isinstance(value, str) and value in ALLOWED_TRANSITIONS:
                    locations.append(
                        StoryLocation(
                            story_key=str(key),
                            status=value,
                            path=next_path,
                            kind="mapping",
                        )
                    )
                elif isinstance(value, list) and key in LIST_KEY_TO_STATUS:
                    for item in value:
                        if isinstance(item, str):
                            locations.append(
                                StoryLocation(
                                    story_key=item,
                                    status=LIST_KEY_TO_STATUS[key],
                                    path=[*next_path, item],
                                    kind="list",
                                    list_parent_path=path,
                                    list_name=key,
                                )
                            )
                elif isinstance(value, (dict, list)):
                    self._walk(value, path=next_path, locations=locations)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                self._walk(item, path=[*path, str(index)], locations=locations)

    def resolve_story(
        self,
        data: dict[str, Any],
        story_key_hint: str,
    ) -> tuple[str, list[StoryLocation]]:
        hint = story_key_hint.strip().lower()
        locations = self.collect_story_locations(data)
        exact = [loc for loc in locations if loc.story_key.lower() == hint]
        if exact:
            return exact[0].story_key, exact

        partial_keys = sorted({loc.story_key for loc in locations if hint in loc.story_key.lower()})
        if not partial_keys:
            raise SprintStateError(f"Story not found: {story_key_hint}")
        if len(partial_keys) > 1:
            raise SprintStateError(
                f"Story hint '{story_key_hint}' is ambiguous: {', '.join(partial_keys[:5])}"
            )
        story_key = partial_keys[0]
        return story_key, [loc for loc in locations if loc.story_key == story_key]

    def apply_transition(
        self,
        *,
        data: dict[str, Any],
        story_key: str,
        to_status: str,
        reason: str,
        dry_run: bool,
    ) -> list[TechLeadStateChange]:
        _, locations = self.resolve_story(data, story_key)
        changes: list[TechLeadStateChange] = []
        seen_pairs: set[tuple[str, str]] = set()

        for location in locations:
            pair = (location.story_key, location.status)
            if pair in seen_pairs and location.kind == "mapping":
                continue
            seen_pairs.add(pair)
            from_status = location.status
            if from_status == to_status:
                continue
            allowed = ALLOWED_TRANSITIONS.get(from_status, set())
            if to_status not in allowed:
                raise SprintStateError(
                    f"Illegal transition for {story_key}: {from_status} -> {to_status}"
                )

            changes.append(
                TechLeadStateChange(
                    story_key=location.story_key,
                    from_status=from_status,
                    to_status=to_status,
                    reason=reason,
                    locations=[location.path_str],
                )
            )

        if not dry_run:
            for location in locations:
                if location.status == to_status:
                    continue
                self._apply_single_location(data, location, to_status)

        return changes

    def propose_next_transition(self, current_status: str) -> str:
        if current_status not in AUTO_ADVANCE_TARGET:
            raise SprintStateError(f"No automatic transition defined for status: {current_status}")
        return AUTO_ADVANCE_TARGET[current_status]

    def _apply_single_location(self, data: dict[str, Any], location: StoryLocation, to_status: str) -> None:
        if location.kind == "mapping":
            parent = self._resolve_container(data, location.path[:-1])
            parent[location.path[-1]] = to_status
            return

        if not location.list_parent_path or not location.list_name:
            raise SprintStateError(f"Invalid list location for {location.story_key}")

        parent = self._resolve_container(data, location.list_parent_path)
        source_list = parent.setdefault(location.list_name, [])
        if location.story_key in source_list:
            source_list.remove(location.story_key)

        destination_key = STATUS_TO_LIST_KEY[to_status]
        destination_list = parent.setdefault(destination_key, [])
        if location.story_key not in destination_list:
            destination_list.append(location.story_key)

    def advance(
        self,
        records: list[Any],
        *,
        story_key: str | None = None,
        to_status: str | None = None,
        reason: str = "",
        dry_run: bool = False,
    ) -> list[TechLeadStateChange]:
        data = self.load_status_data()
        if story_key:
            resolved_key = story_key
        else:
            # ``records`` is populated by the caller from
            # ``ProgressSyncService.read_records``, but that service is
            # rooted at the *agent* repo while ``sprint-status.yaml``
            # lives in the *project* repo. In multi-repo deployments the
            # caller therefore can't hand us meaningful records without
            # also carrying a second repo root, so we treat ``records``
            # as a best-effort hint and fall back to picking from the
            # status file we just loaded (which is always correct, no
            # matter which repo it lives in).
            try:
                resolved_key = self._pick_next_story(records)
            except SprintStateError:
                resolved_key = self._pick_next_story_from_data(data)
        canonical_key, locations = self.resolve_story(data, resolved_key)
        current_status = self._pick_status_for_auto_advance(locations)
        target = to_status or self.propose_next_transition(current_status)
        changes = self.apply_transition(
            data=data,
            story_key=canonical_key,
            to_status=target,
            reason=reason,
            dry_run=dry_run,
        )
        if not dry_run and changes:
            # B-1 dual-write (M2/M5 fix) — the legacy save + the
            # tasks-block upsert now happen inside the SAME
            # ``_locked_for_tasks()`` window so a concurrent writer
            # cannot observe a half-updated pair. Previously we took
            # the lock N times (once per change) AFTER saving
            # unlocked; both the ordering and the per-change cost were
            # wrong. Errors in the upsert path are still demoted to
            # warnings because the legacy list remains the
            # authoritative view and ``load_tasks`` can always
            # synthesise from it.
            with self._locked_for_tasks():
                self.save_status_data(data)
                try:
                    self._apply_changes_to_tasks_block(changes)
                except Exception:
                    logger.warning(
                        "tasks-block upsert failed post-advance "
                        "(%d changes); legacy state still authoritative",
                        len(changes),
                        exc_info=True,
                    )
        return changes

    def _apply_changes_to_tasks_block(
        self, changes: list[TechLeadStateChange]
    ) -> None:
        """In-lock helper for :meth:`advance` — load the ``tasks:``
        block, upsert one entry per change (or insert a minimal new
        entry if missing), and persist. The caller MUST already hold
        :meth:`_locked_for_tasks`. We reload ``status_data`` inside
        the lock so we don't clobber concurrent edits to unrelated
        keys."""
        from feishu_agent.team.task_graph import Task

        try:
            data = self.load_status_data()
        except SprintStateError:
            data = {}
        raw_tasks = data.get("tasks") or []
        tasks: list[Task] = [
            Task.from_dict(e)
            for e in raw_tasks
            if isinstance(e, dict)
        ]
        by_id = {t.id: t for t in tasks}
        now = int(time.time())
        for change in changes:
            coerced = _coerce_status(change.to_status)
            existing = by_id.get(change.story_key)
            if existing is not None:
                existing.status = coerced
                existing.updated_at = now
            else:
                tasks.append(
                    Task(
                        id=change.story_key,
                        status=coerced,
                        created_at=now,
                        updated_at=now,
                    )
                )
        data["tasks"] = [t.to_dict() for t in tasks]
        self.save_status_data(data)

    @staticmethod
    def _pick_next_story(records: list[Any]) -> str:
        prioritized = ("review", "in-progress", "planned")
        for status in prioritized:
            for record in records:
                if record.status == status:
                    return record.story_key or record.native_key
        raise SprintStateError("No review / in-progress / planned story found to advance.")

    def _pick_next_story_from_data(self, data: dict[str, Any]) -> str:
        """Pick the next story to advance using the loaded status data.

        Used as a fallback when the caller can't supply usable
        ``records`` (see ``advance`` for why). Picks by the same
        priority as ``_pick_next_story`` — review > in-progress >
        planned — and breaks ties by the first location reported by
        ``collect_story_locations`` (which walks the YAML in
        insertion order, matching user expectation of "top of the
        list").
        """
        prioritized = ("review", "in-progress", "planned")
        locations = self.collect_story_locations(data)
        for status in prioritized:
            for loc in locations:
                if loc.status == status:
                    return loc.story_key
        raise SprintStateError(
            "No review / in-progress / planned story found to advance."
        )

    @staticmethod
    def _pick_status_for_auto_advance(locations: list[StoryLocation]) -> str:
        ordered = ("review", "in-progress", "planned")
        for status in ordered:
            if any(loc.status == status for loc in locations):
                return status
        raise SprintStateError("Resolved story is not in an auto-advanceable status.")

    def _resolve_container(self, data: dict[str, Any], path: list[str]) -> Any:
        node: Any = data
        for key in path:
            node = node[key]
        return node

    # ------------------------------------------------------------------
    # B-1 task-graph extensions — ``tasks:`` top-level key
    # ------------------------------------------------------------------
    #
    # These methods live behind a file lock (``.task-graph.lock`` next
    # to the sprint YAML) so multiple fan-out branches on the same
    # process tree can read-modify-write the ``tasks:`` block without
    # racing. The lock is held for the critical section only
    # (O(milliseconds)); we don't hold it across claim-holder work.
    #
    # Read-compat: the ``tasks:`` key is optional. When missing,
    # :meth:`load_tasks` synthesises entries from the legacy flat
    # ``in_progress/planned/review/completed/blocked`` lists so B-1
    # callers see a coherent DAG from day one. Writes go through
    # :meth:`write_tasks`, which only touches the ``tasks:`` block —
    # the legacy lists stay untouched for read-compat until a later
    # spec formally deprecates them.

    _TASK_LOCK_NAME = ".task-graph.lock"

    @property
    def _task_lock_path(self) -> Path:
        return self.repo_root / self._TASK_LOCK_NAME

    @contextmanager
    def _locked_for_tasks(self) -> Iterator[None]:
        """Exclusive flock on ``<repo_root>/.task-graph.lock``.

        We deliberately keep this lock file outside ``.git/`` so a
        rebase or worktree checkout doesn't accidentally remove it.
        POSIX only; on Windows ``fcntl`` is absent and we fall
        through without locking — a deliberate simplification
        matching the rest of the codebase's POSIX-only fcntl usage.
        """
        if _fcntl is None:  # pragma: no cover — Windows
            yield
            return
        self.repo_root.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            str(self._task_lock_path), os.O_RDWR | os.O_CREAT, 0o644
        )
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX)
            yield
        finally:
            try:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def load_tasks(self) -> list["Task"]:
        """Return the task-graph view of the sprint file.

        When the file has a ``tasks:`` block, we prefer it verbatim.
        Otherwise we coerce the legacy flat lists into minimal
        :class:`~feishu_agent.team.task_graph.Task` entries so a
        fresh sprint picked up by B-1 still has a runnable DAG
        (flat = no dependencies, no concurrency groups).
        """
        from feishu_agent.team.task_graph import Task

        try:
            data = self.load_status_data()
        except SprintStateError:
            return []
        raw_tasks = data.get("tasks") or []
        if isinstance(raw_tasks, list) and raw_tasks:
            out: list[Task] = []
            for entry in raw_tasks:
                if not isinstance(entry, dict):
                    continue
                out.append(Task.from_dict(entry))
            return out

        # Read-compat synth from legacy lists. We don't persist this
        # projection; callers that want it materialised can call
        # ``write_tasks(load_tasks())`` explicitly. Synthesised
        # ``created_at`` uses ``0`` so insertion order via the
        # walk is deterministic across loads.
        synth: list[Task] = []
        for loc in self.collect_story_locations(data):
            synth.append(
                Task(
                    id=loc.story_key,
                    status=_coerce_status(loc.status),
                    assignee=None,
                    blocked_by=[],
                    blocks=[],
                    concurrency_group=None,
                )
            )
        return synth

    def write_tasks(self, tasks: list["Task"]) -> None:
        """Overwrite the ``tasks:`` key only. Holds the task-graph
        file lock for the full read-modify-write; the rest of the
        YAML (current_sprint, config, etc.) is round-tripped
        untouched. Creates the lock file on first call."""
        with self._locked_for_tasks():
            try:
                data = self.load_status_data()
            except SprintStateError:
                data = {}
            data["tasks"] = [t.to_dict() for t in tasks]
            self.save_status_data(data)

    def update_task(
        self,
        task_id: str,
        mutator: Callable[["Task"], "Task"],
    ) -> "Task":
        """CAS-style single-task update under lock.

        Loads the full task list, locates ``task_id``, runs the
        mutator, and writes the list back. The mutator is free to
        raise — we propagate the exception without touching disk,
        so failed mutations leave the YAML intact.
        """
        from feishu_agent.team.task_graph import Task, TaskNotFoundError

        with self._locked_for_tasks():
            try:
                data = self.load_status_data()
            except SprintStateError:
                data = {}
            raw_tasks = data.get("tasks") or []
            tasks: list[Task] = []
            target_idx: int | None = None
            for idx, entry in enumerate(raw_tasks):
                if not isinstance(entry, dict):
                    continue
                task = Task.from_dict(entry)
                if task.id == task_id:
                    target_idx = len(tasks)
                tasks.append(task)
            if target_idx is None:
                raise TaskNotFoundError(task_id)
            mutated = mutator(tasks[target_idx])
            tasks[target_idx] = mutated
            data["tasks"] = [t.to_dict() for t in tasks]
            self.save_status_data(data)
            return mutated

    def update_tasks_batch(
        self,
        mutator: Callable[[list["Task"]], list["Task"]],
    ) -> list["Task"]:
        """Full-list mutator variant for bulk updates.

        Used primarily by ``TaskGraph.release_expired`` which
        flips multiple tasks in one pass. The lock / persist
        contract matches :meth:`update_task`.
        """
        from feishu_agent.team.task_graph import Task

        with self._locked_for_tasks():
            try:
                data = self.load_status_data()
            except SprintStateError:
                data = {}
            raw_tasks = data.get("tasks") or []
            tasks: list[Task] = [
                Task.from_dict(e)
                for e in raw_tasks
                if isinstance(e, dict)
            ]
            updated = mutator(tasks)
            data["tasks"] = [t.to_dict() for t in updated]
            self.save_status_data(data)
            return updated

    def upsert_task(
        self,
        task_id: str,
        *,
        status: str,
        concurrency_group: str | None = None,
        blocked_by: list[str] | None = None,
        blocks: list[str] | None = None,
    ) -> "Task":
        """Insert a :class:`Task` entry if missing, else update its
        status + timestamps. Used by :meth:`advance` to keep the
        legacy list schema and the new DAG schema consistent."""
        from feishu_agent.team.task_graph import Task

        coerced_status = _coerce_status(status)
        now = int(time.time())

        with self._locked_for_tasks():
            try:
                data = self.load_status_data()
            except SprintStateError:
                data = {}
            raw_tasks = data.get("tasks") or []
            tasks: list[Task] = [
                Task.from_dict(e)
                for e in raw_tasks
                if isinstance(e, dict)
            ]
            found: Task | None = None
            for t in tasks:
                if t.id == task_id:
                    found = t
                    break
            if found is None:
                found = Task(
                    id=task_id,
                    status=coerced_status,
                    concurrency_group=concurrency_group,
                    blocked_by=list(blocked_by or []),
                    blocks=list(blocks or []),
                    created_at=now,
                    updated_at=now,
                )
                tasks.append(found)
            else:
                found.status = coerced_status
                if concurrency_group is not None:
                    found.concurrency_group = concurrency_group
                if blocked_by is not None:
                    found.blocked_by = list(blocked_by)
                if blocks is not None:
                    found.blocks = list(blocks)
                found.updated_at = now
            data["tasks"] = [t.to_dict() for t in tasks]
            self.save_status_data(data)
            return found


def _coerce_status(raw: str) -> str:
    """Map sprint-state vocabulary to task-graph vocabulary.

    The sprint YAML uses ``"planned"`` / ``"review"`` for
    intermediate stages; the task graph collapses all non-terminal
    intermediate states into ``"pending"`` (runnable) or
    ``"in-progress"`` (held by someone). ``"blocked"`` and
    ``"done"`` pass through 1:1.
    """
    if raw in {"in-progress", "done", "blocked", "pending"}:
        return raw
    if raw == "review":
        # Review waiting on a human is neither runnable nor done;
        # the closest fit in the task graph is ``in-progress``
        # (something is holding it).
        return "in-progress"
    # "planned" and anything unknown → pending.
    return "pending"
