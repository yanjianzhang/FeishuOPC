"""Whitelisted runner for the ``.specify/scripts/bash/*.sh`` scripts.

Background
----------
Every speckit command file (``.cursor/commands/speckit.*.md``) tells the
calling agent to run one of a few fixed bash scripts shipped in
``.specify/scripts/bash/``. For Cursor / Claude Code IDE users this is
trivial — the IDE executes bash. But the Feishu bots have no generic
``run_shell`` tool (by design: the PM bot must not be able to exec
arbitrary commands). So without this service, the Feishu PM bot reads
the ``speckit.specify`` markdown, sees "run create-new-feature.sh",
and silently skips that step — breaking feature-branch creation and
spec scaffold emission.

This service closes that gap by exposing a tiny, paranoid surface:

- Only scripts living under ``<project_root>/.specify/scripts/bash/``.
- Only a per-agent whitelist of script **names**. PM cannot run
  ``setup-plan.sh`` (TL's), TL cannot run ``create-new-feature.sh``
  (PM's — creating feature branches is a product-scope decision).
- Args are validated character-by-character (no shell metacharacters,
  no null bytes, no path-traversal segments, per-arg length cap).
- ``subprocess.run`` with ``shell=False`` — the argv list never goes
  through ``sh -c``, so even if an arg DID contain metacharacters they
  would never be interpreted.
- Hard timeout, bounded output size, structured return payload so the
  LLM can decide next steps (e.g. read the ``parsed_json`` fields to
  know what branch the script created).
- Every invocation is logged at INFO with agent, script, argv, exit
  code, and elapsed time — the same treatment ``GitOpsService`` gets.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SpeckitScriptError(Exception):
    """Base class. Always carries a stable ``code`` so the LLM can
    decide whether to retry, surface to the user, or halt."""

    code: str = "SPECKIT_SCRIPT_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnknownProjectError(SpeckitScriptError):
    code = "UNKNOWN_PROJECT"


class ScriptNotAllowedError(SpeckitScriptError):
    """Raised when the (agent, script) pair is not in the whitelist."""

    code = "SCRIPT_NOT_ALLOWED_FOR_AGENT"


class ScriptMissingError(SpeckitScriptError):
    """Raised when the whitelisted script name does not exist on disk.

    Usually indicates a project repo is missing the ``.specify/`` tree
    altogether (i.e. the project never ran ``speckit init``).
    """

    code = "SCRIPT_NOT_FOUND"


class ScriptArgRejectedError(SpeckitScriptError):
    """Raised when an argv element fails validation."""

    code = "SCRIPT_ARG_REJECTED"


class ScriptTimeoutError(SpeckitScriptError):
    """Raised when the subprocess exceeds the configured timeout."""

    code = "SCRIPT_TIMEOUT"


class ScriptRuntimeError(SpeckitScriptError):
    """Raised when the subprocess errors out in an unexpected way
    (binary missing, OS error, non-whitelisted script failure mode)."""

    code = "SCRIPT_RUNTIME_ERROR"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


# Per-agent script whitelist. Extend this table — NOT the runtime — when
# wiring new roles or new scripts. Any agent name not listed here is
# rejected before we even touch the filesystem.
_ALLOWED_SCRIPTS_BY_AGENT: dict[str, frozenset[str]] = {
    "product_manager": frozenset(
        {
            # Creates specs/NNN-slug/, cuts the feature branch, drops
            # the spec.md template. Entry point for speckit.specify.
            "create-new-feature.sh",
            # Read-only prerequisite check — safe for every role.
            "check-prerequisites.sh",
        }
    ),
    "tech_lead": frozenset(
        {
            # speckit.plan setup: returns FEATURE_SPEC / IMPL_PLAN /
            # SPECS_DIR / BRANCH JSON.
            "setup-plan.sh",
            # speckit.plan Phase-1: updates agent-specific context file.
            "update-agent-context.sh",
            # Read-only prerequisite check.
            "check-prerequisites.sh",
        }
    ),
}


# Each argv element must match this. Intentionally conservative: ASCII
# word chars, spaces, a handful of punctuation we KNOW the scripts accept
# (dashes, slashes for path-ish args, colons for timestamps, periods,
# commas, underscores, apostrophes for "don't", question/exclamation
# marks for feature descriptions). No shell metacharacters, no backticks,
# no ``$``, no command substitution runs. Also no newlines — argv values
# are one line. Max 500 bytes per arg; total argv count capped below.
_ARG_SAFE_RE = re.compile(
    r"^[\w\-./:, ?!'\u2019\u2014\u2013\u4e00-\u9fff]*$"
)
_ARG_MAX_BYTES = 500
_MAX_ARGV_COUNT = 16
_DEFAULT_TIMEOUT_SECONDS = 60
_MAX_STDOUT_BYTES = 64 * 1024
_MAX_STDERR_BYTES = 32 * 1024


@dataclass(frozen=True)
class SpeckitScriptResult:
    """Structured outcome of a single script invocation."""

    script: str
    argv: tuple[str, ...]
    exit_code: int
    success: bool
    stdout: str
    stderr: str
    parsed_json: dict[str, Any] | None
    elapsed_ms: int


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class SpeckitScriptService:
    """Run a whitelisted ``.specify/scripts/bash/*.sh`` on behalf of
    a role.

    Construct with a ``project_roots`` mapping identical in shape to
    the one used by ``WorkflowService`` / ``GitOpsService``. Both
    ``run_script`` and ``allowed_scripts_for_agent`` are read-only
    from the registry perspective — this service never mutates its
    own state, so one instance is safe to share across bot threads.
    """

    def __init__(
        self,
        *,
        project_roots: Mapping[str, Path],
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._project_roots = {
            pid: root.resolve() for pid, root in project_roots.items()
        }
        self._timeout_seconds = timeout_seconds

    # -- discovery ------------------------------------------------------

    def allowed_scripts_for_agent(self, agent_name: str) -> tuple[str, ...]:
        return tuple(sorted(_ALLOWED_SCRIPTS_BY_AGENT.get(agent_name, frozenset())))

    # -- execute --------------------------------------------------------

    def run_script(
        self,
        *,
        agent_name: str,
        project_id: str,
        script: str,
        args: list[str] | None = None,
    ) -> SpeckitScriptResult:
        argv_extra = list(args or [])
        self._assert_allowed(agent_name, script)
        self._assert_argv(argv_extra)

        root = self._project_roots.get(project_id)
        if root is None:
            raise UnknownProjectError(
                f"No project_root configured for project_id={project_id!r}. "
                f"Known projects: {sorted(self._project_roots)}"
            )

        script_path = (root / ".specify" / "scripts" / "bash" / script).resolve()
        # Containment check — script_path MUST stay under project_root.
        # Without it an agent could pass a cleverly constructed
        # ``script`` value that traverses out (e.g. we already filter
        # ``..`` in args but a future whitelist typo could still let
        # one slip; this is the belt-and-suspenders guard).
        try:
            script_path.relative_to(root)
        except ValueError as exc:  # pragma: no cover - defensive
            raise ScriptNotAllowedError(
                f"Resolved script path escapes project root: {script_path}"
            ) from exc

        if not script_path.is_file():
            raise ScriptMissingError(
                f"Script missing on disk: {script_path.relative_to(root)}. "
                f"Check that the project was initialized with `.specify/`."
            )

        if shutil.which("bash") is None:
            raise ScriptRuntimeError(
                "bash binary not available on PATH; cannot run speckit scripts."
            )

        cmd: list[str] = [str(script_path), *argv_extra]
        start = time.monotonic()
        logger.info(
            "speckit_script: agent=%s project=%s script=%s argv=%s",
            agent_name,
            project_id,
            script,
            argv_extra,
        )
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(root),
                capture_output=True,
                text=True,
                check=False,
                timeout=self._timeout_seconds,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.warning(
                "speckit_script TIMEOUT: agent=%s script=%s elapsed_ms=%d",
                agent_name,
                script,
                elapsed_ms,
            )
            raise ScriptTimeoutError(
                f"Script {script!r} did not finish within "
                f"{self._timeout_seconds}s."
            ) from exc
        except OSError as exc:
            raise ScriptRuntimeError(
                f"Failed to execute {script!r}: {exc}"
            ) from exc

        elapsed_ms = int((time.monotonic() - start) * 1000)
        stdout = _truncate(proc.stdout or "", _MAX_STDOUT_BYTES)
        stderr = _truncate(proc.stderr or "", _MAX_STDERR_BYTES)

        parsed_json: dict[str, Any] | None = None
        if "--json" in argv_extra and proc.returncode == 0:
            parsed_json = _try_parse_single_json_line(stdout)

        logger.info(
            "speckit_script DONE: agent=%s script=%s exit=%d elapsed_ms=%d",
            agent_name,
            script,
            proc.returncode,
            elapsed_ms,
        )

        return SpeckitScriptResult(
            script=script,
            argv=tuple(argv_extra),
            exit_code=proc.returncode,
            success=proc.returncode == 0,
            stdout=stdout,
            stderr=stderr,
            parsed_json=parsed_json,
            elapsed_ms=elapsed_ms,
        )

    # -- guards ---------------------------------------------------------

    def _assert_allowed(self, agent_name: str, script: str) -> None:
        allowed = _ALLOWED_SCRIPTS_BY_AGENT.get(agent_name)
        if not allowed:
            raise ScriptNotAllowedError(
                f"Agent {agent_name!r} has no speckit scripts allowed. "
                f"Known agents: {sorted(_ALLOWED_SCRIPTS_BY_AGENT)}"
            )
        if script not in allowed:
            raise ScriptNotAllowedError(
                f"Script {script!r} is not allowed for agent "
                f"{agent_name!r}. Allowed: {sorted(allowed)}"
            )

    def _assert_argv(self, argv: list[str]) -> None:
        if len(argv) > _MAX_ARGV_COUNT:
            raise ScriptArgRejectedError(
                f"Too many argv entries ({len(argv)} > {_MAX_ARGV_COUNT})."
            )
        for idx, arg in enumerate(argv):
            if not isinstance(arg, str):
                raise ScriptArgRejectedError(
                    f"argv[{idx}] must be a string, got {type(arg).__name__}."
                )
            if len(arg.encode("utf-8")) > _ARG_MAX_BYTES:
                raise ScriptArgRejectedError(
                    f"argv[{idx}] exceeds {_ARG_MAX_BYTES} bytes."
                )
            if "\x00" in arg or "\n" in arg or "\r" in arg:
                raise ScriptArgRejectedError(
                    f"argv[{idx}] contains NUL or newline."
                )
            if ".." in arg.split("/"):
                raise ScriptArgRejectedError(
                    f"argv[{idx}] contains path-traversal segment '..'."
                )
            if not _ARG_SAFE_RE.match(arg):
                # Surface the first offending char so operators can
                # debug without re-running with logging bumped up.
                bad = next(
                    (c for c in arg if not _ARG_SAFE_RE.match(c)),
                    "?",
                )
                raise ScriptArgRejectedError(
                    f"argv[{idx}] contains disallowed character {bad!r}; "
                    "only word chars, spaces, and a small punctuation set "
                    "are accepted."
                )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _truncate(s: str, limit_bytes: int) -> str:
    """Truncate s so its UTF-8 encoding stays under limit_bytes.

    We care about byte length, not codepoint length, because downstream
    logs and tool payloads get measured in bytes. Cut at a codepoint
    boundary to avoid producing invalid UTF-8.
    """
    encoded = s.encode("utf-8")
    if len(encoded) <= limit_bytes:
        return s
    trimmed = encoded[:limit_bytes].decode("utf-8", errors="ignore")
    return trimmed + "\n…[truncated]"


def _try_parse_single_json_line(stdout: str) -> dict[str, Any] | None:
    """Extract the first JSON object emitted on stdout by --json scripts.

    ``create-new-feature.sh`` emits one JSON object on stdout and
    informational lines on stderr. We iterate stdout lines and pick
    the first one that parses as a JSON object. Unparseable stdout
    returns None; callers treat that as "script succeeded but gave us
    no structured output" — they can still inspect ``stdout``.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None
