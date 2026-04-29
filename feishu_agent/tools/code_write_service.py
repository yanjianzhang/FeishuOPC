"""Project source-code write service.

Separate from ``workflow_service`` on purpose: workflow artifacts live in
fixed methodology directories (``specs/`` ``stories/`` ``reviews/``) and
are categorically safe to write. Source code (``lib/`` ``test/``
``tools/`` ``example_app/lib/`` ...) is not — it needs a tighter,
auditable permission model so an LLM cannot accidentally (or adversarially)
rewrite secrets, ``.git/``, CI config, or ship massive silent diffs.

Four layers of defense enforced by this service:

1. Project gate  — ``project_id`` must resolve to a configured
   ``project_repo_root``.
2. Path gate     — resolved target must stay inside ``project_repo_root``,
   must hit one of ``policy.allowed_write_roots`` prefixes, and must NOT
   hit any ``policy.denied_path_segments`` substring.
3. Size gate     — single file above ``hard_max_bytes_per_file`` is
   **refused**; above ``require_confirmation_above_bytes`` requires the
   caller to pass ``confirmed=True`` (LLM is expected to have gone through
   ``request_confirmation`` first — that flag is an explicit acknowledgement,
   not a silent bypass).
4. Batch gate    — batch size capped at ``max_files_per_write_batch``;
   per-file size gates still apply.

Every successful write appends a JSONL record to
``{app_repo_root}/data/code-writes/<trace_id>.jsonl`` with path, bytes,
sha256 before/after, and the ``reason`` supplied by the caller. The
parent executor is expected to also push a Feishu thread line per write.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from feishu_agent.tools import secret_scanner

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Non-overridable baseline: these segments are ALWAYS denied, regardless of
# what a user-supplied policy JSON says. Prevents a misconfig (accidental or
# malicious) from opening up ``.env`` / ``.git`` / keys.
# ---------------------------------------------------------------------------
_HARDCODED_DENIED_SEGMENTS: tuple[str, ...] = (
    ".env",
    ".git",
    "secrets",
    "id_ed25519",
    "id_rsa",
    ".pem",
    ".key",
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CodeWriteError(Exception):
    """Base error for code-write operations. Carries a stable code."""

    code: str = "CODE_WRITE_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class CodeWritePathError(CodeWriteError):
    code = "CODE_WRITE_PATH_DENIED"


class CodeWriteSizeError(CodeWriteError):
    code = "CODE_WRITE_SIZE_EXCEEDED"


class CodeWriteConfirmationRequired(CodeWriteError):
    code = "CODE_WRITE_NEEDS_CONFIRMATION"


class CodeWriteBatchError(CodeWriteError):
    code = "CODE_WRITE_BATCH_DENIED"


class CodeWriteProjectError(CodeWriteError):
    code = "CODE_WRITE_UNKNOWN_PROJECT"


class CodeWriteSecretError(CodeWriteError):
    """Write refused because ``content`` matched a secret pattern."""

    code = "CODE_WRITE_SECRET_DETECTED"

    def __init__(self, message: str, *, findings: list[secret_scanner.SecretFinding]) -> None:
        super().__init__(message)
        self.findings = findings


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationCommand:
    """Per-project validation command run by the pre-push inspector.

    These are the project-side contracts the tech-lead must satisfy BEFORE
    minting an inspection_token (e.g. ``npm run typecheck``, ``pnpm lint``,
    ``pytest -q``). A non-zero exit or a timeout becomes a blocker that
    refuses the push, preventing the "looked green in review, failed in
    CI" class of bug that used to slip through when ``pre_push_inspector``
    only did secret/path/size checks.
    """

    name: str
    """Human-readable identifier; appears as ``path`` in the resulting
    InspectionIssue so the LLM can quote it verbatim."""

    cmd: tuple[str, ...]
    """argv list. Executed WITHOUT a shell. Paths inside are treated as
    relative to ``project_repo_root / cwd``."""

    cwd: str = ""
    """Subdirectory of the project repo to run the command in. Empty =
    repo root. POSIX separators; must stay inside the repo."""

    timeout_seconds: int = 120
    """Hard upper bound on the subprocess; timeout -> blocker."""


@dataclass(frozen=True)
class CodeWritePolicy:
    """Per-project rules for where / how big / how many files the agent
    may write. Default values are conservative on purpose; callers pick
    them explicitly per project."""

    allowed_write_roots: tuple[str, ...]
    """Path prefixes (relative to project_repo_root, POSIX separators).
    The resolved target must live under one of these. Each project
    declares its own shape in ``policies.jsonl`` — FeishuOPC itself has
    no opinion about directory layouts."""

    denied_path_segments: tuple[str, ...] = (
        ".env",
        ".git",
        "secrets",
        "id_ed25519",
        "id_rsa",
        ".pem",
        ".key",
        "node_modules",
        ".dart_tool",
        "build/",
        ".venv",
    )
    """Case-insensitive substring match on the resolved target path.
    Any hit refuses the write unconditionally."""

    allowed_read_roots: tuple[str, ...] | None = None
    """If set, ``read_source`` / ``list_paths`` are limited to these
    prefixes. If None, falls back to ``allowed_write_roots``."""

    allowed_artifact_roots: tuple[str, ...] = (
        "specs/",
        "stories/",
        "reviews/",
        "docs/stories/",
        "docs/specs/",
        "docs/reviews/",
    )
    """Paths where *workflow artifacts* (story files, review notes, spec
    drafts written by ``write_role_artifact`` / workflow tools) may
    legitimately live. The LLM still cannot call ``write_project_code``
    here — those tools enforce ``allowed_write_roots`` only. But the
    pre-push inspector and ``git_commit`` staging treat these as
    policy-compliant so a commit that contains a new story file isn't
    blocked as "outside policy". Per-project overrides via
    ``policies.jsonl`` can add things like ``"docs/"`` or shrink the
    default. Hardcoded denylist still applies — you cannot artifact
    your way into ``.env`` or ``secrets/``."""

    hard_max_bytes_per_file: int = 512 * 1024  # 512KB
    """Absolute ceiling per file. Written files above this are refused."""

    require_confirmation_above_bytes: int = 64 * 1024  # 64KB
    """Above this, caller must pass ``confirmed=True``. Also applies to
    overwrites that change > this many bytes vs existing content."""

    max_files_per_write_batch: int = 30
    """Upper bound on files in a single ``write_batch`` call."""

    protected_branches: tuple[str, ...] = ("main", "master")
    """Branches the agent is **never** allowed to push to. ``GitOpsService``
    refuses ``git_push`` when the current branch is one of these (or matches
    one of them by exact name). Always includes ``main`` / ``master`` by
    default; a project can override via ``policies.jsonl`` to add things
    like ``"release"`` or ``"prod"``. Overriding to an empty tuple is
    allowed only if the caller is certain — we still advise keeping
    ``main`` in the list."""

    validation_commands: tuple[ValidationCommand, ...] = ()
    """Commands the pre-push inspector runs locally before minting a
    token. Empty tuple (the default) preserves the legacy "secret/path/
    size only" behavior. Each command is executed on the inspected worktree
    HEAD; any non-zero exit or timeout becomes a blocker and refuses the
    push. This is the local mirror of GitHub Actions jobs — configured per
    project in ``policies.jsonl`` so downstream projects without a
    typecheck/lint target keep working unchanged."""

    def is_protected_branch(self, branch: str) -> bool:
        b = (branch or "").strip()
        if not b:
            return True  # refuse on empty — caller must supply explicit branch
        return b in self.protected_branches

    def is_readable_path(self, rel_posix: str) -> bool:
        roots = self.allowed_read_roots or self.allowed_write_roots
        return _matches_any_prefix(rel_posix, roots)

    def is_writable_path(self, rel_posix: str) -> bool:
        return _matches_any_prefix(rel_posix, self.allowed_write_roots)

    def is_artifact_path(self, rel_posix: str) -> bool:
        """True if ``rel_posix`` sits inside a declared workflow-artifact
        root. Used by the pre-push inspector and ``git_commit`` staging to
        allow files written by ``write_role_artifact`` / workflow tools
        through even though they're outside ``allowed_write_roots``."""
        return _matches_any_prefix(rel_posix, self.allowed_artifact_roots)

    def is_commit_allowed_path(self, rel_posix: str) -> bool:
        """True if ``rel_posix`` is allowed to show up in a commit: either
        a code-writable path or a workflow-artifact path. Denied segments
        (.env / secrets / keys) still fail regardless."""
        return self.is_writable_path(rel_posix) or self.is_artifact_path(
            rel_posix
        )

    def has_denied_segment(self, candidate: str) -> str | None:
        low = candidate.lower()
        for needle in self.denied_path_segments:
            if needle.lower() in low:
                return needle
        return None


def _matches_any_prefix(rel_posix: str, roots: Iterable[str]) -> bool:
    for raw in roots:
        root = raw.rstrip("/") + "/"
        if rel_posix == raw.rstrip("/") or rel_posix.startswith(root):
            return True
    return False


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@dataclass
class CodeWriteAuditLog:
    """Append-only JSONL audit log. One file per trace_id so concurrent
    agent runs don't interleave."""

    root: Path
    trace_id: str = "no-trace"

    def __post_init__(self) -> None:
        self.root = Path(self.root)

    def _path(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in self.trace_id) or "no-trace"
        return self.root / f"{safe}.jsonl"

    def append(self, record: dict[str, Any]) -> None:
        try:
            line = json.dumps(record, ensure_ascii=False, default=str)
            with self._path().open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            logger.warning("code-write audit append failed", exc_info=True)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@dataclass
class CodeWriteResult:
    path: str  # project-relative POSIX
    bytes_written: int
    bytes_delta: int  # new - old (0 for new files, negative when shrinking)
    is_new_file: bool
    sha256_before: str | None
    sha256_after: str


class CodeWriteService:
    """Permission-aware read/write for a project's source tree."""

    def __init__(
        self,
        *,
        project_roots: dict[str, Path],
        policies: dict[str, CodeWritePolicy],
        audit_root: Path | None = None,
        trace_id: str = "no-trace",
    ) -> None:
        self._project_roots = {
            pid: Path(root).resolve() for pid, root in project_roots.items()
        }
        self._policies = dict(policies)
        self._audit_root = audit_root
        self._trace_id = trace_id

    # -- discovery ------------------------------------------------------

    def describe_policy(self, project_id: str) -> dict[str, Any]:
        _, pol = self._resolve(project_id)
        return {
            "project_id": project_id,
            "allowed_write_roots": list(pol.allowed_write_roots),
            "allowed_read_roots": list(
                pol.allowed_read_roots or pol.allowed_write_roots
            ),
            "denied_path_segments": list(pol.denied_path_segments),
            "hard_max_bytes_per_file": pol.hard_max_bytes_per_file,
            "require_confirmation_above_bytes": pol.require_confirmation_above_bytes,
            "max_files_per_write_batch": pol.max_files_per_write_batch,
            "protected_branches": list(pol.protected_branches),
            "validation_commands": [
                {
                    "name": v.name,
                    "cmd": list(v.cmd),
                    "cwd": v.cwd,
                    "timeout_seconds": v.timeout_seconds,
                }
                for v in pol.validation_commands
            ],
        }

    # -- read -----------------------------------------------------------

    def read_source(
        self,
        *,
        project_id: str,
        relative_path: str,
        max_bytes: int = 512 * 1024,
    ) -> dict[str, Any]:
        root, pol = self._resolve(project_id)
        target = self._guard_path(root, pol, relative_path, for_write=False)
        if not target.is_file():
            raise CodeWriteError(f"Not a file or does not exist: {relative_path}")
        size = target.stat().st_size
        truncated = size > max_bytes
        with target.open("r", encoding="utf-8", errors="replace") as fh:
            content = fh.read(max_bytes)
        return {
            "project_id": project_id,
            "path": self._rel_posix(target, root),
            "bytes": size,
            "truncated": truncated,
            "content": content,
        }

    def list_paths(
        self,
        *,
        project_id: str,
        sub_path: str = "",
        max_entries: int = 200,
    ) -> dict[str, Any]:
        root, pol = self._resolve(project_id)
        rel = sub_path.strip("/")
        if rel == "":
            # list top-level read roots
            entries: list[dict[str, Any]] = []
            for raw in pol.allowed_read_roots or pol.allowed_write_roots:
                clean = raw.rstrip("/")
                p = (root / clean).resolve()
                try:
                    p.relative_to(root)
                except ValueError:
                    continue
                if p.exists():
                    entries.append(
                        {
                            "name": clean,
                            "type": "dir" if p.is_dir() else "file",
                            "rel_path": clean,
                        }
                    )
            return {
                "project_id": project_id,
                "sub_path": "",
                "entries": entries,
                "truncated": False,
            }

        target = self._guard_path(root, pol, rel, for_write=False, must_exist=False)
        if not target.exists():
            return {
                "project_id": project_id,
                "sub_path": rel,
                "entries": [],
                "exists": False,
                "truncated": False,
            }
        if not target.is_dir():
            raise CodeWriteError(f"Not a directory: {rel}")

        entries = []
        truncated = False
        for i, entry in enumerate(sorted(target.iterdir())):
            if i >= max_entries:
                truncated = True
                break
            entries.append(
                {
                    "name": entry.name,
                    "type": "dir" if entry.is_dir() else "file",
                    "rel_path": self._rel_posix(entry, root),
                    "size": entry.stat().st_size if entry.is_file() else None,
                }
            )
        return {
            "project_id": project_id,
            "sub_path": rel,
            "entries": entries,
            "exists": True,
            "truncated": truncated,
        }

    # -- write ----------------------------------------------------------

    def write_source(
        self,
        *,
        project_id: str,
        relative_path: str,
        content: str,
        reason: str,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        root, pol = self._resolve(project_id)
        target = self._guard_path(root, pol, relative_path, for_write=True)
        self._check_secret_policy(content, rel_posix=self._rel_posix(target, root))
        result = self._do_write(
            root=root, pol=pol, target=target, content=content,
            reason=reason, confirmed=confirmed,
        )
        return self._serialize(result, project_id=project_id, reason=reason)

    def write_sources_batch(
        self,
        *,
        project_id: str,
        files: list[dict[str, Any]],
        reason: str,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        root, pol = self._resolve(project_id)

        if not isinstance(files, list) or not files:
            raise CodeWriteBatchError("files must be a non-empty list.")
        if len(files) > pol.max_files_per_write_batch:
            raise CodeWriteBatchError(
                f"Too many files: {len(files)} > max_files_per_write_batch="
                f"{pol.max_files_per_write_batch}."
            )

        planned: list[tuple[Path, str, str, str]] = []
        for idx, item in enumerate(files):
            if not isinstance(item, dict):
                raise CodeWriteBatchError(f"files[{idx}] is not an object.")
            rel = item.get("relative_path") or item.get("path")
            body = item.get("content")
            per_reason = item.get("reason") or reason
            if not rel or not isinstance(rel, str):
                raise CodeWriteBatchError(f"files[{idx}].relative_path required.")
            if body is None or not isinstance(body, str):
                raise CodeWriteBatchError(f"files[{idx}].content must be a string.")
            target = self._guard_path(root, pol, rel, for_write=True)
            self._check_size_policy(
                target=target, content=body, pol=pol, confirmed=confirmed,
            )
            self._check_secret_policy(body, rel_posix=self._rel_posix(target, root))
            planned.append((target, body, per_reason, rel))

        written: list[dict[str, Any]] = []
        for target, body, per_reason, _rel in planned:
            res = self._do_write(
                root=root, pol=pol, target=target, content=body,
                reason=per_reason, confirmed=confirmed,
                skip_size_check=True,  # already validated above
            )
            written.append(self._serialize(res, project_id=project_id, reason=per_reason))
        return {
            "project_id": project_id,
            "count": len(written),
            "files": written,
        }

    # -- internal -------------------------------------------------------

    def _resolve(self, project_id: str) -> tuple[Path, CodeWritePolicy]:
        root = self._project_roots.get(project_id)
        pol = self._policies.get(project_id)
        if root is None or pol is None:
            raise CodeWriteProjectError(
                f"No code-write policy configured for project_id={project_id!r}."
            )
        return root, pol

    def _guard_path(
        self,
        root: Path,
        pol: CodeWritePolicy,
        relative_path: str,
        *,
        for_write: bool,
        must_exist: bool = False,
    ) -> Path:
        if not relative_path or relative_path.startswith("/"):
            raise CodeWritePathError(
                f"Invalid relative_path: {relative_path!r} (must be non-empty, non-absolute)."
            )

        # ``.resolve()`` follows symlinks before we can inspect them, so
        # checking ``target.is_symlink()`` afterwards always returns
        # False. Walk the un-resolved path instead and refuse if *any*
        # intermediate component is a symlink. This blocks tricks like
        # ``docs/link-to-.env`` where a symlink inside the repo points
        # elsewhere inside the repo (still a writable root by policy,
        # but semantically a sneaky redirect) as well as symlinks that
        # escape the project.
        unresolved = root / relative_path
        probe = root
        for part in Path(relative_path).parts:
            probe = probe / part
            if probe.is_symlink():
                raise CodeWritePathError(
                    f"Refusing to follow symlink: {self._rel_posix(probe, root)}"
                )

        target = unresolved.resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise CodeWritePathError(
                f"Path {relative_path!r} escapes project root."
            ) from exc

        rel_posix = self._rel_posix(target, root)

        denied = pol.has_denied_segment(rel_posix)
        if denied:
            raise CodeWritePathError(
                f"Path {rel_posix!r} hits denied segment {denied!r}."
            )

        if for_write:
            if not pol.is_writable_path(rel_posix):
                raise CodeWritePathError(
                    f"Path {rel_posix!r} is not under any allowed_write_roots "
                    f"{list(pol.allowed_write_roots)}."
                )
        else:
            if not pol.is_readable_path(rel_posix):
                raise CodeWritePathError(
                    f"Path {rel_posix!r} is not under any allowed_read_roots."
                )

        if must_exist and not target.exists():
            raise CodeWriteError(f"Does not exist: {rel_posix}")

        return target

    def _check_secret_policy(self, content: str, *, rel_posix: str) -> None:
        """Refuse writes whose content contains secret material.

        Defense in depth: the LLM — or upstream caller — must not be able
        to smuggle a credential (private key, AWS key, OpenAI key, GitHub
        PAT, etc.) into a file we'd then commit on the user's behalf. See
        ``secret_scanner.py`` for the rule set.
        """
        try:
            secret_scanner.ensure_clean(content, path=rel_posix)
        except secret_scanner.SecretDetectedError as exc:
            raise CodeWriteSecretError(str(exc), findings=list(exc.findings)) from exc

    def _check_size_policy(
        self,
        *,
        target: Path,
        content: str,
        pol: CodeWritePolicy,
        confirmed: bool,
    ) -> None:
        size_new = len(content.encode("utf-8"))
        if size_new > pol.hard_max_bytes_per_file:
            raise CodeWriteSizeError(
                f"File too large: {size_new} bytes > hard_max_bytes_per_file="
                f"{pol.hard_max_bytes_per_file}."
            )
        # delta (for overwrites)
        size_old = target.stat().st_size if target.is_file() else 0
        delta = abs(size_new - size_old)
        ceiling = pol.require_confirmation_above_bytes
        needs_confirm = max(size_new, delta) > ceiling
        if needs_confirm and not confirmed:
            raise CodeWriteConfirmationRequired(
                f"Write of {size_new} bytes (delta {delta}) exceeds "
                f"require_confirmation_above_bytes={ceiling}. "
                f"Call request_confirmation first, then retry with confirmed=True."
            )

    def _do_write(
        self,
        *,
        root: Path,
        pol: CodeWritePolicy,
        target: Path,
        content: str,
        reason: str,
        confirmed: bool,
        skip_size_check: bool = False,
    ) -> CodeWriteResult:
        if not skip_size_check:
            self._check_size_policy(
                target=target, content=content, pol=pol, confirmed=confirmed,
            )

        is_new = not target.is_file()
        sha_before: str | None = None
        size_before = 0
        if not is_new:
            existing = target.read_bytes()
            sha_before = hashlib.sha256(existing).hexdigest()
            size_before = len(existing)

        encoded = content.encode("utf-8")
        sha_after = hashlib.sha256(encoded).hexdigest()

        target.parent.mkdir(parents=True, exist_ok=True)
        # atomic-ish write: write to sibling .tmp then replace
        tmp = target.with_suffix(target.suffix + ".tmp-codewrite")
        try:
            tmp.write_bytes(encoded)
            tmp.replace(target)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

        rel = self._rel_posix(target, root)

        if self._audit_root is not None:
            audit = CodeWriteAuditLog(
                root=self._audit_root, trace_id=self._trace_id,
            )
            audit.append(
                {
                    "ts": time.time(),
                    "trace_id": self._trace_id,
                    "project_root": str(root),
                    "path": rel,
                    "bytes_before": size_before,
                    "bytes_after": len(encoded),
                    "sha256_before": sha_before,
                    "sha256_after": sha_after,
                    "is_new_file": is_new,
                    "confirmed": confirmed,
                    "reason": reason,
                }
            )

        return CodeWriteResult(
            path=rel,
            bytes_written=len(encoded),
            bytes_delta=len(encoded) - size_before,
            is_new_file=is_new,
            sha256_before=sha_before,
            sha256_after=sha_after,
        )

    @staticmethod
    def _rel_posix(target: Path, root: Path) -> str:
        return str(target.relative_to(root)).replace("\\", "/")

    @staticmethod
    def _serialize(
        r: CodeWriteResult, *, project_id: str, reason: str
    ) -> dict[str, Any]:
        return {
            "project_id": project_id,
            "path": r.path,
            "bytes_written": r.bytes_written,
            "bytes_delta": r.bytes_delta,
            "is_new_file": r.is_new_file,
            "sha256_before": r.sha256_before,
            "sha256_after": r.sha256_after,
            "reason": reason,
        }


# ---------------------------------------------------------------------------
# Policy loading from JSONL
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PolicyFileEntry:
    project_id: str
    project_repo_root: Path | None
    policy: CodeWritePolicy


class PolicyFileError(CodeWriteError):
    code = "CODE_WRITE_POLICY_FILE_INVALID"


def _as_str_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise PolicyFileError(f"{field_name} must be a list of strings.")
    out: list[str] = []
    for idx, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise PolicyFileError(
                f"{field_name}[{idx}] must be a non-empty string."
            )
        out.append(item)
    return tuple(out)


def _as_positive_int(value: Any, field_name: str, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PolicyFileError(f"{field_name} must be a positive integer.")
    return value


def _build_policy_from_entry(entry: dict[str, Any], *, default: CodeWritePolicy) -> CodeWritePolicy:
    """Merge a JSON entry on top of ``default``. Missing fields inherit from
    the default. Required: ``allowed_write_roots`` (non-empty).

    ``denied_path_segments`` in the entry is UNIONED with the hardcoded
    baseline so a misconfig cannot open up ``.env``/``.git``/keys.
    """
    write_roots = _as_str_tuple(
        entry.get("allowed_write_roots"), "allowed_write_roots"
    )
    if not write_roots:
        write_roots = default.allowed_write_roots
    if not write_roots:
        raise PolicyFileError("allowed_write_roots must be non-empty.")

    read_roots_raw = entry.get("allowed_read_roots")
    read_roots: tuple[str, ...] | None
    if read_roots_raw is None:
        read_roots = default.allowed_read_roots
    else:
        read_roots = _as_str_tuple(read_roots_raw, "allowed_read_roots")

    artifact_roots_raw = entry.get("allowed_artifact_roots")
    if artifact_roots_raw is None:
        artifact_roots = default.allowed_artifact_roots
    else:
        artifact_roots = _as_str_tuple(
            artifact_roots_raw, "allowed_artifact_roots"
        )

    entry_denied = _as_str_tuple(
        entry.get("denied_path_segments"), "denied_path_segments"
    )
    base_denied = default.denied_path_segments or _HARDCODED_DENIED_SEGMENTS
    merged_denied = tuple(
        dict.fromkeys(  # order-preserving dedupe
            list(base_denied) + list(_HARDCODED_DENIED_SEGMENTS) + list(entry_denied)
        )
    )

    # protected_branches: entry may override, but hardcoded ("main", "master")
    # always stays in the set — never let a misconfig open a direct push
    # path to production.
    entry_protected = _as_str_tuple(
        entry.get("protected_branches"), "protected_branches"
    )
    merged_protected = tuple(
        dict.fromkeys(
            list(default.protected_branches)
            + ["main", "master"]
            + list(entry_protected)
        )
    )

    validation_commands_raw = entry.get("validation_commands")
    if validation_commands_raw is None:
        validation_commands = default.validation_commands
    else:
        validation_commands = _as_validation_commands(validation_commands_raw)

    return CodeWritePolicy(
        allowed_write_roots=write_roots,
        denied_path_segments=merged_denied,
        allowed_read_roots=read_roots,
        allowed_artifact_roots=artifact_roots,
        hard_max_bytes_per_file=_as_positive_int(
            entry.get("hard_max_bytes_per_file"),
            "hard_max_bytes_per_file",
            default.hard_max_bytes_per_file,
        ),
        require_confirmation_above_bytes=_as_positive_int(
            entry.get("require_confirmation_above_bytes"),
            "require_confirmation_above_bytes",
            default.require_confirmation_above_bytes,
        ),
        max_files_per_write_batch=_as_positive_int(
            entry.get("max_files_per_write_batch"),
            "max_files_per_write_batch",
            default.max_files_per_write_batch,
        ),
        protected_branches=merged_protected,
        validation_commands=validation_commands,
    )


def _as_validation_commands(value: Any) -> tuple[ValidationCommand, ...]:
    """Parse ``policies.jsonl`` ``validation_commands`` array.

    Each element must be an object with ``name`` (str) + ``cmd`` (list[str]).
    ``cwd`` defaults to ``""`` (repo root); ``timeout_seconds`` defaults to 120.
    Any structural problem raises ``PolicyFileError`` so misconfig fails
    loudly — a silently-ignored validation gate is worse than no gate.
    """
    if not isinstance(value, list):
        raise PolicyFileError("validation_commands must be a list of objects.")
    out: list[ValidationCommand] = []
    for idx, item in enumerate(value):
        if not isinstance(item, dict):
            raise PolicyFileError(
                f"validation_commands[{idx}] must be an object."
            )
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise PolicyFileError(
                f"validation_commands[{idx}].name must be a non-empty string."
            )
        cmd = _as_str_tuple(
            item.get("cmd"), f"validation_commands[{idx}].cmd"
        )
        if not cmd:
            raise PolicyFileError(
                f"validation_commands[{idx}].cmd must be non-empty."
            )
        cwd_raw = item.get("cwd", "")
        if cwd_raw is None:
            cwd_raw = ""
        if not isinstance(cwd_raw, str):
            raise PolicyFileError(
                f"validation_commands[{idx}].cwd must be a string."
            )
        cwd_clean = cwd_raw.strip().strip("/")
        if ".." in Path(cwd_clean).parts:
            raise PolicyFileError(
                f"validation_commands[{idx}].cwd must stay inside the repo "
                f"(no '..' segments)."
            )
        timeout_seconds = _as_positive_int(
            item.get("timeout_seconds"),
            f"validation_commands[{idx}].timeout_seconds",
            120,
        )
        out.append(
            ValidationCommand(
                name=name.strip(),
                cmd=cmd,
                cwd=cwd_clean,
                timeout_seconds=timeout_seconds,
            )
        )
    return tuple(out)


def load_policy_file(
    path: Path,
    *,
    default_policy: CodeWritePolicy,
    fallback_project_roots: dict[str, Path] | None = None,
) -> dict[str, _PolicyFileEntry]:
    """Parse a JSONL file of per-project code-write policies.

    File format: one JSON object per line. Schema:

    ```jsonl
    {"project_id": "<project-id>",
     "project_repo_root": "/abs/path/or/~/path",
     "allowed_write_roots": ["lib/", "test/"],
     "allowed_read_roots":  ["lib/", "docs/"],
     "denied_path_segments": [".env", "secrets"],
     "hard_max_bytes_per_file": 524288,
     "require_confirmation_above_bytes": 65536,
     "max_files_per_write_batch": 30}
    ```

    Fields other than ``project_id`` are optional:
    - ``project_repo_root`` missing → falls back to ``fallback_project_roots[project_id]``.
    - Any policy field missing → inherits from ``default_policy``.
    - ``denied_path_segments`` is always UNIONED with the hardcoded baseline
      (``.env``/``.git``/``secrets``/``.key``/``.pem``/``id_ed25519``/``id_rsa``).

    A malformed line aborts the whole file — we refuse to half-load a
    security policy. If ``path`` does not exist, returns ``{}`` silently.
    """
    result: dict[str, _PolicyFileEntry] = {}
    if not path.is_file():
        return result

    fallback_project_roots = fallback_project_roots or {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyFileError(f"Failed to read policy file {path}: {exc}") from exc

    for lineno, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise PolicyFileError(
                f"{path}:{lineno}: invalid JSON: {exc.msg}"
            ) from exc
        if not isinstance(entry, dict):
            raise PolicyFileError(f"{path}:{lineno}: entry must be an object.")

        pid = entry.get("project_id")
        if not isinstance(pid, str) or not pid:
            raise PolicyFileError(f"{path}:{lineno}: project_id required.")

        repo_root_raw = entry.get("project_repo_root")
        if isinstance(repo_root_raw, str) and repo_root_raw:
            repo_root: Path | None = Path(repo_root_raw).expanduser()
        else:
            repo_root = fallback_project_roots.get(pid)

        try:
            policy = _build_policy_from_entry(entry, default=default_policy)
        except PolicyFileError as exc:
            raise PolicyFileError(f"{path}:{lineno}: {exc.message}") from exc

        result[pid] = _PolicyFileEntry(
            project_id=pid, project_repo_root=repo_root, policy=policy
        )

    return result
