"""Post-dispatch verification of tool outputs.

Why this module exists
----------------------
Our tools are trusted to tell the truth. ``CodeWriteService`` returns
``{"path": "...", "bytes_written": 42}``; ``GitOpsService`` returns
``{"commit_sha": "abc123"}``. The LLM then reads those fields and
decides what to do next — push, run tests, write an impl-note, etc.

Three failure modes we've seen or need to guard against:

1. **Silent partial writes**: a future bug in ``CodeWriteService``
   could report success while the on-disk file doesn't actually
   exist (e.g. race with a parallel worktree cleanup, a tmpfs eviction,
   a permissions edge case). The LLM then confidently pushes a ghost
   commit.
2. **Stale claims on retry**: if a tool call retries mid-flight (we
   don't do this today, but the new ``LlmProviderPool`` makes the
   *LLM* call retryable, and follow-up "did that actually land?"
   verifications belong somewhere).
3. **Hallucinated return values**: some provider+model combos
   occasionally return an ``assistant`` message that cites a tool
   result without the tool having run (or with fabricated fields).
   The verifier catches this because it runs against the *actual*
   on-disk / git state, not the returned payload.

Hermes Agent calls this "tool output verification" in their harness
talk. They use it to check that file-edit tools' claimed byte counts
match the post-write file size; we do the same + add a git-commit
sanity check.

Design decisions
----------------
- **Pluggable per tool**: a ``ToolVerifier`` is a registry keyed by
  tool name; unknown tools bypass verification (returning ``ok=True``)
  so adding a new tool never silently breaks the harness.
- **Async validators**: every check must be async so validators can
  invoke subprocess (``git cat-file``) or stat calls without blocking
  the event loop.
- **Failed verification becomes a structured error fed back to the LLM**:
  not an exception that aborts the session. The LLM can then retry,
  acknowledge, or escalate — same pattern as our policy-denied tool
  returns.
- **Validators receive original result**: so the wrapper can decide
  whether to mutate the result (append ``_validation_warning``) or
  replace it (``{"error": "TOOL_VERIFICATION_FAILED"}``). Current
  policy is replace-on-hard-fail so the LLM doesn't mix the stale
  claim with the verification error.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


@dataclass
class ToolVerification:
    """Outcome of one verification pass on one tool call."""

    ok: bool
    error: str | None = None
    # Opaque metadata collected during the check; useful for audit
    # logs (e.g. actual file size we observed) but the LLM only sees
    # ``ok`` / ``error``.
    diagnostics: dict[str, Any] | None = None


# Validator signature: given tool name, arguments dict, and the raw
# result from ``AgentToolExecutor.execute_tool``, return a
# ``ToolVerification``. Context (project root, git cwd, etc.) is
# closed over via closure / partial.
Validator = Callable[
    [str, dict[str, Any], Any], Awaitable[ToolVerification]
]


class ToolVerifier:
    """Registry of per-tool validators.

    Instantiate once per adapter (per Feishu message). Validators are
    registered by tool name; dispatch is O(1). A verifier with no
    registered validators is a no-op — so you can safely wire it
    everywhere and opt in per-role.
    """

    def __init__(
        self, validators: dict[str, Validator] | None = None
    ) -> None:
        self._validators: dict[str, Validator] = dict(validators or {})

    def register(self, tool_name: str, fn: Validator) -> None:
        if tool_name in self._validators:
            # Ambiguous intent: caller probably doesn't mean to shadow
            # an existing validator. Log loudly; keep new one.
            logger.warning(
                "ToolVerifier.register overwriting existing validator for %s",
                tool_name,
            )
        self._validators[tool_name] = fn

    def registered_tools(self) -> set[str]:
        return set(self._validators)

    async def verify(
        self, tool_name: str, arguments: dict[str, Any], result: Any
    ) -> ToolVerification:
        """Run the validator for ``tool_name`` or return a trivial OK.

        Validators themselves SHOULD NOT raise — they should return
        ``ok=False`` with an error message. If one does raise anyway,
        we treat it as a verifier bug (not a tool failure) and return
        ``ok=True`` with a warning logged; we don't want a validator
        bug to destroy a legitimate tool call.
        """
        validator = self._validators.get(tool_name)
        if validator is None:
            return ToolVerification(ok=True)
        try:
            return await validator(tool_name, arguments, result)
        except Exception:  # pragma: no cover — defensive
            logger.exception(
                "tool verifier for %s raised; treating as pass", tool_name
            )
            return ToolVerification(
                ok=True,
                error=None,
                diagnostics={"verifier_error": "validator raised"},
            )


# ---------------------------------------------------------------------------
# Built-in validators
# ---------------------------------------------------------------------------


def make_write_project_code_validator(
    project_root_resolver: Callable[[dict[str, Any]], Path | None],
) -> Validator:
    """Verify the file the tool claims to have written actually exists.

    ``project_root_resolver`` receives the tool arguments and returns the
    project root for THAT call (we support multiple projects, so the
    validator can't hard-code a single root). Returning ``None`` signals
    "don't validate this call" — used when the project isn't resolvable,
    which the tool itself will already have erred on.

    Checks performed:

    - ``result`` is a dict with no ``error`` field (we skip validation
      on tool-side errors — the error is already visible to the LLM).
    - The resolved path exists on disk.
    - ``bytes_written`` (if present) matches ``stat().st_size`` within a
      small tolerance (CRLF normalization and trailing newlines can
      cause a ±2 byte drift).

    Returns ``ok=False`` with a specific ``error`` string if any check
    fails. The caller replaces the tool result with the error so the
    LLM must react rather than proceeding on a fiction.
    """

    async def _validate(
        tool_name: str, args: dict[str, Any], result: Any
    ) -> ToolVerification:
        if not isinstance(result, dict):
            return ToolVerification(ok=True)
        if result.get("error"):
            # Tool itself failed — error already visible to LLM.
            return ToolVerification(ok=True)

        # For write_project_code_batch we need to recurse over files
        if tool_name == "write_project_code_batch":
            files = result.get("files") or []
            for entry in files:
                if not isinstance(entry, dict):
                    continue
                v = await _validate_single(args, entry)
                if not v.ok:
                    return v
            return ToolVerification(ok=True)

        return await _validate_single(args, result)

    async def _validate_single(
        args: dict[str, Any], payload: dict[str, Any]
    ) -> ToolVerification:
        project_root = project_root_resolver(args)
        if project_root is None:
            return ToolVerification(
                ok=True,
                diagnostics={"skipped": "project_root not resolvable"},
            )
        rel_path = (
            payload.get("path")
            or args.get("relative_path")
            or args.get("path")
        )
        if not rel_path:
            return ToolVerification(
                ok=True, diagnostics={"skipped": "no path to verify"}
            )
        target = (project_root / rel_path).resolve()
        # Containment re-check: the resolved path must stay under the
        # project root. If a future bug in CodeWriteService returned a
        # crafted path (e.g. "../other-project/foo"), we'd catch it
        # here even though CodeWriteService should have blocked it
        # earlier. Defense in depth.
        try:
            target.relative_to(project_root.resolve())
        except ValueError:
            return ToolVerification(
                ok=False,
                error=f"written path escapes project root: {rel_path}",
            )
        if not target.exists():
            return ToolVerification(
                ok=False,
                error=(
                    f"write_project_code claimed success but file not "
                    f"found on disk: {rel_path}"
                ),
            )
        if not target.is_file():
            return ToolVerification(
                ok=False,
                error=f"target is not a regular file: {rel_path}",
            )

        claimed = payload.get("bytes_written")
        if isinstance(claimed, int):
            # ``CodeWriteService.write_text`` reports ``len(encoded)``
            # — the exact byte count of the file as it was written.
            # On POSIX text files (our production target) this equals
            # ``st_size`` exactly. A 2-byte slack used to live here but
            # masked silent corruption; we now require exact parity
            # and only fall back to a single-byte slack for trailing-
            # newline additions applied by some editors on save.
            actual = target.stat().st_size
            drift = abs(actual - claimed)
            # 1-byte drift: accept (a trailing '\n' normalization is
            # the only common cause we're willing to paper over).
            # 2+ byte drift: fail loudly. If this ever fires on a real
            # write, ``CodeWriteService`` is lying — don't let the LLM
            # treat the write as successful.
            if drift > 1:
                return ToolVerification(
                    ok=False,
                    error=(
                        f"bytes_written mismatch for {rel_path}: "
                        f"claimed {claimed}, found {actual}"
                    ),
                    diagnostics={
                        "claimed": claimed,
                        "actual": actual,
                        "drift": drift,
                    },
                )
        return ToolVerification(ok=True)

    return _validate


def make_git_commit_validator(
    project_root_resolver: Callable[[dict[str, Any]], Path | None],
) -> Validator:
    """Verify the commit SHA the tool returned actually exists in git.

    Uses ``git cat-file -t <sha>`` which returns ``commit`` for valid
    commits, non-zero for anything else. We check:

    - ``commit_sha`` looks like a SHA (7-40 hex chars)
    - ``git cat-file -t <sha>`` returns type ``commit``

    If either check fails we return ``ok=False``; the LLM will see a
    verification error in place of the fake commit SHA.

    We do NOT re-read commit metadata (author, message, file list) —
    that's another round-trip per commit, and the SHA-existence check
    is already strong evidence the tool actually committed.
    """

    async def _validate(
        tool_name: str, args: dict[str, Any], result: Any
    ) -> ToolVerification:
        if not isinstance(result, dict):
            return ToolVerification(ok=True)
        if result.get("error"):
            return ToolVerification(ok=True)

        sha = result.get("commit_sha")
        if not sha:
            return ToolVerification(ok=True)  # nothing to verify
        if not isinstance(sha, str) or not _SHA_RE.match(sha):
            return ToolVerification(
                ok=False,
                error=f"commit_sha does not look like a git SHA: {sha!r}",
            )

        project_root = project_root_resolver(args)
        if project_root is None:
            return ToolVerification(
                ok=True,
                diagnostics={"skipped": "project_root not resolvable"},
            )

        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(project_root),
            "cat-file",
            "-t",
            sha,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=5.0
            )
        except asyncio.TimeoutError:
            # Don't deadlock the session on a hung git — treat
            # verification as inconclusive (pass) but log loudly.
            logger.warning(
                "git cat-file timed out verifying commit %s at %s",
                sha,
                project_root,
            )
            try:
                proc.kill()
            except ProcessLookupError:  # pragma: no cover
                pass
            return ToolVerification(
                ok=True,
                diagnostics={"skipped": "git cat-file timed out"},
            )

        if proc.returncode != 0:
            return ToolVerification(
                ok=False,
                error=(
                    f"git cat-file rejected commit_sha {sha}: "
                    + stderr.decode(errors="replace").strip()
                ),
            )
        object_type = stdout.decode(errors="replace").strip()
        if object_type != "commit":
            return ToolVerification(
                ok=False,
                error=(
                    f"commit_sha {sha} points to a {object_type}, "
                    "not a commit"
                ),
            )
        return ToolVerification(ok=True)

    return _validate


def build_default_validators(
    *, project_root_resolver: Callable[[dict[str, Any]], Path | None]
) -> dict[str, Validator]:
    """Bundle the built-in validators into a registry dict.

    Wire this via ``ToolVerifier(build_default_validators(resolver=…))``
    at adapter construction time. Extend by mutating the returned dict
    before handing it to ``ToolVerifier``.
    """
    write_v = make_write_project_code_validator(project_root_resolver)
    commit_v = make_git_commit_validator(project_root_resolver)
    return {
        "write_project_code": write_v,
        "write_project_code_batch": write_v,
        "git_commit": commit_v,
    }
