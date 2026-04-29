"""Per-project persistent agent memory.

Why this module exists
----------------------
Every Feishu session starts from a cold LLM context. The tech lead has
no idea what was decided yesterday, which branches are in flight, or
which architectural choices have already been rejected. In practice
this meant the TL would re-ask the user the same questions across
sessions ("should we commit the venv?") and occasionally re-litigate
design decisions the user had already resolved.

Hermes Agent solves this with a per-agent ``MEMORY.md`` that the agent
is expected to curate itself via an ``append_memory`` tool. We adopt
the same pattern at the project level: each project gets an
``AGENT_NOTES.md`` in its repo root that the tech lead writes to via
``append_agent_note``. On the next session, those notes are injected
into the tech lead's system prompt.

Design decisions
----------------
- **Project-scoped, not global**: multiple projects use the same
  FeishuOPC instance; mixing their memory would be confusing and a
  privacy issue for cross-customer deployments.
- **TechLead-only write**: the tech lead is the single orchestrator;
  giving every role the ability to append creates a tragedy-of-the-
  commons scenario where the file fills up with trivia.
- **Append-only file, newest-first**: makes history auditable; simple
  to diff in PRs; operators can manually redact (delete a line) if
  they disagree with what the agent remembered.
- **Hard per-session caps**: the TL can write at most
  ``max_notes_per_session`` notes (default 5) per Feishu session, and
  each note is capped at ``MAX_NOTE_CHARS``. Without these caps the
  LLM tends to spam notes for every minor observation.
- **Secret-scanned on write**: same scanner we use for code writes —
  a hallucinated API key in a "remember this token" note is exactly
  the leak we designed the scanner to prevent.
- **Bounded on read**: prompt injection reads at most N recent notes
  (default 20). Older memory stays in the file for audit but doesn't
  consume context.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from feishu_agent.tools import secret_scanner

try:  # POSIX only; on Windows we fall back to in-process lock only.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover — not exercised on dev machines
    _fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


class AgentNoteError(Exception):
    """Base class for all note-write failures the LLM should see.

    Subclasses carry a stable ``code`` so the tool surface can return
    ``{"error": code, "detail": str}`` and the LLM can branch on it.
    """

    code: str = "AGENT_NOTE_ERROR"


class AgentNoteDisabledError(AgentNoteError):
    code = "AGENT_NOTE_DISABLED"


class AgentNoteOversizeError(AgentNoteError):
    code = "AGENT_NOTE_OVERSIZE"


class AgentNoteLimitError(AgentNoteError):
    code = "AGENT_NOTE_SESSION_LIMIT"


class AgentNoteEmptyError(AgentNoteError):
    code = "AGENT_NOTE_EMPTY"


class AgentNoteSecretError(AgentNoteError):
    code = "AGENT_NOTE_SECRET_DETECTED"


@dataclass(frozen=True)
class AgentNote:
    """One entry in ``AGENT_NOTES.md``."""

    timestamp_iso: str
    role: str
    project_id: str
    note: str
    trace_id: str | None = None

    def render_markdown(self) -> str:
        """Render for ``AGENT_NOTES.md``. Intentionally compact — these
        accumulate and we want the file to stay readable."""
        ts = self.timestamp_iso
        trace = f" `{self.trace_id}`" if self.trace_id else ""
        body = self.note.rstrip()
        return f"- **{ts}** @{self.role}{trace}: {body}"


class AgentNotesService:
    """Manages ``AGENT_NOTES.md`` inside a project repo.

    Invariants:

    - File path is always ``<project_root>/AGENT_NOTES.md``.
    - Writes are atomic (write to ``<path>.tmp`` + os.replace).
    - Reads skip malformed lines (never raise — a corrupted file
      degrades gracefully to "no memory").
    - Per-session counters reset when a new service instance is built
      (one instance per Feishu message; counters are in-memory).
    """

    MAX_NOTE_CHARS: int = 512
    RELATIVE_PATH: str = "AGENT_NOTES.md"
    HEADER_LINE: str = "# Agent Notes"
    HEADER_BLURB: str = (
        "Agent-curated memory. Newest first. Each entry is a bullet "
        "containing timestamp, role, trace id, and a short note. "
        "Safe to delete lines; the agent will treat the file as the "
        "full memory on the next session."
    )

    def __init__(
        self,
        *,
        project_id: str,
        project_root: Path,
        max_notes_per_session: int = 5,
        enabled: bool = True,
    ) -> None:
        if max_notes_per_session < 0:
            raise ValueError("max_notes_per_session must be >= 0")
        self._project_id = project_id
        self._project_root = Path(project_root)
        self._max_per_session = max_notes_per_session
        self._enabled = enabled
        self._session_counts: dict[str, int] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def notes_path(self) -> Path:
        return self._project_root / self.RELATIVE_PATH

    @property
    def project_id(self) -> str:
        return self._project_id

    def append(
        self,
        *,
        role: str,
        note: str,
        trace_id: str | None = None,
        now_fn: Any = None,
    ) -> AgentNote:
        """Append one note to the project's ``AGENT_NOTES.md``.

        Raises the specific ``AgentNoteError`` subclass on any check
        failure so the tool surface can distinguish reasons. Successful
        writes return the persisted ``AgentNote`` (with the final
        timestamp applied).
        """
        if not self._enabled:
            raise AgentNoteDisabledError(
                "agent notes are disabled for this project"
            )
        if not note or not note.strip():
            raise AgentNoteEmptyError("note content is empty")
        if len(note) > self.MAX_NOTE_CHARS:
            raise AgentNoteOversizeError(
                f"note exceeds {self.MAX_NOTE_CHARS} chars "
                f"(got {len(note)})"
            )

        # Session-level cap. Per-role to keep one noisy role from
        # exhausting another role's budget (future: non-TL roles may
        # gain write access — cap prevents a single bad session from
        # bloating the file).
        with self._lock:
            used = self._session_counts.get(role, 0)
            if used >= self._max_per_session:
                raise AgentNoteLimitError(
                    f"role {role!r} already used its session quota of "
                    f"{self._max_per_session}"
                )

            try:
                secret_scanner.ensure_clean(note, path=str(self.notes_path))
            except secret_scanner.SecretDetectedError as exc:
                raise AgentNoteSecretError(str(exc)) from exc

            ts = (
                now_fn().isoformat(timespec="seconds")
                if now_fn is not None
                else datetime.now(timezone.utc).isoformat(timespec="seconds")
            )
            entry = AgentNote(
                timestamp_iso=ts,
                role=role,
                project_id=self._project_id,
                note=note.strip(),
                trace_id=trace_id,
            )
            self._prepend(entry)
            self._session_counts[role] = used + 1
            return entry

    def read_recent(self, *, limit: int = 20) -> list[AgentNote]:
        """Read the most recent notes (up to ``limit``).

        Safe on missing file: returns an empty list. Safe on malformed
        lines: skips them. The point of memory is "best-effort
        recall" — a parsing bug should never crash a session.
        """
        if limit <= 0:
            return []
        path = self.notes_path
        if not path.exists():
            return []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            logger.warning(
                "failed to read agent notes at %s", path, exc_info=True
            )
            return []

        out: list[AgentNote] = []
        for line in text.splitlines():
            if not line.startswith("- **"):
                continue
            note = self._parse_line(line)
            if note is not None:
                out.append(note)
            if len(out) >= limit:
                break
        return out

    def select_for_prompt(
        self,
        *,
        query: str,
        limit: int = 20,
        role: str | None = None,
        task_mode: str | None = None,
    ) -> list[AgentNote]:
        """Return a relevance-ranked subset of recent notes.

        V1 keeps retrieval file-based and explainable: we score a bounded
        recent window by simple lexical overlap with the current query,
        then apply small bonuses for role and task-mode hints. This is
        intentionally modest; the goal is to beat naive recency without
        introducing opaque indexing infrastructure yet.
        """
        if limit <= 0:
            return []
        # Score a wider recent window, then keep the best N. The file is
        # newest-first, so the recency signal is already encoded by index.
        candidates = self.read_recent(limit=max(limit * 3, limit))
        if not candidates:
            return []
        query_tokens = _tokenize(query)
        mode_tokens = _tokenize(task_mode or "") if task_mode else set()
        scored: list[tuple[float, int, AgentNote]] = []
        for index, note in enumerate(candidates):
            score = _score_note(
                note,
                query_tokens=query_tokens,
                mode_tokens=mode_tokens,
                role=role,
                recency_index=index,
            )
            scored.append((score, index, note))
        # Sort: highest score first, break ties by keeping newer items
        # (lower recency index) above older ones.
        scored.sort(key=lambda item: (-item[0], item[1]))
        top = scored[:limit]
        # Restore newest-first rendering order using the recency index
        # we already have — avoids an O(n^2) ``list.index`` lookup.
        top.sort(key=lambda item: item[1])
        return [note for _score, _idx, note in top]

    def _prepend(self, entry: AgentNote) -> None:
        """Read-modify-write with a cross-process lock.

        Without the flock, two Feishu messages that land on the same
        project at the same time would each ``read → splice → replace``,
        and whichever ``os.replace`` lands last wins — losing the
        other's note. ``os.replace`` is still the final atomic swap, but
        the flock serializes the read-modify-write pair so nothing is
        lost.

        Locking is done against a side-car ``AGENT_NOTES.md.lock`` file
        (not the notes file itself) so the lock survives file
        replacement. On platforms without ``fcntl`` we fall back to the
        in-process ``threading.Lock`` only — acceptable since our
        production deployment is POSIX.
        """
        path = self.notes_path
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(path.suffix + ".lock")

        with self._locked_for_write(lock_path):
            existing = ""
            if path.exists():
                try:
                    existing = path.read_text(encoding="utf-8")
                except OSError:
                    logger.warning(
                        "failed to read agent notes at %s (overwriting)",
                        path,
                        exc_info=True,
                    )

            new_body = self._inject_entry(existing, entry)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(new_body, encoding="utf-8")
            # os.replace is atomic on POSIX + Windows — ensures a concurrent
            # reader either sees the old file or the new one, never a
            # half-written in-progress state.
            os.replace(tmp, path)

    @contextlib.contextmanager
    def _locked_for_write(self, lock_path: Path):
        """Acquire an exclusive file lock for the duration of the block.

        No-op on platforms without ``fcntl``. Lock file is created on
        demand and persisted (deletion would race with a concurrent
        opener). The lock is released automatically when the file
        descriptor is closed on exit.
        """
        if _fcntl is None:  # pragma: no cover — Windows path
            yield
            return
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            str(lock_path),
            os.O_RDWR | os.O_CREAT,
            0o644,
        )
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX)
            yield
        finally:
            try:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _inject_entry(self, existing_text: str, entry: AgentNote) -> str:
        """Return the new file body with ``entry`` as the newest item."""
        line = entry.render_markdown()
        if not existing_text.strip():
            return (
                f"{self.HEADER_LINE}\n\n"
                f"> {self.HEADER_BLURB}\n\n"
                f"{line}\n"
            )
        # Find where the existing entries start. The convention is that
        # entry lines begin with ``- **``; we splice new entry ahead of
        # the first one.
        lines = existing_text.splitlines()
        insert_at = None
        for i, ln in enumerate(lines):
            if ln.startswith("- **"):
                insert_at = i
                break
        if insert_at is None:
            # No existing entries — append after the header block, ensuring
            # a blank line between blurb and entries.
            tail = existing_text.rstrip() + "\n\n" + line + "\n"
            return tail
        new_lines = lines[:insert_at] + [line] + lines[insert_at:]
        return "\n".join(new_lines) + "\n"

    @staticmethod
    def _parse_line(line: str) -> AgentNote | None:
        """Invert ``AgentNote.render_markdown``.

        Format: ``- **<iso>** @<role> [`<trace>`]: <note>``.
        Returns ``None`` on any mismatch — we never raise from the
        reader path.
        """
        if not line.startswith("- **"):
            return None
        try:
            after_ts_marker = line[len("- **") :]
            ts_end = after_ts_marker.index("** @")
            ts = after_ts_marker[:ts_end]
            rest = after_ts_marker[ts_end + len("** @") :]
            # role ends at either space-backtick (trace present) or colon
            trace_id: str | None = None
            if " `" in rest and rest.index(" `") < rest.index(":"):
                role_end = rest.index(" `")
                role = rest[:role_end]
                after_role = rest[role_end + 2 :]
                trace_end = after_role.index("`")
                trace_id = after_role[:trace_end]
                rest2 = after_role[trace_end + 1 :]
                if not rest2.startswith(": "):
                    return None
                note = rest2[2:]
            else:
                role_end = rest.index(":")
                role = rest[:role_end]
                if not rest[role_end:].startswith(": "):
                    return None
                note = rest[role_end + 2 :]
        except (ValueError, IndexError):
            return None
        return AgentNote(
            timestamp_iso=ts,
            role=role,
            project_id="",  # project id is implicit from the file location
            note=note,
            trace_id=trace_id,
        )


def render_notes_for_prompt(
    notes: list[AgentNote], *, header: str = "## Project memory"
) -> str:
    """Render a short block for injection into the system prompt.

    Kept compact — prompts are precious; memory should be "reminded, not
    rehashed." Caller decides whether to inject; empty notes list
    produces empty string so the caller can unconditionally concatenate.
    """
    if not notes:
        return ""
    lines = [
        header,
        "",
        (
            "You have written the following notes in previous sessions "
            "for this project. Treat them as durable context but "
            "verify before acting on them; a note may be stale."
        ),
        "",
    ]
    for n in notes:
        trace = f" ({n.trace_id})" if n.trace_id else ""
        lines.append(f"- {n.timestamp_iso} @{n.role}{trace}: {n.note}")
    lines.append("")
    return "\n".join(lines)


def _tokenize(text: str) -> set[str]:
    """Tokenize a string into a set of lower-cased overlap tokens.

    Known tradeoff (V1, by design): CJK characters are tokenized one
    character at a time. This is a deliberately cheap, dependency-free
    choice — "飞书" becomes ``{"飞", "书"}``, which can over-match notes
    that share any single CJK character with the query. We accept that
    because:

    * the scoring is additive and still dominated by multi-character
      query/note overlaps in practice;
    * introducing a real segmenter (jieba etc.) would pull a heavyweight
      dependency into a hot path that runs on every prompt assembly;
    * the callers apply a fixed ``limit`` after scoring, so the blast
      radius of false-positive matches is bounded.

    Revisit this if retrieval quality becomes the dominant loss.
    """

    return {m.group(0).lower() for m in _TOKEN_RE.finditer(text or "")}


def _score_note(
    note: AgentNote,
    *,
    query_tokens: set[str],
    mode_tokens: set[str],
    role: str | None,
    recency_index: int,
) -> float:
    note_tokens = _tokenize(note.note)
    score = 0.0
    if query_tokens and note_tokens:
        overlap = query_tokens.intersection(note_tokens)
        score += float(len(overlap)) * 3.0
    if mode_tokens and note_tokens:
        score += float(len(mode_tokens.intersection(note_tokens))) * 1.5
    if role and role == note.role:
        score += 1.0
    # Slightly prefer newer items when semantic scores tie.
    score += max(0.0, 1.0 - (recency_index * 0.05))
    return score
