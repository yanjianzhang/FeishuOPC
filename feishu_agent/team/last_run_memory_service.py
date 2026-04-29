"""Per-project "last run" memory for the tech-lead.

Why this module exists
----------------------
Every Feishu message spins up a brand new ``LlmAgentAdapter`` session
with a cold context. If the previous run crashed / was cancelled /
errored out halfway through a workflow (e.g. ``developer`` committed
to ``feature/3-1-seed-grant`` but ``reviewer`` found 3 lint errors and
the user came back 30 minutes later), the tech-lead has no memory of
that state and will ask the user to restate the entire goal. The user
rightly complains: "不要上一次错了之后每次都从头开始".

``AgentNotesService`` already exists for *durable project decisions*
("我们约定 feature 分支用 feature/<story>-<slug> 命名"), but its 5-note
per-session cap and secret scanner explicitly reject transient run
state — and they should, because mixing "long-term rule" and "where I
was 30 minutes ago" in the same prompt-injection block is confusing
and the rules can actually become stale if overwritten by run data.

This service is the sibling for transient run state:

- **Scope**: per project, keyed by ``project_root`` (same pattern as
  ``AgentNotesService``).
- **Trigger**: every Feishu session end automatically writes one
  ``RunDigest`` via a ``HookBus`` subscriber — the LLM doesn't need
  to call any tool.
- **Injection policy**: the *most recent* digest is injected into the
  tech-lead system prompt **only when it did NOT succeed**. Once a
  successful run lands on top, the memory is effectively cleared for
  the prompt (the record stays in the file for audit). This matches
  the user's choice in the design conversation: "只注入'最近一条非成功
  run'直到下一次成功清空".
- **Privacy**: ``tool_calls`` carry only name + ok + short summary;
  full tool inputs/outputs never leave the adapter.

Design decisions
----------------
- **File format**: JSONL (``.feishu_run_history.jsonl``). Append-only,
  one digest per line, trimmed from the front when it exceeds
  ``MAX_HISTORY_RECORDS``. JSON keeps the schema machine-readable for
  future re-injection or dashboards; ``.jsonl`` keeps the file
  greppable by humans with ``jq``.
- **Capped tool_calls per digest**: we store at most 10 summaries; a
  20-turn reviewer/bug-fixer loop would blow the prompt budget
  otherwise. The overflow count is retained separately so the prompt
  can say "(+7 more)".
- **No LLM-facing write tool**: capture is 100 % automatic through the
  hook bus. This was an explicit design decision — see the user answer
  in the design conversation: "不用 —— 只靠 HookBus 自动抓取"。
- **Best-effort git probe**: at session end we try to read the current
  branch + short HEAD in ``project_root``. Failure (not a git repo,
  detached HEAD, git binary missing) is silent; the digest just omits
  ``git_state``. This isn't authoritative (could drift before the next
  message) but it's enough to prompt "上一次在 feature/3-1-seed-grant@abc12345
  上动过".
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from feishu_agent.core.hook_bus import HookBus

try:  # POSIX only — same pattern as AgentNotesService's flock usage.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover
    _fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# Stop reasons that the adapter emits when the session genuinely
# finished. Everything else (timeout / error / cancelled / max_turns)
# counts as a non-success worth reminding the next session about.
_SUCCESS_STOP_REASONS: frozenset[str] = frozenset(
    {"end_turn", "complete", "stop"}
)


@dataclass
class ToolCallSummary:
    """One compact line per tool call. Deliberately lossy — we keep
    just enough to explain "where the last run got to"."""

    name: str
    ok: bool
    duration_ms: int = 0
    summary: str = ""  # ≤ 120 chars, trimmed on ingest


@dataclass
class RunDigest:
    """One per Feishu session. Persisted as a single JSONL line.

    ``ok`` is the canonical "was this a success" flag rather than
    recomputing from ``stop_reason`` in every consumer — the
    collector already did the join once.
    """

    trace_id: str
    started_at: str                  # ISO-8601 UTC
    ended_at: str | None = None
    role: str = "tech_lead"
    user_command: str = ""           # truncated to USER_COMMAND_MAX
    stop_reason: str | None = None
    ok: bool = False
    error_detail: str | None = None
    tool_calls: list[ToolCallSummary] = field(default_factory=list)
    tool_calls_overflow: int = 0     # # suppressed beyond MAX_TOOL_CALLS
    git_state: dict[str, str] | None = None

    def to_json(self) -> str:
        payload = asdict(self)
        return json.dumps(payload, ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> "RunDigest | None":
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            return None
        try:
            tc_raw = raw.get("tool_calls") or []
            tool_calls = [
                ToolCallSummary(
                    name=str(t.get("name", "")),
                    ok=bool(t.get("ok", False)),
                    duration_ms=int(t.get("duration_ms", 0) or 0),
                    summary=str(t.get("summary", ""))[: LastRunMemoryService.TOOL_SUMMARY_MAX],
                )
                for t in tc_raw
                if isinstance(t, dict)
            ]
            return cls(
                trace_id=str(raw.get("trace_id", "")),
                started_at=str(raw.get("started_at", "")),
                ended_at=raw.get("ended_at"),
                role=str(raw.get("role", "tech_lead")),
                user_command=str(raw.get("user_command", "")),
                stop_reason=raw.get("stop_reason"),
                ok=bool(raw.get("ok", False)),
                error_detail=raw.get("error_detail"),
                tool_calls=tool_calls,
                tool_calls_overflow=int(raw.get("tool_calls_overflow", 0) or 0),
                git_state=raw.get("git_state"),
            )
        except (TypeError, ValueError):
            return None


class LastRunMemoryService:
    """Project-scoped JSONL store for per-session run digests."""

    RELATIVE_PATH: str = ".feishu_run_history.jsonl"
    MAX_TOOL_CALLS_PER_DIGEST: int = 10
    MAX_HISTORY_RECORDS: int = 100
    USER_COMMAND_MAX: int = 500
    TOOL_SUMMARY_MAX: int = 120

    def __init__(
        self,
        *,
        project_id: str,
        project_root: Path,
        enabled: bool = True,
    ) -> None:
        self._project_id = project_id
        self._project_root = Path(project_root)
        self._enabled = enabled
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def project_id(self) -> str:
        return self._project_id

    @property
    def project_root(self) -> Path:
        return self._project_root

    @property
    def history_path(self) -> Path:
        return self._project_root / self.RELATIVE_PATH

    def append(self, digest: RunDigest) -> None:
        """Append one digest; trim from the front if over the cap.

        The trim uses a read-modify-write pair inside the flock so
        concurrent writers can't each append-then-rewrite and lose
        each other's entries.
        """
        if not self._enabled:
            return
        path = self.history_path
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(path.suffix + ".lock")
        # Best-effort: keep the history + lock files out of `git status`.
        # We write to `.git/info/exclude` (per-clone ignore list) rather
        # than the project's `.gitignore`, because these files are a
        # runtime artifact of THIS machine's agent — they shouldn't show
        # up as a "modified .gitignore" in the user's PR queue. Failures
        # are swallowed: if the repo root isn't a git checkout, or the
        # info dir is read-only, the worst case is the dev sees the file
        # flagged by run_pre_push_inspection and deals with it manually.
        self._ensure_git_excluded(path, lock_path)

        serialized = digest.to_json()
        if "\n" in serialized:  # defensive; shouldn't happen with ensure_ascii+None-trimmed
            serialized = serialized.replace("\n", " ")

        with self._locked_for_write(lock_path), self._lock:
            lines: list[str] = []
            if path.exists():
                try:
                    text = path.read_text(encoding="utf-8")
                    lines = [ln for ln in text.splitlines() if ln.strip()]
                except OSError:
                    logger.warning(
                        "failed reading %s; overwriting", path, exc_info=True
                    )
                    lines = []
            lines.append(serialized)
            # Drop oldest entries (file is chronological, newest last)
            # so we never exceed MAX_HISTORY_RECORDS on disk.
            if len(lines) > self.MAX_HISTORY_RECORDS:
                lines = lines[-self.MAX_HISTORY_RECORDS :]
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.replace(tmp, path)

    def load_last(self) -> RunDigest | None:
        """Return the most recent digest, or ``None`` on empty/missing file."""
        if not self._enabled:
            return None
        path = self.history_path
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("failed reading %s", path, exc_info=True)
            return None
        for line in reversed(text.splitlines()):
            if not line.strip():
                continue
            digest = RunDigest.from_json(line)
            if digest is not None:
                return digest
        return None

    def load_inject_target(self) -> RunDigest | None:
        """Return the most recent digest **iff it was not a success**.

        Implements the "clear on next success" policy: once a successful
        run is the newest record, the prompt stops surfacing history.
        The caller should always check this; inject unconditionally
        would confuse the LLM after a clean success.
        """
        last = self.load_last()
        if last is None or last.ok:
            return None
        return last

    def _locked_for_write(self, lock_path: Path):
        """File lock for cross-process safety. No-op where fcntl is missing."""
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            if _fcntl is None:  # pragma: no cover — Windows
                yield
                return
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
            try:
                _fcntl.flock(fd, _fcntl.LOCK_EX)
                yield
            finally:
                try:
                    _fcntl.flock(fd, _fcntl.LOCK_UN)
                finally:
                    os.close(fd)

        return _cm()

    def _ensure_git_excluded(self, *targets: Path) -> None:
        """Make sure ``.git/info/exclude`` ignores our runtime files.

        We use ``.git/info/exclude`` (local-only) rather than the tracked
        ``.gitignore`` because these files are a per-machine runtime
        artifact of the agent — adding them to the shared ``.gitignore``
        would create a phantom "M .gitignore" on every agent deployment
        and make the operator review a commit they don't care about.

        Idempotent: we touch the file only when an entry is missing. We
        only act if a real ``.git`` directory exists at the project root.
        A git *file* (submodule) or any other weird layout silently
        aborts. All errors are swallowed — this method never raises.
        """
        try:
            project_root = self._project_root
            git_dir = project_root / ".git"
            # Must be a real .git directory (not a submodule pointer file).
            if not git_dir.is_dir():
                return
            info_dir = git_dir / "info"
            exclude_path = info_dir / "exclude"

            needed: list[str] = []
            for t in targets:
                try:
                    rel = t.relative_to(project_root)
                except ValueError:
                    continue
                entry = "/" + rel.as_posix()
                if entry not in needed:
                    needed.append(entry)
            if not needed:
                return

            existing_lines: list[str] = []
            if exclude_path.exists():
                try:
                    existing_lines = exclude_path.read_text(
                        encoding="utf-8"
                    ).splitlines()
                except (OSError, UnicodeDecodeError):
                    return
            existing_set = {ln.strip() for ln in existing_lines if ln.strip()}
            missing = [e for e in needed if e not in existing_set]
            if not missing:
                return
            header = "# feishu-agent: run history (auto-managed, local-only)"
            new_block: list[str] = []
            if existing_lines and existing_lines[-1].strip():
                new_block.append("")
            if header not in existing_set:
                new_block.append(header)
            new_block.extend(missing)
            payload = "\n".join(existing_lines + new_block) + "\n"
            try:
                info_dir.mkdir(parents=True, exist_ok=True)
                exclude_path.write_text(payload, encoding="utf-8")
            except OSError:
                return
        except Exception:  # noqa: BLE001 — self-healing, must not crash append()
            logger.debug(".git/info/exclude self-heal failed", exc_info=True)


class RunDigestCollector:
    """HookBus subscriber that builds a :class:`RunDigest` and persists
    it on ``on_session_end``.

    Usage::

        collector = RunDigestCollector(
            service=last_run_svc,
            trace_id=trace,
            user_command=command_text,
        )
        collector.attach(bus)
        # ... run the session ...
        # on_session_end triggers collector._on_end -> service.append()

        # If the adapter itself raises (before on_session_end fires),
        # the runtime should call collector.flush_on_exception(exc).
    """

    def __init__(
        self,
        *,
        service: LastRunMemoryService,
        trace_id: str,
        user_command: str,
        role: str = "tech_lead",
    ) -> None:
        self._svc = service
        cmd = (user_command or "").strip()
        if len(cmd) > LastRunMemoryService.USER_COMMAND_MAX:
            cmd = cmd[: LastRunMemoryService.USER_COMMAND_MAX] + " …"
        self._digest = RunDigest(
            trace_id=trace_id,
            started_at=_now_iso(),
            role=role,
            user_command=cmd,
        )
        self._persisted = False
        self._last_failed_tool_summary: str | None = None

    @property
    def digest(self) -> RunDigest:
        return self._digest

    def attach(self, bus: HookBus) -> None:
        bus.subscribe("on_tool_call", self._on_tool_call)
        bus.subscribe("on_session_end", self._on_session_end)

    async def _on_tool_call(self, event: str, payload: dict[str, Any]) -> None:
        name = str(payload.get("tool_name") or "?")
        result = payload.get("result")
        duration_ms = int(payload.get("duration_ms") or 0)
        ok, summary = _summarize_tool_result(name, result)
        summary = summary[: LastRunMemoryService.TOOL_SUMMARY_MAX]

        if len(self._digest.tool_calls) >= LastRunMemoryService.MAX_TOOL_CALLS_PER_DIGEST:
            self._digest.tool_calls_overflow += 1
            return
        self._digest.tool_calls.append(
            ToolCallSummary(
                name=name,
                ok=ok,
                duration_ms=duration_ms,
                summary=summary,
            )
        )
        if not ok:
            # Remember the latest failure so we can default ``error_detail``
            # from it if the session ends without a clean stop_reason.
            self._last_failed_tool_summary = f"{name}: {summary}"

    async def _on_session_end(self, event: str, payload: dict[str, Any]) -> None:
        if self._persisted:
            return
        stop_reason = payload.get("stop_reason")
        ok = bool(payload.get("ok", False)) and (
            stop_reason is None or stop_reason in _SUCCESS_STOP_REASONS
        )
        self._digest.ended_at = _now_iso()
        self._digest.stop_reason = stop_reason
        self._digest.ok = ok
        if not ok and not self._digest.error_detail:
            self._digest.error_detail = (
                self._last_failed_tool_summary
                or f"stop_reason={stop_reason}"
            )
        self._digest.git_state = _probe_git_state(self._svc.project_root)
        try:
            self._svc.append(self._digest)
        except Exception:
            logger.warning(
                "persisting run digest failed trace=%s",
                self._digest.trace_id,
                exc_info=True,
            )
        finally:
            self._persisted = True

    def flush_on_exception(self, exc: BaseException) -> None:
        """Called by the runtime when the adapter raises before ever
        emitting ``on_session_end``. Writes a failure digest so the
        next session still sees *something*.
        """
        if self._persisted:
            return
        self._digest.ended_at = _now_iso()
        self._digest.stop_reason = "exception"
        self._digest.ok = False
        # Prefer the tool-call failure over the exception message — the
        # tool context is usually more actionable than "httpx timeout".
        self._digest.error_detail = (
            self._last_failed_tool_summary
            or f"{type(exc).__name__}: {exc}"[: LastRunMemoryService.TOOL_SUMMARY_MAX]
        )
        self._digest.git_state = _probe_git_state(self._svc.project_root)
        try:
            self._svc.append(self._digest)
        except Exception:
            logger.warning(
                "persisting exception digest failed trace=%s",
                self._digest.trace_id,
                exc_info=True,
            )
        finally:
            self._persisted = True


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def render_last_run_for_prompt(digest: RunDigest) -> str:
    """Return a compact ``## Last run context`` block to prepend to the
    tech-lead system prompt. Returns empty string for a ``None`` input
    so callers can concatenate unconditionally.

    The block is deliberately short (~250 tokens) and ends with an
    instruction to the LLM on how to use it — otherwise models tend to
    treat it as authoritative "current state" and re-do finished work.
    """
    if digest is None:
        return ""

    lines: list[str] = [
        "## Last run context",
        "",
        (
            "上一轮飞书会话没有成功收尾。请不要把本次请求当作空白起点："
            "若用户的本次消息是接着上轮做（追问/继续），先说明你打算从哪一步"
            "接上；若是全新任务，忽略本块并按常规流程。**不要重新执行上轮已"
            "完成的步骤**。"
        ),
        "",
        f"- trace: `{digest.trace_id}`",
        f"- 开始: {digest.started_at}",
        f"- 结束: {digest.ended_at or '(unknown)'}",
        f"- 停止原因: `{digest.stop_reason or 'unknown'}`",
    ]
    if digest.user_command:
        lines.append(f"- 上轮用户指令: {digest.user_command}")
    if digest.error_detail:
        lines.append(f"- 最后失败点: {digest.error_detail}")
    if digest.git_state:
        branch = digest.git_state.get("branch", "?")
        head = digest.git_state.get("head", "?")
        lines.append(f"- 仓库状态: `{branch}` @ `{head}`")

    if digest.tool_calls:
        lines.append("- 本轮已执行的工具:")
        for tc in digest.tool_calls:
            marker = "✅" if tc.ok else "❌"
            extra = f" ({tc.summary})" if tc.summary else ""
            lines.append(f"  - {marker} `{tc.name}`{extra}")
        if digest.tool_calls_overflow:
            lines.append(f"  - (+{digest.tool_calls_overflow} more tool calls)")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _summarize_tool_result(name: str, result: Any) -> tuple[bool, str]:
    """Boil a tool result down to (ok, one-line-summary).

    We accept three shapes we actually emit from the executors:
    - ``{"ok": bool, "note": str, ...}`` — our native shape.
    - ``{"error": "CODE", "detail": "..."}`` — failure shape.
    - str — rare, e.g. pre-formatted markdown. Assume ok=True.

    Anything else falls back to ``(True, "")``; we never raise — this
    runs inside a best-effort hook.
    """
    if isinstance(result, str):
        snippet = result.strip().splitlines()[0] if result.strip() else ""
        return True, snippet[:200]
    if not isinstance(result, dict):
        return True, ""
    if "error" in result:
        code = str(result.get("error") or "")
        detail = str(result.get("detail") or result.get("verification_error") or "")
        return False, f"{code} {detail}".strip()
    ok_field = result.get("ok")
    ok = True if ok_field is None else bool(ok_field)
    note = str(
        result.get("note")
        or result.get("summary")
        or result.get("message")
        or ""
    )
    return ok, note


def _probe_git_state(project_root: Path, timeout: float = 2.0) -> dict[str, str] | None:
    """Best-effort current branch + short HEAD. Returns ``None`` on any
    failure (not a repo, git missing, detached HEAD, timeout).
    """
    try:
        root = str(project_root)
        branch_out = subprocess.run(
            ["git", "-C", root, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if branch_out.returncode != 0:
            return None
        branch = branch_out.stdout.strip() or "?"
        head_out = subprocess.run(
            ["git", "-C", root, "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if head_out.returncode != 0:
            return {"branch": branch}
        head = head_out.stdout.strip() or "?"
        return {"branch": branch, "head": head}
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


__all__ = [
    "LastRunMemoryService",
    "RunDigest",
    "RunDigestCollector",
    "ToolCallSummary",
    "render_last_run_for_prompt",
]
