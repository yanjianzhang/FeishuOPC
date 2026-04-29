"""GitHub Actions watch gate for the tech-lead agent.

Tech-lead opens a PR via :class:`PullRequestService`, then must verify
the PR is actually mergeable before declaring delivery complete. Up
until this service existed, TL would report "PR opened, 待 merge" the
moment ``gh pr create`` returned — and if CI failed (e.g. TypeScript
errors that passed local review but failed ``tsc --noEmit``) the user
only learned about it hours later.

This service wraps ``gh pr checks <n> --watch --fail-fast`` so the
tech-lead can block on CI:

- **success** — every required check passed; safe to merge
- **failure** — at least one check failed, with failing-job details so
  the LLM can route the failure to ``bug_fixer``
- **timeout** — CI took longer than the caller's budget; TL escalates
  to the human instead of silently hanging
- **unavailable** — ``gh`` missing / not authenticated; TL falls back
  to "PR opened, please check CI manually" — it never auto-declares
  success in this state

On failure, the service also runs a follow-up ``gh pr checks <n>
--json ...`` call to fetch structured failing-job data (name, workflow,
link) so the LLM can quote them verbatim to ``bug_fixer``.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from feishu_agent.tools.pull_request_service import build_gh_env

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CIWatchError(Exception):
    """Base error type; carries a stable ``code`` for LLM branching."""

    code: str = "CI_WATCH_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class CIWatchProjectError(CIWatchError):
    code = "CI_WATCH_UNKNOWN_PROJECT"


# NOTE: we intentionally do NOT expose a ``CIWatchGhMissingError`` here.
# "gh not installed" and "gh not authenticated" are environmental
# degradations, not programming errors, so the service returns
# ``CIWatchResult(status="unavailable", ...)`` for both — matching the
# contract promised in the tool-spec description and skills/tech_lead.md.


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FailingJob:
    name: str
    """Check name as reported by GitHub, e.g. ``miniapp (typecheck)``."""

    workflow: str
    """GitHub Actions workflow filename, e.g. ``miniapp.yml``."""

    state: str
    """Raw state field: ``failure`` / ``cancelled`` / ``timed_out`` / ...
    Kept verbatim from the API so the LLM can reason about the exact
    failure mode."""

    link: str
    """URL to the failing run; the operator (or bug_fixer) follows this
    for detailed logs."""

    description: str
    """Short status description returned by the API; can be empty."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "workflow": self.workflow,
            "state": self.state,
            "link": self.link,
            "description": self.description,
        }


@dataclass
class CIWatchResult:
    status: str
    """One of ``success`` / ``failure`` / ``timeout`` / ``unavailable``."""

    pr_number: int
    failing_jobs: list[FailingJob]
    summary: str
    watched_seconds: float
    reason: str | None = None
    """Populated for ``unavailable`` / ``timeout`` to explain WHY we could
    not declare success."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "pr_number": self.pr_number,
            "failing_jobs": [j.to_dict() for j in self.failing_jobs],
            "summary": self.summary,
            "watched_seconds": self.watched_seconds,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class CIWatchService:
    """Wrap ``gh pr checks`` to turn GitHub Actions status into a
    structured gate result the tech-lead LLM can branch on.

    The service is read-only: it never mutates PR state, never closes
    PRs, never re-runs failed jobs. Those actions require explicit
    ``gh`` calls that belong to the tech-lead's own dispatched
    ``bug_fixer`` flow.
    """

    # gh pr checks --fail-fast exits with these codes; see `gh help exit-codes`.
    _GH_EXIT_FAILURE: int = 1
    _GH_EXIT_PENDING: int = 8

    DEFAULT_TIMEOUT_SECONDS: int = 600
    MAX_TIMEOUT_SECONDS: int = 30 * 60  # 30min; past this, escalate instead
    MIN_TIMEOUT_SECONDS: int = 30
    DEFAULT_POLL_INTERVAL: int = 15

    _JSON_FIELDS: str = "name,state,bucket,link,workflow,description"

    # Substrings that indicate `gh` ran but we weren't authenticated, so
    # the result is ``unavailable`` (environmental) rather than
    # ``failure`` (the PR's own CI). Keeping this narrow: we only match
    # verbatim error phrases gh itself emits for auth failures, so we
    # never accidentally swallow a genuine CI failure whose logs happen
    # to mention "auth" / "login". If gh changes its wording upstream
    # we'll see it as a regular ``failure`` and the test-lead's skill
    # will still refuse to declare success.
    _GH_AUTH_ERROR_MARKERS: tuple[str, ...] = (
        "gh auth login",
        "authentication required",
        "not logged in",
        "gh auth status",
        "HTTP 401",
    )

    def __init__(
        self,
        *,
        project_roots: dict[str, Path],
        gh_binary: str = "gh",
        git_binary: str = "git",
        gh_token_path: Path | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._project_roots = {k: Path(v) for k, v in project_roots.items()}
        self._gh = gh_binary
        self._git = git_binary
        self._gh_token_path = gh_token_path
        self._now = now

    # -- public API -----------------------------------------------------

    def watch(
        self,
        *,
        project_id: str,
        pr_number: int,
        timeout_seconds: int | None = None,
        poll_interval: int | None = None,
    ) -> CIWatchResult:
        root = self._resolve(project_id)

        effective_timeout = self._clamp_timeout(timeout_seconds)
        effective_interval = self._clamp_interval(poll_interval)

        if pr_number <= 0:
            raise CIWatchError(f"pr_number must be positive (got {pr_number}).")

        start = self._now()
        try:
            exit_code, combined_output = self._run_watch(
                root=root,
                pr_number=pr_number,
                timeout_seconds=effective_timeout,
                poll_interval=effective_interval,
            )
        except FileNotFoundError as exc:
            # gh binary missing is an environmental degradation, not a
            # programming error. Per skill contract (skills/tech_lead.md
            # and the tool-spec description) this MUST surface as
            # ``status="unavailable"`` so the LLM branches to the
            # "PR opened, please verify CI manually" reply instead of
            # getting an opaque {"error": ...} payload.
            watched = self._now() - start
            return CIWatchResult(
                status="unavailable",
                pr_number=pr_number,
                failing_jobs=[],
                summary=(
                    f"PR #{pr_number}: cannot read CI status — `{self._gh}` "
                    f"binary not installed on this host."
                ),
                watched_seconds=watched,
                reason=(
                    f"{self._gh} not found ({exc}). Install GitHub CLI or "
                    f"place a token at "
                    f".larkagent/secrets/github_key/gh_token.env."
                ),
            )
        except subprocess.TimeoutExpired as exc:
            watched = self._now() - start
            # On timeout, still try to surface which checks are pending
            # so the user gets a useful report before deciding whether
            # to keep waiting.
            pending = self._fetch_jobs(root, pr_number, bucket_filter=None) or []
            return CIWatchResult(
                status="timeout",
                pr_number=pr_number,
                failing_jobs=[],
                summary=(
                    f"PR #{pr_number}: CI still running after "
                    f"{effective_timeout}s ({len(pending)} checks tracked); "
                    f"tech-lead should escalate or extend the budget."
                ),
                watched_seconds=watched,
                reason=f"timeout after {effective_timeout}s ({exc})",
            )

        watched = self._now() - start

        if exit_code == 0:
            return CIWatchResult(
                status="success",
                pr_number=pr_number,
                failing_jobs=[],
                summary=f"PR #{pr_number}: all CI checks passed.",
                watched_seconds=watched,
            )

        # ``gh`` ran but wasn't authenticated. This is environmental, not
        # the PR's CI failing — route to ``unavailable`` so the skill
        # doesn't dispatch bug_fixer against a phantom code bug.
        if self._is_gh_auth_failure(combined_output):
            return CIWatchResult(
                status="unavailable",
                pr_number=pr_number,
                failing_jobs=[],
                summary=(
                    f"PR #{pr_number}: cannot read CI status — `{self._gh}` "
                    f"is not authenticated on this host."
                ),
                watched_seconds=watched,
                reason=(
                    f"gh pr checks exited rc={exit_code} with an auth error. "
                    f"Run `gh auth login` on this host or put a token at "
                    f".larkagent/secrets/github_key/gh_token.env. Tail: "
                    f"{combined_output[-300:].strip()!r}"
                ),
            )

        # Non-zero: either failure or pending (the latter shouldn't
        # happen with --watch but we defend against it).
        failing = self._fetch_jobs(root, pr_number, bucket_filter="fail")
        if exit_code == self._GH_EXIT_PENDING:
            return CIWatchResult(
                status="timeout",
                pr_number=pr_number,
                failing_jobs=[],
                summary=(
                    f"PR #{pr_number}: gh reports checks still pending "
                    f"after watch ({watched:.0f}s)."
                ),
                watched_seconds=watched,
                reason="gh pr checks returned exit code 8 (pending)",
            )

        if not failing:
            # Exit non-zero but JSON follow-up found nothing failing —
            # surface the raw output so the operator can investigate.
            return CIWatchResult(
                status="failure",
                pr_number=pr_number,
                failing_jobs=[],
                summary=(
                    f"PR #{pr_number}: gh exited rc={exit_code} but no failing "
                    f"jobs were parsed. Raw tail: "
                    f"{combined_output[-400:].strip()!r}"
                ),
                watched_seconds=watched,
                reason=f"gh pr checks exit code {exit_code}",
            )

        summary_names = ", ".join(j.name for j in failing[:3])
        if len(failing) > 3:
            summary_names += f" (+{len(failing) - 3} more)"
        return CIWatchResult(
            status="failure",
            pr_number=pr_number,
            failing_jobs=failing,
            summary=(
                f"PR #{pr_number}: {len(failing)} failing check(s): "
                f"{summary_names}"
            ),
            watched_seconds=watched,
        )

    # -- internals ------------------------------------------------------

    def _is_gh_auth_failure(self, combined_output: str) -> bool:
        """True if the gh output looks like an auth problem rather than a
        real CI failure. Matched against a small, explicit phrase list
        to avoid false positives on code that legitimately logs
        "authentication" in test output."""
        lower = combined_output.lower()
        return any(m.lower() in lower for m in self._GH_AUTH_ERROR_MARKERS)

    def _resolve(self, project_id: str) -> Path:
        root = self._project_roots.get(project_id)
        if root is None:
            raise CIWatchProjectError(
                f"No project configured for project_id={project_id!r}."
            )
        if not (root / ".git").exists():
            raise CIWatchError(f"{root} is not a git checkout (missing .git).")
        return root

    def _clamp_timeout(self, seconds: int | None) -> int:
        if seconds is None:
            return self.DEFAULT_TIMEOUT_SECONDS
        return max(
            self.MIN_TIMEOUT_SECONDS,
            min(int(seconds), self.MAX_TIMEOUT_SECONDS),
        )

    def _clamp_interval(self, seconds: int | None) -> int:
        if seconds is None:
            return self.DEFAULT_POLL_INTERVAL
        # gh's --interval default is 10; allow 5..60 to stay sane.
        return max(5, min(int(seconds), 60))

    def _run_watch(
        self,
        *,
        root: Path,
        pr_number: int,
        timeout_seconds: int,
        poll_interval: int,
    ) -> tuple[int, str]:
        """Run ``gh pr checks <n> --watch --fail-fast`` with a timeout.

        Returns ``(exit_code, combined_stdout_stderr)``. Raises
        ``FileNotFoundError`` when ``gh`` is missing, or
        ``subprocess.TimeoutExpired`` when the overall budget expires.
        """
        args = [
            self._gh,
            "pr",
            "checks",
            str(pr_number),
            "--watch",
            "--fail-fast",
            "--interval",
            str(poll_interval),
        ]
        logger.info(
            "CI watch: %s (cwd=%s, timeout=%ss)",
            shlex.join(args),
            root,
            timeout_seconds,
        )
        res = subprocess.run(
            args,
            cwd=str(root),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=build_gh_env(self._gh_token_path),
        )
        combined = "\n".join(
            filter(None, [(res.stdout or "").strip(), (res.stderr or "").strip()])
        )
        return res.returncode, combined

    def _fetch_jobs(
        self,
        root: Path,
        pr_number: int,
        *,
        bucket_filter: str | None,
    ) -> list[FailingJob]:
        """Call ``gh pr checks <n> --json ...`` to parse job details.

        ``bucket_filter="fail"`` returns only failing jobs; ``None`` returns
        everything (used on timeout to report still-pending checks). On
        any error we return ``[]`` rather than raise — the caller has
        already determined ``status`` and the job list is advisory only.
        """
        args = [
            self._gh,
            "pr",
            "checks",
            str(pr_number),
            "--json",
            self._JSON_FIELDS,
        ]
        try:
            res = subprocess.run(
                args,
                cwd=str(root),
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                env=build_gh_env(self._gh_token_path),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            logger.warning("failed to fetch failing jobs: %s", exc)
            return []
        if res.returncode not in (0, self._GH_EXIT_FAILURE, self._GH_EXIT_PENDING):
            logger.warning(
                "gh pr checks --json returned rc=%s: %s",
                res.returncode,
                (res.stderr or res.stdout or "").strip()[:400],
            )
            return []
        try:
            payload = json.loads(res.stdout or "[]")
        except json.JSONDecodeError:
            logger.warning(
                "gh pr checks --json returned non-JSON: %r",
                (res.stdout or "")[:400],
            )
            return []
        if not isinstance(payload, list):
            return []

        out: list[FailingJob] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            bucket = str(item.get("bucket", "")).lower()
            if bucket_filter is not None and bucket != bucket_filter:
                continue
            out.append(
                FailingJob(
                    name=str(item.get("name", "") or ""),
                    workflow=str(item.get("workflow", "") or ""),
                    state=str(item.get("state", "") or ""),
                    link=str(item.get("link", "") or ""),
                    description=str(item.get("description", "") or ""),
                )
            )
        return out
