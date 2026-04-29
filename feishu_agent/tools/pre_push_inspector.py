"""Pre-push inspection for the tech-lead agent.

Tech-lead is the only role that can write code to a project repo. It is
also the gate-keeper: before committing / pushing, it must run an
inspection pass that catches the things a coding LLM is bad at
self-checking:

1. **Diff summary**  — what files changed, how many lines added/removed.
   Lets tech-lead (and the human) eyeball whether the scope looks right.
2. **Secret scan on diff**  — every *added* line in ``git diff`` plus
   every line of every *untracked* file under the project repo, fed
   through ``secret_scanner``. Catches secrets that didn't go through
   ``write_source`` (e.g. files dropped on disk by a different tool or
   a prior agent run).
3. **Path policy check**  — every changed / untracked file path must
   sit inside ``policy.allowed_write_roots``. A file that slipped
   outside the policy is always an issue, even if the content looks
   fine.
4. **Oversize check**  — per-file insertions+deletions above
   ``policy.require_confirmation_above_bytes`` get flagged as a
   "large change" the tech-lead should call out in the Feishu thread.
5. **Untracked files listing**  — name every untracked file. Untracked
   files are the top vector for "we accidentally committed something".

All git calls are read-only (``status``/``diff``/``ls-files``/``rev-parse``).
No mutations.

On a clean report (``blockers == []``) the inspector mints an
``inspection_token`` that ``GitOpsService.push`` will accept. The
token is tied to the exact HEAD SHA at inspection time so that if the
agent writes more code after passing inspection, the push will refuse
until a fresh inspection is run.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from feishu_agent.tools import secret_scanner
from feishu_agent.tools.code_write_service import CodeWritePolicy, ValidationCommand

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InspectionError(Exception):
    """Base class for inspection failures."""

    code: str = "INSPECTION_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InspectionProjectError(InspectionError):
    code = "INSPECTION_UNKNOWN_PROJECT"


class InspectionGitError(InspectionError):
    code = "INSPECTION_GIT_UNAVAILABLE"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileDiffStat:
    path: str
    additions: int
    deletions: int
    status: str  # "M", "A", "D", "R", "??", etc.


@dataclass(frozen=True)
class InspectionIssue:
    kind: str
    # one of:
    #   "secret_in_diff" / "secret_in_untracked"
    #   "path_outside_policy"
    #   "oversize_change"
    severity: str  # "blocker" | "warning"
    path: str
    detail: str


@dataclass
class InspectionReport:
    project_id: str
    branch: str
    head_sha: str
    is_protected_branch: bool
    files_changed: list[FileDiffStat]
    untracked_files: list[str]
    issues: list[InspectionIssue]
    inspection_token: str | None  # non-None iff ok
    inspected_at: float  # unix time

    @property
    def ok(self) -> bool:
        return not any(i.severity == "blocker" for i in self.issues)

    @property
    def blockers(self) -> list[InspectionIssue]:
        return [i for i in self.issues if i.severity == "blocker"]

    @property
    def warnings(self) -> list[InspectionIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "branch": self.branch,
            "head_sha": self.head_sha,
            "is_protected_branch": self.is_protected_branch,
            "ok": self.ok,
            "files_changed": [
                {
                    "path": f.path,
                    "status": f.status,
                    "additions": f.additions,
                    "deletions": f.deletions,
                }
                for f in self.files_changed
            ],
            "untracked_files": list(self.untracked_files),
            "blockers": [
                {"kind": i.kind, "path": i.path, "detail": i.detail}
                for i in self.blockers
            ],
            "warnings": [
                {"kind": i.kind, "path": i.path, "detail": i.detail}
                for i in self.warnings
            ],
            "inspection_token": self.inspection_token,
            "inspected_at": self.inspected_at,
        }


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@dataclass
class _TokenEntry:
    token: str
    head_sha: str
    branch: str
    issued_at: float


class PrePushInspector:
    """Run readonly git queries + secret_scanner + path/size checks, and
    mint short-lived ``inspection_token``s that ``GitOpsService`` can
    consume.

    Token persistence
    -----------------
    Tokens are persisted to ``token_store_path`` (if provided) so that
    an "inspect in one Feishu message, push in another" flow works
    across adapter lifetimes. Without disk persistence the inspector
    instance — recreated per Feishu message — would lose every token
    as soon as its LLM session ended, forcing the tech lead to always
    re-inspect right before push (which is safe, but clunky).

    The file is read lazily and pruned on every mint. File format is
    ``.jsonl``; each line is one ``_TokenEntry`` dict. Writes are
    best-effort — a disk failure falls back to in-memory tokens so
    same-message inspect+push still works.
    """

    TOKEN_TTL_SECONDS: int = 10 * 60  # 10 min
    UNTRACKED_MAX_BYTES: int = 1024 * 1024  # 1MB; larger files are skipped with a warning

    def __init__(
        self,
        *,
        project_roots: dict[str, Path],
        policies: dict[str, CodeWritePolicy],
        git_binary: str = "git",
        now: Callable[[], float] = time.time,
        token_store_path: Path | None = None,
    ) -> None:
        self._project_roots = {k: Path(v) for k, v in project_roots.items()}
        self._policies = dict(policies)
        self._git = git_binary
        self._now = now
        self._tokens: dict[str, _TokenEntry] = {}
        self._token_store_path = Path(token_store_path) if token_store_path else None
        if self._token_store_path is not None:
            # Fail LOUDLY if the configured path is unwritable. Silent
            # fallback to in-memory-only tokens used to mask real deploy
            # errors (wrong uid under systemd, read-only mount, etc.)
            # where tokens appeared to work within one Feishu message
            # but were silently lost across messages. We still degrade
            # gracefully — store_path is set to None so we continue
            # with in-memory tokens — but the warning makes the
            # operator aware.
            self._probe_token_store_writable()
            if self._token_store_path is not None:
                self._load_tokens_from_disk()

    # -- public API -----------------------------------------------------

    def inspect(self, project_id: str) -> InspectionReport:
        root = self._project_roots.get(project_id)
        pol = self._policies.get(project_id)
        if root is None or pol is None:
            raise InspectionProjectError(
                f"No project configured for project_id={project_id!r}."
            )
        if not (root / ".git").exists():
            raise InspectionGitError(
                f"{root} is not a git checkout (missing .git directory)."
            )

        branch = self._git_out(root, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
        head_sha = self._git_out(root, ["rev-parse", "HEAD"]).strip()

        files_changed = self._collect_changed_files(root)
        untracked = self._collect_untracked_files(root)

        issues: list[InspectionIssue] = []

        # -- secret scan on diff (added lines) --------------------------
        added_text_by_file = self._collect_added_text_per_file(root)
        for rel_path, added_blob in added_text_by_file.items():
            for f in secret_scanner.scan(added_blob):
                issues.append(
                    InspectionIssue(
                        kind="secret_in_diff",
                        severity="blocker",
                        path=rel_path,
                        detail=(
                            f"{f.description} ({f.rule_id}) "
                            f"preview={f.matched_preview}"
                        ),
                    )
                )

        # -- secret scan on untracked files -----------------------------
        for rel_path in untracked:
            abs_path = (root / rel_path).resolve()
            try:
                abs_path.relative_to(root)
            except ValueError:
                continue
            if not abs_path.is_file():
                continue
            try:
                size = abs_path.stat().st_size
            except OSError:
                continue
            if size > self.UNTRACKED_MAX_BYTES:
                issues.append(
                    InspectionIssue(
                        kind="oversize_change",
                        severity="warning",
                        path=rel_path,
                        detail=(
                            f"untracked file is {size} bytes, skipping secret scan; "
                            "review manually."
                        ),
                    )
                )
                continue
            try:
                content = abs_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for f in secret_scanner.scan(content):
                issues.append(
                    InspectionIssue(
                        kind="secret_in_untracked",
                        severity="blocker",
                        path=rel_path,
                        detail=(
                            f"{f.description} ({f.rule_id}) "
                            f"line={f.line_number} preview={f.matched_preview}"
                        ),
                    )
                )

        # -- path-policy check (tracked changes + untracked) ------------
        paths_to_check = [f.path for f in files_changed] + list(untracked)
        for rel_path in paths_to_check:
            if not pol.is_commit_allowed_path(rel_path):
                issues.append(
                    InspectionIssue(
                        kind="path_outside_policy",
                        severity="blocker",
                        path=rel_path,
                        detail=(
                            "path is outside policy.allowed_write_roots="
                            f"{list(pol.allowed_write_roots)} and "
                            "allowed_artifact_roots="
                            f"{list(pol.allowed_artifact_roots)}."
                        ),
                    )
                )
            denied = pol.has_denied_segment(rel_path)
            if denied:
                issues.append(
                    InspectionIssue(
                        kind="path_outside_policy",
                        severity="blocker",
                        path=rel_path,
                        detail=(
                            f"path hits denied segment {denied!r} "
                            "(never commit .env / secrets / keys)."
                        ),
                    )
                )

        # -- oversize check --------------------------------------------
        ceiling = pol.require_confirmation_above_bytes
        for fstat in files_changed:
            # Use added+deleted lines * 80 chars as a coarse byte proxy;
            # this is deliberately loose so the warning fires only for
            # genuinely large edits. We prefer "explicit threshold" over
            # trying to exactly reproduce on-disk byte deltas (for rename
            # / binary / submodule edge cases).
            approx = (fstat.additions + fstat.deletions) * 80
            if approx > ceiling:
                issues.append(
                    InspectionIssue(
                        kind="oversize_change",
                        severity="warning",
                        path=fstat.path,
                        detail=(
                            f"approx {approx} bytes changed "
                            f"(+{fstat.additions}/-{fstat.deletions} lines) "
                            f"> require_confirmation_above_bytes={ceiling}."
                        ),
                    )
                )

        # -- project validation commands --------------------------------
        # Runs project-configured gates (typecheck / lint / fast tests)
        # locally BEFORE we mint a token. A non-zero exit (or a timeout)
        # turns into a blocker, identical in shape to any other blocker,
        # so existing TL handling (read blockers -> escalate / dispatch
        # bug_fixer) works without special-casing. Configured per-project
        # via ``policy.validation_commands`` in ``policies.jsonl``; when
        # empty this loop is a no-op and behavior matches the legacy
        # "secret/path/size only" pre-push inspector.
        for vcmd in pol.validation_commands:
            issues.extend(self._run_validation_command(root, vcmd))

        token: str | None = None
        report = InspectionReport(
            project_id=project_id,
            branch=branch,
            head_sha=head_sha,
            is_protected_branch=pol.is_protected_branch(branch),
            files_changed=files_changed,
            untracked_files=list(untracked),
            issues=issues,
            inspection_token=None,
            inspected_at=self._now(),
        )
        if report.ok:
            token = self._mint_token(head_sha=head_sha, branch=branch)
            report.inspection_token = token
        return report

    def consume_token(
        self,
        token: str,
        *,
        expected_head_sha: str,
        expected_branch: str,
    ) -> bool:
        """One-shot consume. Returns True iff the token is valid, matches
        the provided HEAD sha + branch, and is within TTL. Regardless of
        outcome, the token is removed from the inspector (tokens are not
        replayable)."""
        entry = self._tokens.pop(token, None)
        if entry is None:
            return False
        # Persist token removal so another process / message can't
        # also consume it (tokens are single-use by design).
        self._flush_tokens_to_disk()
        if self._now() - entry.issued_at > self.TOKEN_TTL_SECONDS:
            return False
        if entry.head_sha != expected_head_sha:
            return False
        if entry.branch != expected_branch:
            return False
        return True

    # -- internal -------------------------------------------------------

    def _mint_token(self, *, head_sha: str, branch: str) -> str:
        token = secrets.token_urlsafe(24)
        self._tokens[token] = _TokenEntry(
            token=token,
            head_sha=head_sha,
            branch=branch,
            issued_at=self._now(),
        )
        # Cheap GC: drop expired entries whenever we mint.
        self._gc_tokens()
        self._flush_tokens_to_disk()
        return token

    def _gc_tokens(self) -> None:
        cutoff = self._now() - self.TOKEN_TTL_SECONDS
        stale = [k for k, v in self._tokens.items() if v.issued_at < cutoff]
        for k in stale:
            self._tokens.pop(k, None)

    def _probe_token_store_writable(self) -> None:
        """One-shot check that ``token_store_path`` is writable. On
        failure: log a warning, zero out ``_token_store_path`` so the
        rest of the inspector treats tokens as in-memory-only, and move
        on — this never breaks inspection itself."""
        path = self._token_store_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            probe = path.parent / ".writable-probe"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            logger.warning(
                "inspection token store at %s is not writable (%s); "
                "falling back to in-memory-only tokens — cross-message "
                "inspect+push will not work until this is fixed.",
                path,
                exc,
            )
            self._token_store_path = None

    def _load_tokens_from_disk(self) -> None:
        """Best-effort read of a previously persisted token store.

        We tolerate a missing file (fresh deploy), a truncated final
        line (crash mid-write), and entries with fields we no longer
        understand (schema evolution). On any serious error we warn and
        continue with an empty in-memory set — same-message inspect +
        push still works, cross-message does not.
        """
        path = self._token_store_path
        if path is None or not path.exists():
            return
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("failed to read inspection token store at %s", path, exc_info=True)
            return
        cutoff = self._now() - self.TOKEN_TTL_SECONDS
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                entry = _TokenEntry(
                    token=str(data["token"]),
                    head_sha=str(data["head_sha"]),
                    branch=str(data["branch"]),
                    issued_at=float(data["issued_at"]),
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            if entry.issued_at < cutoff:
                continue
            self._tokens[entry.token] = entry

    def _flush_tokens_to_disk(self) -> None:
        """Overwrite the token store atomically. Best-effort — a disk
        failure only impacts cross-message token reuse; it never breaks
        the LLM session in progress."""
        path = self._token_store_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            lines = []
            for entry in self._tokens.values():
                lines.append(
                    json.dumps(
                        {
                            "token": entry.token,
                            "head_sha": entry.head_sha,
                            "branch": entry.branch,
                            "issued_at": entry.issued_at,
                        },
                        ensure_ascii=False,
                    )
                )
            tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            logger.warning(
                "failed to persist inspection token store at %s", path, exc_info=True
            )

    # -- validation-command runner --------------------------------------

    _VALIDATION_DETAIL_LINE_CAP: int = 40
    """Tail N lines of combined stdout+stderr we surface in the blocker
    detail. Enough for a tsc / eslint / pytest failure to be actionable
    to the dispatched bug_fixer; not so much that a 10k-line failing
    build spams the Feishu thread."""

    def _run_validation_command(
        self, root: Path, vcmd: ValidationCommand
    ) -> list[InspectionIssue]:
        """Run one project-configured validation command.

        Returns a (possibly empty) list of InspectionIssues. Non-zero exit
        or timeout is a blocker; the tail of the command output is put in
        ``detail`` so the LLM can route it to bug_fixer without a second
        tool call. Unexpected exceptions (missing binary, os error) are
        ALSO treated as blockers — a validation gate we can't run is the
        same as one that failed; silently letting push proceed would
        defeat the purpose.
        """
        cwd = (root / vcmd.cwd).resolve() if vcmd.cwd else root.resolve()
        try:
            cwd.relative_to(root.resolve())
        except ValueError:
            return [
                InspectionIssue(
                    kind="validation_failed",
                    severity="blocker",
                    path=vcmd.name,
                    detail=(
                        f"validation cwd {vcmd.cwd!r} resolved outside "
                        f"project root {root}; refusing to run."
                    ),
                )
            ]

        logger.info(
            "pre-push validation: running %r in %s (timeout=%ss)",
            vcmd.name,
            cwd,
            vcmd.timeout_seconds,
        )
        try:
            res = subprocess.run(
                list(vcmd.cmd),
                cwd=str(cwd),
                check=False,
                capture_output=True,
                text=True,
                timeout=vcmd.timeout_seconds,
            )
        except FileNotFoundError as exc:
            return [
                InspectionIssue(
                    kind="validation_failed",
                    severity="blocker",
                    path=vcmd.name,
                    detail=(
                        f"validation command binary not found: "
                        f"{vcmd.cmd[0]!r} ({exc}). Install the toolchain "
                        f"or remove the validation_commands entry."
                    ),
                )
            ]
        except subprocess.TimeoutExpired as exc:
            tail = self._tail_output(exc.stdout, exc.stderr)
            return [
                InspectionIssue(
                    kind="validation_failed",
                    severity="blocker",
                    path=vcmd.name,
                    detail=(
                        f"timed out after {vcmd.timeout_seconds}s: "
                        f"{shlex.join(vcmd.cmd)}\n--- last output ---\n{tail}"
                    ),
                )
            ]
        except OSError as exc:
            return [
                InspectionIssue(
                    kind="validation_failed",
                    severity="blocker",
                    path=vcmd.name,
                    detail=f"failed to launch validation command: {exc}",
                )
            ]

        if res.returncode == 0:
            return []
        tail = self._tail_output(res.stdout, res.stderr)
        return [
            InspectionIssue(
                kind="validation_failed",
                severity="blocker",
                path=vcmd.name,
                detail=(
                    f"{shlex.join(vcmd.cmd)} exited with rc={res.returncode}\n"
                    f"--- last output ---\n{tail}"
                ),
            )
        ]

    def _tail_output(self, stdout: str | bytes | None, stderr: str | bytes | None) -> str:
        """Return the last ``_VALIDATION_DETAIL_LINE_CAP`` lines of the
        combined output. ``stderr`` typically carries the actual error
        (tsc / eslint / pytest all write errors there), but ``stdout``
        also matters for tools that print diagnostics to stdout (e.g.
        pytest in --tb=short mode), so we concatenate both."""
        def _as_text(value: str | bytes | None) -> str:
            if value is None:
                return ""
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace")
            return value

        out_text = _as_text(stdout)
        err_text = _as_text(stderr)
        # Join with a newline so the last line of stdout cannot fuse
        # with the first line of stderr when stdout lacks a trailing
        # newline — that fusion showed up as a single garbled line in
        # the blocker detail and confused the bug_fixer dispatch.
        parts = [p for p in (out_text, err_text) if p]
        combined = "\n".join(parts).strip()
        if not combined:
            return "(no output)"
        lines = combined.splitlines()
        tail = lines[-self._VALIDATION_DETAIL_LINE_CAP :]
        return "\n".join(tail)

    def _collect_changed_files(self, root: Path) -> list[FileDiffStat]:
        """Tracked files with modifications staged or unstaged. We call
        ``git diff HEAD --numstat`` which covers both staged and unstaged
        vs the last commit — exactly what "about to push" means.

        Uses ``git status --porcelain=v1 -z`` so paths with spaces /
        quotes / non-ASCII bytes are parsed correctly (line-mode git
        shell-escapes those, and string split-on-space then silently
        associates the wrong status with the wrong file).
        """
        numstat = self._git_out(root, ["diff", "HEAD", "--numstat"]).strip()
        raw = self._git_out(root, ["status", "--porcelain=v1", "-z"])
        records = raw.split("\0")
        status_by_path: dict[str, str] = {}
        i = 0
        while i < len(records):
            rec = records[i]
            i += 1
            if not rec or len(rec) < 4:
                continue
            x, y = rec[0], rec[1]
            path = rec[3:]
            # Rename / copy: next NUL-delimited record is the old name;
            # consume & discard so indexes stay aligned.
            if x in ("R", "C"):
                if i < len(records):
                    i += 1
            status_by_path[path] = (x + y).strip()

        out: list[FileDiffStat] = []
        for line in numstat.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            a, d, path = parts[0], parts[1], parts[2]
            # Binary files show as "-" instead of numbers.
            additions = int(a) if a.isdigit() else 0
            deletions = int(d) if d.isdigit() else 0
            out.append(
                FileDiffStat(
                    path=path,
                    additions=additions,
                    deletions=deletions,
                    status=status_by_path.get(path, "M"),
                )
            )
        return out

    def _collect_untracked_files(self, root: Path) -> list[str]:
        out = self._git_out(
            root, ["ls-files", "--others", "--exclude-standard"]
        ).splitlines()
        return [line for line in out if line]

    def _collect_added_text_per_file(self, root: Path) -> dict[str, str]:
        """Extract every *added* line (``+``-prefixed, excluding ``+++``
        headers) per file, grouped. We feed the added text into
        ``secret_scanner`` so we only check what this change introduces —
        not pre-existing repo content."""
        diff = self._git_out(root, ["diff", "HEAD", "--unified=0"])
        out: dict[str, list[str]] = {}
        current_file: str | None = None
        for line in diff.splitlines():
            if line.startswith("+++ b/"):
                current_file = line[len("+++ b/"):].strip() or None
                continue
            if line.startswith("+++") or line.startswith("---"):
                # headers at the top of each diff block
                continue
            if line.startswith("+") and current_file is not None:
                out.setdefault(current_file, []).append(line[1:])
        return {k: "\n".join(v) for k, v in out.items()}

    def _git_out(self, root: Path, args: list[str]) -> str:
        try:
            res = subprocess.run(
                [self._git, *args],
                cwd=str(root),
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except FileNotFoundError as exc:
            raise InspectionGitError(
                f"git binary not found: {self._git}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise InspectionGitError(
                f"git {shlex.join(args)} timed out after 30s in {root}"
            ) from exc
        if res.returncode != 0:
            raise InspectionGitError(
                f"git {shlex.join(args)} failed (rc={res.returncode}) "
                f"in {root}: {res.stderr.strip()}"
            )
        return res.stdout
