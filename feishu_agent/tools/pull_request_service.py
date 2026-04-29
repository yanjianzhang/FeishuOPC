"""GitHub pull request creation via `gh` CLI.

Tech-lead's job isn't done when code is pushed — it's done when a PR
is open and the URL is in the Feishu thread so the human owner can
review and merge. This service wraps ``gh pr create`` so the agent
can deliver that URL directly.

## Auth

``gh`` needs to be logged in on the machine running FeishuOPC. Two
ways:

1. ``gh auth login`` — interactive, sets up ``~/.config/gh``.
2. ``GH_TOKEN`` / ``GITHUB_TOKEN`` env var — preferred for automation
   / remote deploys. Scope must include ``repo``.

This service does NOT manage the auth itself; if ``gh`` fails we
surface the error to the LLM verbatim so the human can read it.

## Docs-change heuristic

Before invoking ``gh``, we diff the feature branch against ``base``
and check whether any path under ``docs/`` changed. If not, we
prepend a warning line to the PR body — but we do NOT block the PR.
Tech-lead / human can still decide the change genuinely needs no doc
update (e.g. a pure test-only change).
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from feishu_agent.tools.code_write_service import (
    CodeWriteAuditLog,
    CodeWritePolicy,
)


def load_gh_token_from_env_file(path: Path) -> str | None:
    """Read a token from a one-line env file.

    Accepts three common layouts so operators don't have to think:

    - ``ghp_xxx`` — bare token on a single line
    - ``GH_TOKEN=ghp_xxx`` — shell-style assignment
    - ``export GH_TOKEN=ghp_xxx`` — sourceable shell snippet

    Returns ``None`` if the file is missing, empty, or can't be parsed
    as any of those forms. Never raises — auth failures surface later
    when ``gh`` actually gets invoked.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            if key not in ("GH_TOKEN", "GITHUB_TOKEN"):
                continue
            value = value.strip().strip('"').strip("'")
            if value:
                return value
            continue
        # bare token line (no '=')
        candidate = line.strip().strip('"').strip("'")
        if candidate:
            return candidate
    return None


def build_gh_env(
    gh_token_path: Path | None = None,
) -> dict[str, str] | None:
    """Build an env dict for a ``gh`` subprocess.

    Returns ``None`` when we should inherit the caller's env as-is (no
    token in the file, none in parent env — let ``gh``'s own keyring
    login handle auth). Returns an env dict when we want to overlay a
    ``GH_TOKEN`` from the configured env file.

    Shared by ``PullRequestService`` and ``CIWatchService`` so both use
    the exact same auth resolution; having two subtly different copies
    was how an earlier "PR creates fine but CI watch hits 401" incident
    happened."""
    base = os.environ.copy()
    if gh_token_path is not None:
        token = load_gh_token_from_env_file(gh_token_path)
        if token:
            base["GH_TOKEN"] = token
            base.setdefault("GITHUB_TOKEN", token)
            return base
    if "GH_TOKEN" in base or "GITHUB_TOKEN" in base:
        return base
    return None


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PullRequestError(Exception):
    """Base error type for PR creation; carries a stable ``code``."""

    code: str = "PR_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PullRequestProjectError(PullRequestError):
    code = "PR_UNKNOWN_PROJECT"


class PullRequestBranchProtectedError(PullRequestError):
    code = "PR_BRANCH_PROTECTED"


class PullRequestCommandError(PullRequestError):
    code = "PR_COMMAND_FAILED"


class PullRequestNotPushedError(PullRequestError):
    code = "PR_BRANCH_NOT_PUSHED"


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class PullRequestResult:
    project_id: str
    branch: str
    base: str
    title: str
    body: str
    url: str
    number: int | None
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "branch": self.branch,
            "base": self.base,
            "title": self.title,
            "body": self.body,
            "url": self.url,
            "number": self.number,
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PullRequestService:
    TITLE_MAX_BYTES = 256
    BODY_MAX_BYTES = 64 * 1024
    TIMEOUT_SECONDS = 60

    # Matches "https://github.com/.../pull/123" etc.
    _URL_RE = re.compile(r"https?://\S+/pull/\d+")

    def __init__(
        self,
        *,
        project_roots: dict[str, Path],
        policies: dict[str, CodeWritePolicy],
        audit_log: CodeWriteAuditLog | None = None,
        gh_binary: str = "gh",
        git_binary: str = "git",
        default_base: str = "main",
        default_remote: str = "origin",
        gh_token_path: Path | None = None,
    ) -> None:
        self._project_roots = {k: Path(v) for k, v in project_roots.items()}
        self._policies = dict(policies)
        self._audit = audit_log
        self._gh = gh_binary
        self._git = git_binary
        self._default_base = default_base
        self._default_remote = default_remote
        self._gh_token_path = gh_token_path

    def _gh_env(self) -> dict[str, str] | None:
        """Return the env dict for invoking ``gh``. Delegates to the
        module-level :func:`build_gh_env` so ``PullRequestService`` and
        ``CIWatchService`` share identical auth resolution."""
        return build_gh_env(self._gh_token_path)

    # -- public API -----------------------------------------------------

    def create_pull_request(
        self,
        *,
        project_id: str,
        title: str,
        body: str,
        base: str | None = None,
    ) -> PullRequestResult:
        root, pol = self._resolve(project_id)
        base = (base or self._default_base).strip() or self._default_base

        self._validate_text(title, "title", self.TITLE_MAX_BYTES)
        self._validate_text(body, "body", self.BODY_MAX_BYTES)

        branch = self._current_branch(root)
        if pol.is_protected_branch(branch):
            raise PullRequestBranchProtectedError(
                f"Refusing to open a PR from protected branch {branch!r}. "
                f"A PR must come from a feature branch."
            )
        if base == branch:
            raise PullRequestBranchProtectedError(
                f"PR base == head ({branch!r}); PRs must target a different branch."
            )

        # Confirm the feature branch has an upstream on origin; gh will
        # otherwise error with a less-helpful message.
        try:
            self._git_out(
                root, ["rev-parse", "--verify", f"{self._default_remote}/{branch}"]
            )
        except PullRequestCommandError as exc:
            raise PullRequestNotPushedError(
                f"Remote {self._default_remote}/{branch} not found. "
                f"Push the branch first (git_push) before opening a PR."
            ) from exc

        # Docs-change heuristic.
        warnings: list[str] = []
        effective_body = body
        if not self._diff_touches_docs(root, base=base, head=branch):
            warning_line = (
                "⚠️ No `docs/` changes detected in this PR — if this change "
                "is user-visible or changes public behavior, consider adding "
                "a changelog / design-doc update before merging."
            )
            warnings.append(warning_line)
            effective_body = f"{warning_line}\n\n{body}"

        url = self._gh_pr_create(
            root=root,
            title=title,
            body=effective_body,
            base=base,
            head=branch,
        )
        number = self._extract_pr_number(url)

        self._audit_append(
            {
                "event": "pr_create",
                "project_id": project_id,
                "branch": branch,
                "base": base,
                "url": url,
                "number": number,
            }
        )
        return PullRequestResult(
            project_id=project_id,
            branch=branch,
            base=base,
            title=title,
            body=effective_body,
            url=url,
            number=number,
            warnings=warnings,
        )

    # -- internal -------------------------------------------------------

    def _resolve(self, project_id: str) -> tuple[Path, CodeWritePolicy]:
        root = self._project_roots.get(project_id)
        pol = self._policies.get(project_id)
        if root is None or pol is None:
            raise PullRequestProjectError(
                f"No PR config for project_id={project_id!r}."
            )
        if not (root / ".git").exists():
            raise PullRequestCommandError(f"{root} is not a git checkout.")
        return root, pol

    def _validate_text(self, value: str, field: str, limit: int) -> None:
        if not value or not value.strip():
            raise PullRequestError(f"PR {field} must be non-empty.")
        if len(value.encode("utf-8")) > limit:
            raise PullRequestError(
                f"PR {field} too long: > {limit} bytes."
            )

    def _current_branch(self, root: Path) -> str:
        branch = self._git_out(
            root, ["rev-parse", "--abbrev-ref", "HEAD"]
        ).strip()
        if not branch or branch == "HEAD":
            raise PullRequestBranchProtectedError(
                "Current HEAD is detached; cannot open a PR."
            )
        return branch

    def _diff_touches_docs(self, root: Path, *, base: str, head: str) -> bool:
        """Return True if any file path changed between base and head
        starts with ``docs/``.

        Compares against ``origin/<base>`` so the check is meaningful
        even when the local ``base`` branch is stale. If neither ref
        resolves, conservatively return True (i.e. don't false-positive
        the warning when we can't actually tell)."""
        candidates = [
            f"{self._default_remote}/{base}...HEAD",
            f"{base}...HEAD",
        ]
        for spec in candidates:
            try:
                out = self._git_out(
                    root, ["diff", "--name-only", spec]
                )
                return any(
                    line.strip().startswith("docs/")
                    for line in out.splitlines()
                    if line.strip()
                )
            except PullRequestCommandError:
                continue
        logger.info("could not compute docs-diff for %s..HEAD; suppressing warning", base)
        return True  # can't tell → don't emit false warning

    def _gh_pr_create(
        self,
        *,
        root: Path,
        title: str,
        body: str,
        base: str,
        head: str,
    ) -> str:
        args = [
            self._gh,
            "pr",
            "create",
            "--title",
            title,
            "--body",
            body,
            "--base",
            base,
            "--head",
            head,
        ]
        try:
            res = subprocess.run(
                args,
                cwd=str(root),
                check=False,
                capture_output=True,
                text=True,
                timeout=self.TIMEOUT_SECONDS,
                env=self._gh_env(),
            )
        except FileNotFoundError as exc:
            raise PullRequestCommandError(
                f"`{self._gh}` binary not found. Install GitHub CLI "
                f"(https://cli.github.com) and run `gh auth login`, or "
                f"put a token at "
                f".larkagent/secrets/github_key/gh_token.env."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise PullRequestCommandError(
                f"gh pr create timed out after {self.TIMEOUT_SECONDS}s."
            ) from exc

        if res.returncode != 0:
            raise PullRequestCommandError(
                f"gh pr create failed (rc={res.returncode}): "
                f"{(res.stderr or res.stdout).strip()}"
            )

        # gh prints the new PR URL on stdout (sometimes prefixed with
        # "Creating pull request..." on stderr). Prefer the first
        # stdout line that matches our URL regex.
        stdout = (res.stdout or "").strip()
        stderr = (res.stderr or "").strip()
        combined = "\n".join(filter(None, [stdout, stderr]))
        match = self._URL_RE.search(combined)
        if not match:
            raise PullRequestCommandError(
                f"gh pr create succeeded (rc=0) but no PR URL was "
                f"extracted from output: {combined!r}"
            )
        return match.group(0)

    def _extract_pr_number(self, url: str) -> int | None:
        m = re.search(r"/pull/(\d+)", url)
        if not m:
            return None
        try:
            return int(m.group(1))
        except ValueError:
            return None

    def _audit_append(self, record: dict[str, Any]) -> None:
        if self._audit is None:
            return
        record = {"ts": time.time(), **record}
        try:
            self._audit.append(record)
        except Exception:
            logger.warning("pr-service audit append failed", exc_info=True)

    def _git_out(self, root: Path, args: list[str]) -> str:
        try:
            res = subprocess.run(
                [self._git, *args],
                cwd=str(root),
                check=False,
                capture_output=True,
                text=True,
                timeout=self.TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            raise PullRequestCommandError(
                f"git binary not found: {self._git}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise PullRequestCommandError(
                f"git {shlex.join(args)} timed out after {self.TIMEOUT_SECONDS}s"
            ) from exc
        if res.returncode != 0:
            raise PullRequestCommandError(
                f"git {shlex.join(args)} failed (rc={res.returncode}): "
                f"{res.stderr.strip()}"
            )
        return res.stdout
