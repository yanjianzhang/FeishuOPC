"""Per-project deploy runner, configured from FeishuOPC side.

Design
------
Deployments are a two-party contract. The **project** supplies its own
bash entrypoint (e.g. ``deploy/deploy.sh``) — because only the project
knows how to build itself (``flutter build`` / ``docker build`` /
``rsync`` topology / remote ssh targets) and holds its own server
credentials in ``deploy/secrets/server.env``. **FeishuOPC** owns the
*metadata about how to call that script*: script path (override if the
project doesn't use the default), supported flags, default timeout,
free-form notes. That metadata lives in
``.larkagent/secrets/deploy_projects/<project_id>.json`` — see
``docs/deploy-convention.md``.

Consequences:

- Cross-project support costs **one JSON file** in FeishuOPC plus a
  working script in the project. No directory layout requirements
  on the project side, no enforced README format.
- Changing how TL thinks about flags / timeout for a project = edit
  one JSON file in FeishuOPC; no redeploy-the-project-repo dance.
- Project maintainers still own the script that actually deploys.

Security posture (unchanged from the first version):

- ``tech_lead`` is the only agent allowed to run deploys. Others get
  ``DEPLOY_NOT_ALLOWED_FOR_AGENT`` at the service boundary — not the
  tool-surface level — so a stray sub-agent that somehow obtains a
  ``DeployService`` reference still can't ship prod.
- argv is validated element-by-element (same regex as speckit scripts:
  word chars + a small punctuation set + CJK). No shell metachars, no
  newlines, no NULs, no ``..`` path traversal.
- ``subprocess.run`` is always ``shell=False``; argv is a list, never
  a string, so an escaped char wouldn't be re-parsed by a shell.
- Resolved script path must live inside the configured project root
  (symlink-escape guard).
- Per-invocation timeout is tunable but hard-capped at 1 hour. Default
  comes from the project's JSON (usually 1800s for a full exampleapp
  deploy); callers can reduce, never raise past the cap.
- Full stdout+stderr spooled to ``<app_repo_root>/.larkagent/logs/
  deploy/<project>-<ts>.log`` so an operator can ``tail -f`` during
  the run; LLM only sees the last ~8KB as ``stdout_tail``/
  ``stderr_tail`` (same buffer — we merge stderr into stdout for
  chronological readability).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DeployError(Exception):
    """Base class; every subclass carries a stable ``code`` string so the
    LLM (or an operator reading the agent log) can branch on the code
    rather than parsing a human-readable message."""

    code: str = "DEPLOY_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnknownProjectError(DeployError):
    code = "UNKNOWN_PROJECT"


class DeployNotConfiguredError(DeployError):
    """Raised when the project has no ``deploy_projects/<pid>.json``
    entry in FeishuOPC — even if a script happens to exist on disk, we
    refuse to run it without explicit metadata. Keeps "accidental
    deploys" off the table when someone drops a ``deploy/deploy.sh``
    into a repo that FeishuOPC isn't authorised to manage."""

    code = "DEPLOY_NOT_CONFIGURED"


class DeployNotAllowedError(DeployError):
    """Raised when the invoking agent is not on the deploy allowlist."""

    code = "DEPLOY_NOT_ALLOWED_FOR_AGENT"


class DeployScriptMissingError(DeployError):
    """Raised when the configured ``script_path`` does not exist inside
    the project repo. Different code from ``DEPLOY_NOT_CONFIGURED`` so
    operators can tell "metadata missing" apart from "metadata says
    script is at X but X isn't there"."""

    code = "DEPLOY_SCRIPT_MISSING"


class DeployArgRejectedError(DeployError):
    code = "DEPLOY_ARG_REJECTED"


class DeployTimeoutError(DeployError):
    code = "DEPLOY_TIMEOUT"


class DeployRuntimeError(DeployError):
    """Unexpected OS-level failure (missing bash, permission denied)."""

    code = "DEPLOY_RUNTIME_ERROR"


class DeployConfigError(DeployError):
    """Raised by the config loader for malformed
    ``deploy_projects/<pid>.json`` files. Loader errors surface at
    startup, not at tool-call time, so operators see them in the agent
    log rather than TL's Feishu thread."""

    code = "DEPLOY_CONFIG_INVALID"


# ---------------------------------------------------------------------------
# Constants / validation
# ---------------------------------------------------------------------------


# Who is allowed to deploy. Hard-coded on purpose — infrastructure
# permission, not a per-project choice.
_ALLOWED_AGENTS: frozenset[str] = frozenset({"tech_lead"})

_ARG_SAFE_RE = re.compile(
    r"^[\w\-./:=, ?!'\u2019\u2014\u2013\u4e00-\u9fff]*$"
)
_ARG_MAX_BYTES = 500
_MAX_ARGV_COUNT = 16

# Defaults if the project's JSON omits these fields. Chosen to match
# the empirical exampleapp deploy.sh (flutter build + docker build +
# rsync → 5-10 minutes); a project that deploys faster can lower its
# ``default_timeout_seconds``.
_FALLBACK_DEFAULT_TIMEOUT_SECONDS = 1800  # 30 min
_MAX_TIMEOUT_SECONDS = 3600  # 1 hour — hard cap, not configurable
_MAX_STDOUT_BYTES = 8 * 1024

# Default script path if the project's JSON omits ``script_path``.
# Keeps the happy path ergonomic for projects that adopt the
# convention without overriding.
DEFAULT_SCRIPT_RELATIVE_PATH = "deploy/deploy.sh"


# ---------------------------------------------------------------------------
# Config dataclasses (parsed from deploy_projects/<pid>.json)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeployFlagSpec:
    """One row in a project's ``supported_flags`` catalog.

    The ``flag`` string is what TL passes in ``deploy_project.args``;
    the description is free-form Chinese/English meant to be read by
    the LLM when it decides which flag fits the user's request. An
    optional ``expected_duration_seconds`` hint lets TL set a tighter
    ``timeout_seconds`` when the user picked a fast path
    (``--server-only`` / ``--web-only``) without maintaining a
    separate table.
    """

    flag: str
    description: str
    expected_duration_seconds: int | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "flag": self.flag,
            "description": self.description,
        }
        if self.expected_duration_seconds is not None:
            out["expected_duration_seconds"] = self.expected_duration_seconds
        return out


@dataclass(frozen=True)
class DeployProjectConfig:
    """FeishuOPC-side deploy metadata for one project.

    Lives at ``<app>/.larkagent/secrets/deploy_projects/<project_id>.json``
    (gitignored; committed ``<pid>.json.example`` for onboarding).
    Loaded once at TL-executor build time. Never mutated at runtime.

    Only ``project_id`` is required by the loader. Everything else has
    a default, so the minimal valid config is literally
    ``{"project_id": "foo"}`` (uses ``deploy/deploy.sh`` with a 30-min
    timeout and no documented flags).

    ``host_bootstrap_script`` is the optional path (relative to
    ``project_repo_root``) of an idempotent script that prepares the
    agent host for running ``script_path`` — installing build
    toolchains (``flutter``, ``docker``, language runtimes) and other
    things that ``deploy.sh`` will need. It is NOT run by the TL,
    and ``deploy_project`` at runtime does NOT re-run it either:
    bootstrap runs exclusively during ``agent_deploy.sh`` (the
    FeishuOPC-side deployer on the operator's laptop), once per
    ``agent_deploy.sh --all`` pass, after the project's
    ``shared-repo`` has been synced to the target host. The tool
    merely records its configured path so ``describe_deploy_project``
    can tell the LLM it exists. See ``docs/deploy-convention.md`` for
    the contract.
    """

    project_id: str
    script_path: str = DEFAULT_SCRIPT_RELATIVE_PATH
    host_bootstrap_script: str | None = None
    default_args: tuple[str, ...] = ()
    supported_flags: tuple[DeployFlagSpec, ...] = ()
    default_timeout_seconds: int = _FALLBACK_DEFAULT_TIMEOUT_SECONDS
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "script_path": self.script_path,
            "host_bootstrap_script": self.host_bootstrap_script,
            "default_args": list(self.default_args),
            "supported_flags": [f.to_dict() for f in self.supported_flags],
            "default_timeout_seconds": self.default_timeout_seconds,
            "notes": self.notes,
        }


def _parse_flag_spec(raw: Any, *, idx: int, project_id: str) -> DeployFlagSpec:
    if not isinstance(raw, dict):
        raise DeployConfigError(
            f"{project_id}: supported_flags[{idx}] must be an object, "
            f"got {type(raw).__name__}."
        )
    flag = raw.get("flag", "")
    description = raw.get("description", "")
    if not isinstance(flag, str):
        raise DeployConfigError(
            f"{project_id}: supported_flags[{idx}].flag must be a string."
        )
    if not isinstance(description, str):
        raise DeployConfigError(
            f"{project_id}: supported_flags[{idx}].description must be a string."
        )
    duration = raw.get("expected_duration_seconds")
    if duration is not None and not isinstance(duration, int):
        raise DeployConfigError(
            f"{project_id}: supported_flags[{idx}].expected_duration_seconds "
            "must be an int when present."
        )
    return DeployFlagSpec(
        flag=flag,
        description=description,
        expected_duration_seconds=duration,
    )


def parse_deploy_project_config(raw: Mapping[str, Any]) -> DeployProjectConfig:
    """Parse one project's JSON-dict into a validated config.

    Raises ``DeployConfigError`` on any shape problem. Loader callers
    should catch + log + skip that project (keep the rest loadable)
    rather than crashing the whole runtime.
    """
    pid = raw.get("project_id")
    if not isinstance(pid, str) or not pid.strip():
        raise DeployConfigError(
            "project_id is required and must be a non-empty string."
        )
    pid = pid.strip()

    script_path = _parse_relative_script_path(
        raw.get("script_path", DEFAULT_SCRIPT_RELATIVE_PATH),
        pid=pid,
        field_name="script_path",
        allow_none=False,
    )
    host_bootstrap_script = _parse_relative_script_path(
        raw.get("host_bootstrap_script"),
        pid=pid,
        field_name="host_bootstrap_script",
        allow_none=True,
    )

    default_args_raw = raw.get("default_args", [])
    if not isinstance(default_args_raw, list) or not all(
        isinstance(a, str) for a in default_args_raw
    ):
        raise DeployConfigError(
            f"{pid}: default_args must be a list of strings."
        )

    flags_raw = raw.get("supported_flags", [])
    if not isinstance(flags_raw, list):
        raise DeployConfigError(
            f"{pid}: supported_flags must be a list."
        )
    supported_flags = tuple(
        _parse_flag_spec(f, idx=i, project_id=pid)
        for i, f in enumerate(flags_raw)
    )

    timeout = raw.get("default_timeout_seconds", _FALLBACK_DEFAULT_TIMEOUT_SECONDS)
    if not isinstance(timeout, int) or timeout <= 0:
        raise DeployConfigError(
            f"{pid}: default_timeout_seconds must be a positive int."
        )
    if timeout > _MAX_TIMEOUT_SECONDS:
        # Clamp silently — matches per-call behaviour. Log it so the
        # operator notices they wrote 7200 and got 3600.
        logger.warning(
            "deploy config: %s default_timeout_seconds=%d clamped to %d",
            pid,
            timeout,
            _MAX_TIMEOUT_SECONDS,
        )
        timeout = _MAX_TIMEOUT_SECONDS

    notes = raw.get("notes", "")
    if not isinstance(notes, str):
        raise DeployConfigError(f"{pid}: notes must be a string when present.")

    return DeployProjectConfig(
        project_id=pid,
        script_path=script_path,
        host_bootstrap_script=host_bootstrap_script,
        default_args=tuple(default_args_raw),
        supported_flags=supported_flags,
        default_timeout_seconds=timeout,
        notes=notes.strip(),
    )


# Paths are interpolated into SSH command strings by agent_deploy.sh
# (``bash '<path>'``). A single quote, backtick, ``$`` or newline in
# the path breaks out of the outer single-quote wrapping and runs as
# remote shell. So we allowlist a boring subset that can't do that —
# letters, digits, dot, underscore, slash, hyphen. This matches the
# posture the existing flag validator uses on ``deploy_project``
# argv. Intentionally stricter than POSIX "valid filename"; the cost
# is rejecting weird but legal paths like ``deploy/build v2.sh`` —
# in exchange we get a validator that can't be argued out of.
_SAFE_RELATIVE_PATH = re.compile(r"^[A-Za-z0-9._/-]+$")


def _parse_relative_script_path(
    raw: Any,
    *,
    pid: str,
    field_name: str,
    allow_none: bool,
) -> str | None:
    """Shared validator for ``script_path`` / ``host_bootstrap_script``.

    Both fields have the same shape: a non-empty relative path inside
    ``project_repo_root``, no absolute paths, no parent-escape, and
    only characters that can survive shell interpolation without
    metachar breakout. The only difference between the two is whether
    ``None`` / omission is acceptable — mandatory ``script_path`` vs.
    optional ``host_bootstrap_script``.

    Kept as a helper rather than a regex so the error messages can
    name the specific field (operators reading the agent log need to
    know *which* field of *which* project's JSON is malformed, not
    just "some path is bad").
    """
    if raw is None:
        if allow_none:
            return None
        raise DeployConfigError(
            f"{pid}: {field_name} is required."
        )
    if not isinstance(raw, str) or not raw.strip():
        raise DeployConfigError(
            f"{pid}: {field_name} must be a non-empty string when present."
        )
    trimmed = raw.strip()
    # Containment is the whole point of keeping the path relative —
    # reject absolute paths / ``..`` segments at parse time so nobody
    # ships a config like ``"script_path": "/etc/passwd"``.
    if trimmed.startswith("/"):
        raise DeployConfigError(
            f"{pid}: {field_name} must be relative to project_repo_root, "
            f"got absolute path {trimmed!r}."
        )
    if ".." in Path(trimmed).parts:
        raise DeployConfigError(
            f"{pid}: {field_name} must not contain '..' segments."
        )
    if not _SAFE_RELATIVE_PATH.match(trimmed):
        raise DeployConfigError(
            f"{pid}: {field_name} must match {_SAFE_RELATIVE_PATH.pattern} "
            f"(letters, digits, dot, underscore, slash, hyphen). "
            f"Got {trimmed!r}."
        )
    return trimmed


def load_deploy_project_configs(
    configs_dir: Path,
) -> dict[str, DeployProjectConfig]:
    """Load all ``<pid>.json`` files from ``configs_dir``.

    Ignores:
    - ``*.example.json`` — committed templates for onboarding
    - Files starting with ``.`` — editor turds, OS junk
    - Any non-``.json`` file

    Returns ``{project_id: config}``. Bad files are logged (WARN) and
    skipped so one malformed file doesn't knock the rest out.
    """
    out: dict[str, DeployProjectConfig] = {}
    if not configs_dir.is_dir():
        return out

    for path in sorted(configs_dir.iterdir()):
        if not path.is_file():
            continue
        name = path.name
        if name.startswith("."):
            continue
        if not name.endswith(".json"):
            continue
        if name.endswith(".example.json"):
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "deploy_projects: skipping %s — invalid JSON: %s",
                path.name,
                exc,
            )
            continue
        if not isinstance(raw, dict):
            logger.warning(
                "deploy_projects: skipping %s — top level must be an object",
                path.name,
            )
            continue
        try:
            cfg = parse_deploy_project_config(raw)
        except DeployConfigError as exc:
            logger.warning(
                "deploy_projects: skipping %s — %s", path.name, exc
            )
            continue
        if cfg.project_id in out:
            logger.warning(
                "deploy_projects: duplicate project_id=%s (%s overrides earlier)",
                cfg.project_id,
                path.name,
            )
        out[cfg.project_id] = cfg

    return out


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeployResult:
    """Structured outcome of a single deploy invocation."""

    project_id: str
    argv: tuple[str, ...]
    exit_code: int
    success: bool
    stdout_tail: str
    stderr_tail: str
    elapsed_ms: int
    log_path: str
    command: str
    script_path: str


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class DeployService:
    """Run a project's deploy script on behalf of the TL.

    Construct with:
    - ``project_roots``: map of ``project_id → Path`` pointing at the
      project's checkout on the agent host.
    - ``configs``: map of ``project_id → DeployProjectConfig`` loaded
      from ``deploy_projects/<pid>.json``. Projects with NO config are
      simply not deployable — this is how operators gate deployment on
      a per-project basis without touching code.
    - ``log_dir``: where full run logs are spooled.

    The intersection of ``project_roots`` and ``configs`` is the set of
    deployable projects. ``is_deployable(pid)`` surfaces the check so
    the TL executor can hide / show tools accordingly.
    """

    def __init__(
        self,
        *,
        project_roots: Mapping[str, Path],
        configs: Mapping[str, DeployProjectConfig],
        log_dir: Path,
    ) -> None:
        self._project_roots = {
            pid: root.resolve() for pid, root in project_roots.items()
        }
        self._configs = dict(configs)
        self._log_dir = log_dir

    # -- introspection --------------------------------------------------

    def known_projects(self) -> tuple[str, ...]:
        """Projects that have BOTH a real repo root AND a config."""
        return tuple(
            sorted(set(self._project_roots) & set(self._configs))
        )

    def has_config(self, project_id: str) -> bool:
        return project_id in self._configs

    def get_config(self, project_id: str) -> DeployProjectConfig | None:
        return self._configs.get(project_id)

    def is_agent_allowed(self, agent_name: str) -> bool:
        return agent_name in _ALLOWED_AGENTS

    def deploy_script_path(self, project_id: str) -> Path | None:
        """Absolute resolved path to the project's deploy script, or
        ``None`` when the project is unknown / has no config. Does NOT
        check on-disk existence — use ``is_deployable`` for that."""
        cfg = self._configs.get(project_id)
        root = self._project_roots.get(project_id)
        if cfg is None or root is None:
            return None
        return (root / cfg.script_path).resolve()

    def is_deployable(self, project_id: str) -> bool:
        """True iff project has: (1) a config, (2) a project_root,
        (3) the configured script exists on disk and is inside the
        project root.

        TL's ``_deploy_tool_available`` uses this to decide whether to
        surface ``deploy_project`` / ``describe_deploy_project``.
        """
        cfg = self._configs.get(project_id)
        root = self._project_roots.get(project_id)
        if cfg is None or root is None:
            return False
        script = (root / cfg.script_path).resolve()
        try:
            script.relative_to(root)
        except ValueError:
            return False
        return script.is_file()

    def describe(self, project_id: str) -> dict[str, Any]:
        """Return the metadata a caller should show to the LLM so it
        can choose a flag. Shape:

        ``{project_id, script_path, script_absolute, script_exists,
           default_args, supported_flags, default_timeout_seconds,
           max_timeout_seconds, notes}``

        Raises ``UnknownProjectError`` / ``DeployNotConfiguredError``
        so the tool call returns a structured error instead of
        succeeding with misleading defaults.
        """
        root = self._project_roots.get(project_id)
        if root is None:
            raise UnknownProjectError(
                f"No project_root configured for project_id={project_id!r}."
            )
        cfg = self._configs.get(project_id)
        if cfg is None:
            raise DeployNotConfiguredError(
                f"No FeishuOPC deploy config for project_id={project_id!r}. "
                "Expected a file at "
                f".larkagent/secrets/deploy_projects/{project_id}.json "
                "(see docs/deploy-convention.md)."
            )
        script_abs = (root / cfg.script_path).resolve()
        info: dict[str, Any] = {
            **cfg.to_dict(),
            "script_absolute": str(script_abs),
            "script_exists": script_abs.is_file(),
            "max_timeout_seconds": _MAX_TIMEOUT_SECONDS,
        }
        if cfg.host_bootstrap_script is not None:
            bootstrap_abs = (root / cfg.host_bootstrap_script).resolve()
            info["host_bootstrap_script_absolute"] = str(bootstrap_abs)
            info["host_bootstrap_script_exists"] = bootstrap_abs.is_file()
        else:
            info["host_bootstrap_script_absolute"] = None
            info["host_bootstrap_script_exists"] = False
        return info

    # -- execute --------------------------------------------------------

    def run(
        self,
        *,
        agent_name: str,
        project_id: str,
        args: list[str] | None = None,
        timeout_seconds: int | None = None,
    ) -> DeployResult:
        argv_extra = list(args or [])
        self._assert_allowed(agent_name)
        self._assert_argv(argv_extra)

        root = self._project_roots.get(project_id)
        if root is None:
            raise UnknownProjectError(
                f"No project_root configured for project_id={project_id!r}. "
                f"Known projects: {sorted(self._project_roots)}"
            )
        cfg = self._configs.get(project_id)
        if cfg is None:
            raise DeployNotConfiguredError(
                f"No deploy config for project_id={project_id!r}. "
                "See docs/deploy-convention.md."
            )

        script_path = (root / cfg.script_path).resolve()
        try:
            script_path.relative_to(root)
        except ValueError as exc:  # pragma: no cover - defensive
            raise DeployScriptMissingError(
                f"Resolved script_path escapes project_root: {script_path}"
            ) from exc

        if not script_path.is_file():
            raise DeployScriptMissingError(
                f"Deploy script missing: {cfg.script_path} "
                f"(resolved to {script_path}). Update "
                f"deploy_projects/{project_id}.json::script_path or "
                "commit the script."
            )

        if shutil.which("bash") is None:
            raise DeployRuntimeError(
                "bash binary not available on PATH; cannot run deploy script."
            )

        effective_timeout = self._resolve_timeout(
            requested=timeout_seconds,
            config_default=cfg.default_timeout_seconds,
        )

        # Merge default_args (config) with runtime args. Default args
        # come FIRST so runtime args can override/append at the end of
        # the argv — consistent with how most shells treat argument
        # order.
        merged_argv = list(cfg.default_args) + argv_extra
        # Re-validate merged argv (default_args might contain chars
        # that snuck past manual review; defense in depth).
        self._assert_argv(merged_argv)

        log_path = self._log_path_for(project_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd: list[str] = [str(script_path), *merged_argv]
        command_preview = " ".join(cmd)

        start = time.monotonic()
        logger.info(
            "deploy: agent=%s project=%s argv=%s timeout=%ds log=%s",
            agent_name,
            project_id,
            merged_argv,
            effective_timeout,
            log_path,
        )

        with log_path.open("w", encoding="utf-8", errors="replace") as log_fh:
            log_fh.write(
                f"# deploy_project project={project_id} agent={agent_name}\n"
                f"# script_path={cfg.script_path}\n"
                f"# argv={merged_argv!r}\n"
                f"# cwd={root}\n"
                f"# started={_iso_now()}\n"
                f"# command={command_preview}\n"
                f"# --- script output below ---\n"
            )
            log_fh.flush()
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(root),
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    check=False,
                    timeout=effective_timeout,
                    shell=False,
                )
            except subprocess.TimeoutExpired as exc:
                elapsed_ms = int((time.monotonic() - start) * 1000)
                log_fh.write(
                    f"\n# --- TIMEOUT after {effective_timeout}s ---\n"
                )
                logger.warning(
                    "deploy TIMEOUT: agent=%s project=%s elapsed_ms=%d log=%s",
                    agent_name,
                    project_id,
                    elapsed_ms,
                    log_path,
                )
                raise DeployTimeoutError(
                    f"deploy.sh for {project_id!r} did not finish within "
                    f"{effective_timeout}s. Full log: {log_path}"
                ) from exc
            except OSError as exc:
                raise DeployRuntimeError(
                    f"Failed to execute deploy script for {project_id!r}: {exc}"
                ) from exc

            log_fh.write(
                f"\n# --- exited={proc.returncode} elapsed={_iso_now()} ---\n"
            )

        elapsed_ms = int((time.monotonic() - start) * 1000)
        stdout_tail, stderr_tail = _read_tail(log_path)

        logger.info(
            "deploy DONE: agent=%s project=%s exit=%d elapsed_ms=%d log=%s",
            agent_name,
            project_id,
            proc.returncode,
            elapsed_ms,
            log_path,
        )

        return DeployResult(
            project_id=project_id,
            argv=tuple(merged_argv),
            exit_code=proc.returncode,
            success=proc.returncode == 0,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            elapsed_ms=elapsed_ms,
            log_path=str(log_path),
            command=command_preview,
            script_path=cfg.script_path,
        )

    # -- guards ---------------------------------------------------------

    def _assert_allowed(self, agent_name: str) -> None:
        if agent_name not in _ALLOWED_AGENTS:
            raise DeployNotAllowedError(
                f"Agent {agent_name!r} is not allowed to deploy. "
                f"Only {sorted(_ALLOWED_AGENTS)} may invoke deploy_project."
            )

    def _assert_argv(self, argv: list[str]) -> None:
        if len(argv) > _MAX_ARGV_COUNT:
            raise DeployArgRejectedError(
                f"Too many argv entries ({len(argv)} > {_MAX_ARGV_COUNT})."
            )
        for idx, arg in enumerate(argv):
            if not isinstance(arg, str):
                raise DeployArgRejectedError(
                    f"argv[{idx}] must be a string, got {type(arg).__name__}."
                )
            if len(arg.encode("utf-8")) > _ARG_MAX_BYTES:
                raise DeployArgRejectedError(
                    f"argv[{idx}] exceeds {_ARG_MAX_BYTES} bytes."
                )
            if "\x00" in arg or "\n" in arg or "\r" in arg:
                raise DeployArgRejectedError(
                    f"argv[{idx}] contains NUL or newline."
                )
            if ".." in arg.split("/"):
                raise DeployArgRejectedError(
                    f"argv[{idx}] contains path-traversal segment '..'."
                )
            if not _ARG_SAFE_RE.match(arg):
                bad = next(
                    (c for c in arg if not _ARG_SAFE_RE.match(c)),
                    "?",
                )
                raise DeployArgRejectedError(
                    f"argv[{idx}] contains disallowed character {bad!r}."
                )

    def _resolve_timeout(
        self, *, requested: int | None, config_default: int
    ) -> int:
        if requested is None:
            base = config_default
        elif requested <= 0:
            raise DeployArgRejectedError(
                f"timeout_seconds must be positive, got {requested}."
            )
        else:
            base = requested
        return min(base, _MAX_TIMEOUT_SECONDS)

    def _log_path_for(self, project_id: str) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_project = re.sub(r"[^A-Za-z0-9_.-]+", "_", project_id) or "project"
        return self._log_dir / f"{safe_project}-{ts}.log"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_tail(log_path: Path) -> tuple[str, str]:
    """Read the tail of the combined log file.

    stderr was merged into stdout at subprocess time (for chronological
    readability in ``tail -f``), so we return the same tail twice —
    once as ``stdout_tail``, once as ``stderr_tail``. The historical
    return signature is preserved for callers that distinguish them,
    but there is no real split.
    """
    try:
        raw = log_path.read_bytes()
    except OSError:
        marker = f"[log unreadable: {log_path}]"
        return marker, marker

    tail_bytes = raw[-_MAX_STDOUT_BYTES:]
    text = tail_bytes.decode("utf-8", errors="replace")
    if len(raw) > _MAX_STDOUT_BYTES:
        text = "…[truncated — see full log]\n" + text
    return text, text
