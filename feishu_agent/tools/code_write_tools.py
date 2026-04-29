"""Code-write tools for project source trees (TL-only).

These expose ``CodeWriteService`` as LLM tools. They are intentionally
kept separate from ``workflow_tools`` so PM has no way to opt in: PM's
executor simply doesn't instantiate the mixin.

The host class must expose:
- ``self._code_write: CodeWriteService | None``
- ``self.project_id: str``
- ``self._emit_code_write_update(line: str)`` (no-op is fine)
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from feishu_agent.core.agent_types import AgentToolSpec
from feishu_agent.tools.ci_watch_service import (
    CIWatchError,
    CIWatchService,
)
from feishu_agent.tools.code_write_service import (
    CodeWriteError,
    CodeWriteService,
)
from feishu_agent.tools.feishu_agent_tools import _tool_spec
from feishu_agent.tools.git_ops_service import (
    GitOpsError,
    GitOpsService,
)
from feishu_agent.tools.pre_push_inspector import (
    InspectionError,
    PrePushInspector,
)
from feishu_agent.tools.pull_request_service import (
    PullRequestError,
    PullRequestService,
)

# ---------------------------------------------------------------------------
# Arg models
# ---------------------------------------------------------------------------


class ReadProjectCodeArgs(BaseModel):
    relative_path: str = Field(
        description="Path relative to the project repo root (e.g. "
        "'example_app/lib/core/db/app_database.dart'). Must live under "
        "one of the policy's allowed_read_roots."
    )
    max_bytes: int = Field(
        default=512 * 1024,
        description="Read cap; truncated when file is larger.",
    )


class ListProjectPathsArgs(BaseModel):
    sub_path: str = Field(
        default="",
        description="Directory relative to project root. Empty string lists "
        "the top-level allowed_read_roots. Otherwise must live under a "
        "readable root.",
    )
    max_entries: int = Field(default=200, description="Upper bound per call.")


class WriteProjectCodeArgs(BaseModel):
    relative_path: str = Field(
        description="Path relative to project repo root. Must live under "
        "one of the policy's allowed_write_roots."
    )
    content: str = Field(description="Full UTF-8 file content.")
    reason: str = Field(
        description="Short human-readable reason for this write (e.g. "
        "'story 3-1: add DAO for vine_farming'). Recorded in the audit log."
    )
    confirmed: bool = Field(
        default=False,
        description="Set True ONLY after you've called request_confirmation "
        "and the user replied 确认. Required when the write exceeds the "
        "policy's require_confirmation_above_bytes.",
    )


class _ProjectCodeFile(BaseModel):
    relative_path: str = Field(description="Path relative to project repo root.")
    content: str = Field(description="Full UTF-8 file content.")
    reason: str = Field(
        default="",
        description="Optional per-file reason; falls back to the batch reason.",
    )


class WriteProjectCodeBatchArgs(BaseModel):
    files: list[_ProjectCodeFile] = Field(
        description="List of files to write. All-or-nothing: any policy "
        "violation aborts the whole batch before writing."
    )
    reason: str = Field(
        description="Short reason that applies to the whole batch."
    )
    confirmed: bool = Field(default=False, description="See WriteProjectCodeArgs.")


class DescribeCodeWritePolicyArgs(BaseModel):
    pass


# ---- pre-push / git-ops ---------------------------------------------------


class RunPrePushInspectionArgs(BaseModel):
    pass


class GitCommitArgs(BaseModel):
    message: str = Field(
        description=(
            "Commit message. Should reference the story/issue id + a short "
            "summary (e.g. '3-1: add vine_farming DAO + migration')."
        )
    )


class GitPushArgs(BaseModel):
    inspection_token: str = Field(
        description=(
            "The inspection_token returned by the most recent successful "
            "run_pre_push_inspection call (ok=true). Push is refused if "
            "this token is missing, expired, or doesn't match the current "
            "HEAD SHA + branch."
        )
    )
    remote: str = Field(
        default="origin",
        description="Remote name to push to. Default: origin.",
    )


class GitSyncRemoteArgs(BaseModel):
    remote: str = Field(
        default="origin",
        description="Remote name to fetch from. Default: origin.",
    )


class StartWorkBranchArgs(BaseModel):
    kind: str = Field(
        description=(
            "Branch kind / prefix. Must be one of: feat, fix, debug, "
            "chore, docs, refactor, test, exp. Final branch name will "
            "be '<kind>/<slug>'. Pick the one that matches the intent: "
            "new feature → feat, bug fix → fix, investigation spike → "
            "debug / exp, docs-only → docs, etc."
        )
    )
    slug: str = Field(
        description=(
            "Kebab-case descriptive suffix, 1-80 chars, starts with a "
            "lowercase letter or digit, then [a-z0-9._-]. Examples: "
            "'3-2-server-steal-api', 'fix-sprint-state-path', "
            "'debug-feishu-retry-storm'. Keep it short but specific — "
            "this ends up in the PR URL."
        )
    )
    base_branch: str = Field(
        default="main",
        description=(
            "Which remote branch to fork from. Defaults to 'main'. Use "
            "'master' for legacy repos; the service fetches "
            "'<remote>/<base_branch>' and points the new branch at that "
            "tip, leaving your local 'main' untouched."
        ),
    )
    remote: str = Field(
        default="origin",
        description="Remote name (default: origin).",
    )
    allow_discard_dirty: bool = Field(
        default=False,
        description=(
            "If the worktree has UNCOMMITTED changes, default behavior "
            "is to refuse and surface a typed error. Set True ONLY when "
            "the human has explicitly said 'throw away local changes' "
            "(via request_confirmation). Unpushed commits on the "
            "current branch are ALWAYS preserved regardless of this "
            "flag — they stay on that branch and are recoverable."
        ),
    )


class CreatePullRequestArgs(BaseModel):
    title: str = Field(
        description=(
            "PR title. Should reference the story/issue id + a short "
            "summary (e.g. '3-1: Add vine_farming DAO + migration')."
        )
    )
    body: str = Field(
        description=(
            "PR description in Markdown. Include: (a) what was changed "
            "and why, (b) link(s) to the implementation doc / changelog "
            "you updated, (c) testing notes, (d) any follow-ups."
        )
    )
    base: str = Field(
        default="main",
        description="Target base branch. Default: main.",
    )


class WatchPrChecksArgs(BaseModel):
    pr_number: int = Field(
        description=(
            "The PR number returned by create_pull_request. Used to poll "
            "GitHub Actions status via `gh pr checks --watch`."
        ),
        gt=0,
    )
    timeout_seconds: int = Field(
        default=600,
        description=(
            "Hard cap on how long to block waiting for CI (seconds). "
            "Default 600 (10 min); service clamps to a safe range. When "
            "the cap expires the call returns status='timeout' so the "
            "tech-lead can decide whether to keep waiting or escalate, "
            "rather than hanging the Feishu session."
        ),
        ge=30,
        le=1800,
    )
    poll_interval: int = Field(
        default=15,
        description=(
            "Seconds between gh's own refreshes while watching. Passed "
            "through to `gh pr checks --interval`. Kept in the 5..60 range."
        ),
        ge=5,
        le=60,
    )


# ---------------------------------------------------------------------------
# Tool specs
# ---------------------------------------------------------------------------


CODE_WRITE_TOOL_SPECS: list[AgentToolSpec] = [
    _tool_spec(
        "describe_code_write_policy",
        "Report the current project's code-write policy: allowed write roots, "
        "denied path segments, per-file size ceiling, and the confirmation "
        "threshold. Call this FIRST before attempting code edits.",
        DescribeCodeWritePolicyArgs,
    ),
    _tool_spec(
        "read_project_code",
        "Read a source file (e.g. lib/**, test/**, example_app/lib/**, tools/**) "
        "from the project repo. Subject to the same denied-segment list as "
        "writes. Truncates at max_bytes.",
        ReadProjectCodeArgs,
    ),
    _tool_spec(
        "list_project_paths",
        "List entries under a sub-directory of the project repo. Pass empty "
        "sub_path to see the top-level writable/readable roots.",
        ListProjectPathsArgs,
    ),
    _tool_spec(
        "write_project_code",
        "Write (create or overwrite) a single source file in the project repo. "
        "Enforces: (a) path must be under allowed_write_roots and not hit any "
        "denied segment (.env/secrets/.git/*.key/*.pem/...); (b) content must "
        "fit under hard_max_bytes_per_file; (c) if size > "
        "require_confirmation_above_bytes you MUST first call "
        "request_confirmation with a diff summary, then retry with "
        "confirmed=True. Every successful write is audit-logged and pushed to "
        "the Feishu thread.",
        WriteProjectCodeArgs,
    ),
    _tool_spec(
        "write_project_code_batch",
        "Batch variant of write_project_code. Validates every file up-front "
        "(path + size). If ANY file fails policy, NOTHING is written. Use for "
        "coherent multi-file edits (e.g. DAO + migration + test).",
        WriteProjectCodeBatchArgs,
    ),
]


PRE_PUSH_TOOL_SPECS: list[AgentToolSpec] = [
    _tool_spec(
        "run_pre_push_inspection",
        "Read-only pre-push inspection of the project repo. Runs 5 checks: "
        "(1) git diff summary, (2) secret_scanner on every added diff line "
        "AND every untracked file, (3) every changed path must sit under "
        "policy.allowed_write_roots, (4) oversized per-file changes flagged "
        "as warnings, (5) untracked-files listing. Returns "
        "{ok, blockers, warnings, files_changed, untracked_files, branch, "
        "head_sha, inspection_token}. The token is only present when ok=true "
        "and is REQUIRED by git_push. Always call this before declaring a "
        "coding task done.",
        RunPrePushInspectionArgs,
    ),
    _tool_spec(
        "git_commit",
        "Commit the current working-tree changes on the current branch. "
        "Refuses on protected branches (main/master by default). Stages only "
        "files that sit inside policy.allowed_write_roots; never does a "
        "blind `git add -A`. Does NOT push.",
        GitCommitArgs,
    ),
    _tool_spec(
        "git_push",
        "Push the current branch to the remote. Refuses on protected "
        "branches. Requires a fresh inspection_token from "
        "run_pre_push_inspection (same HEAD SHA + branch, inside TTL). "
        "After pushing, the audit log records the event. Does NOT touch "
        "main/master.",
        GitPushArgs,
    ),
    _tool_spec(
        "git_sync_remote",
        "Fetch from remote and fast-forward the current branch if SAFE. "
        "Safe = worktree clean AND local is strictly behind (no diverge). "
        "Dirty / diverged / no-upstream cases refuse to touch the worktree "
        "and return a typed error so the human can decide. Always call "
        "this BEFORE starting a coding task so you work off the latest "
        "upstream state.",
        GitSyncRemoteArgs,
    ),
    _tool_spec(
        "start_work_branch",
        "Create a fresh work branch off <remote>/<base_branch> and check "
        "it out. Call this BEFORE dispatching developer / bug_fixer for "
        "a NEW piece of work (new story, fresh fix, new investigation). "
        "Flow: validates kind/slug/protected status, refuses if worktree "
        "is dirty (unless allow_discard_dirty=True), fetches the base, "
        "creates '<kind>/<slug>' at the remote tip. Local 'main' is "
        "never mutated — the new branch forks directly off "
        "<remote>/<base_branch>. Unpushed commits on the previous branch "
        "stay on that branch and are recoverable. Returns "
        "{branch, base, head_sha, base_upstream_sha, previous_branch, "
        "previous_head_sha, discarded_dirty_paths}.",
        StartWorkBranchArgs,
    ),
    _tool_spec(
        "create_pull_request",
        "Open a GitHub pull request from the current branch to `base` "
        "via `gh pr create`. Refuses on protected branches. Requires the "
        "branch to have been pushed (otherwise git_push first). If the "
        "diff doesn't touch docs/, a warning is prepended to the PR body. "
        "Returns {url, number}. Call this AFTER git_push + implementation "
        "doc update; the URL is what you report back to the user.",
        CreatePullRequestArgs,
    ),
    _tool_spec(
        "watch_pr_checks",
        "Block on GitHub Actions for the given PR until all checks finish "
        "(or the timeout budget expires). MUST be called after "
        "create_pull_request and BEFORE declaring the PR merge-ready. "
        "Returns "
        "{status: success|failure|timeout|unavailable, failing_jobs: [...], "
        "summary, pr_number, watched_seconds, reason}. "
        "On `success` → CI is green, safe to tell the user the PR can be merged. "
        "On `failure` → dispatch bug_fixer with the failing_jobs list, then "
        "re-inspect, re-push, and call watch_pr_checks again. "
        "On `timeout` → escalate to the user; do NOT silently declare success. "
        "On `unavailable` → `gh` is missing / not authenticated; "
        "report to the user and ask them to verify CI manually.",
        WatchPrChecksArgs,
    ),
]


# ---------------------------------------------------------------------------
# Mixin
# ---------------------------------------------------------------------------


class CodeWriteToolsMixin:
    """Mixin adding the code-write + pre-push + git-ops tools to an executor.

    Host must set:
    - ``self._code_write: CodeWriteService | None`` (optional)
    - ``self._pre_push_inspector: PrePushInspector | None`` (optional)
    - ``self._git_ops: GitOpsService | None`` (optional)
    - ``self.project_id: str``

    and implement ``_emit_code_write_update``. Any of the three service
    references may be None; only the tools backed by non-None services
    are advertised to the LLM.

    Tool-allow filter (``_code_write_tool_allow``)
    ----------------------------------------------
    Optional ``frozenset[str]`` on the host. When set, BOTH the tool
    list and the dispatch handler are further intersected with this
    allow-set — meaning the LLM only sees those names AND an
    out-of-band call (prior context, cached name) to any other tool is
    refused with a stable error code. ``None`` (default) preserves the
    historical "all services you wired in get their tools advertised"
    behavior used by TL.

    This is how we split the trust boundary: TL gets the full set,
    developer gets a narrower subset (code-write + git_commit +
    git_sync_remote), and both end up using the same service
    instances.
    """

    _code_write: CodeWriteService | None
    _pre_push_inspector: "PrePushInspector | None"
    _git_ops: "GitOpsService | None"
    _pull_request: "PullRequestService | None"
    _ci_watch: "CIWatchService | None"
    _code_write_tool_allow: "frozenset[str] | None"
    project_id: str

    def code_write_tool_specs(self) -> list[AgentToolSpec]:
        specs: list[AgentToolSpec] = []
        if self._code_write is not None:
            specs.extend(CODE_WRITE_TOOL_SPECS)
        # Advertise each git/inspector/PR tool only when its backing
        # service exists. Partially-configured hosts still surface what
        # they can do.
        by_name = {s.name: s for s in PRE_PUSH_TOOL_SPECS}
        if self._pre_push_inspector is not None:
            specs.append(by_name["run_pre_push_inspection"])
        if self._git_ops is not None:
            specs.append(by_name["git_commit"])
            specs.append(by_name["git_push"])
            specs.append(by_name["git_sync_remote"])
            specs.append(by_name["start_work_branch"])
        if self._pull_request is not None:
            specs.append(by_name["create_pull_request"])
        if getattr(self, "_ci_watch", None) is not None:
            specs.append(by_name["watch_pr_checks"])
        allow = getattr(self, "_code_write_tool_allow", None)
        if allow is not None:
            specs = [s for s in specs if s.name in allow]
        return specs

    def _code_write_tool_allowed(self, tool_name: str) -> bool:
        allow = getattr(self, "_code_write_tool_allow", None)
        if allow is None:
            return True
        return tool_name in allow

    def _emit_code_write_update(self, line: str) -> None:  # override in host
        return None

    async def handle_code_write_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | None:
        # Belt + suspenders: even if a filtered tool name somehow made
        # it into the LLM's input (stale chat context, model
        # hallucination), refuse it here before touching any service.
        _KNOWN = {s.name for s in CODE_WRITE_TOOL_SPECS} | {
            s.name for s in PRE_PUSH_TOOL_SPECS
        }
        if tool_name in _KNOWN and not self._code_write_tool_allowed(tool_name):
            return {
                "error": "TOOL_NOT_ALLOWED_ON_ROLE",
                "message": (
                    f"Tool {tool_name!r} is not exposed to this role. "
                    f"Ask the tech lead to run it, or dispatch the "
                    f"appropriate sub-agent."
                ),
            }
        try:
            if self._code_write is not None:
                if tool_name == "describe_code_write_policy":
                    return self._code_write.describe_policy(self.project_id)
                if tool_name == "read_project_code":
                    parsed_r = ReadProjectCodeArgs.model_validate(arguments)
                    return self._code_write.read_source(
                        project_id=self.project_id,
                        relative_path=parsed_r.relative_path,
                        max_bytes=parsed_r.max_bytes,
                    )
                if tool_name == "list_project_paths":
                    parsed_l = ListProjectPathsArgs.model_validate(arguments)
                    return self._code_write.list_paths(
                        project_id=self.project_id,
                        sub_path=parsed_l.sub_path,
                        max_entries=parsed_l.max_entries,
                    )
                if tool_name == "write_project_code":
                    parsed_w = WriteProjectCodeArgs.model_validate(arguments)
                    res = self._code_write.write_source(
                        project_id=self.project_id,
                        relative_path=parsed_w.relative_path,
                        content=parsed_w.content,
                        reason=parsed_w.reason,
                        confirmed=parsed_w.confirmed,
                    )
                    self._push_write_line(res, multi=False)
                    return res
                if tool_name == "write_project_code_batch":
                    parsed_b = WriteProjectCodeBatchArgs.model_validate(arguments)
                    res = self._code_write.write_sources_batch(
                        project_id=self.project_id,
                        files=[
                            {
                                "relative_path": f.relative_path,
                                "content": f.content,
                                "reason": f.reason or parsed_b.reason,
                            }
                            for f in parsed_b.files
                        ],
                        reason=parsed_b.reason,
                        confirmed=parsed_b.confirmed,
                    )
                    self._push_write_line(res, multi=True)
                    return res

            if (
                tool_name == "run_pre_push_inspection"
                and self._pre_push_inspector is not None
            ):
                report = self._pre_push_inspector.inspect(self.project_id)
                self._push_inspection_line(report)
                return report.to_dict()

            if self._git_ops is not None:
                if tool_name == "git_commit":
                    parsed_c = GitCommitArgs.model_validate(arguments)
                    commit_res = self._git_ops.commit(
                        project_id=self.project_id,
                        message=parsed_c.message,
                    )
                    self._push_commit_line(commit_res.to_dict())
                    return commit_res.to_dict()
                if tool_name == "git_push":
                    parsed_p = GitPushArgs.model_validate(arguments)
                    push_res = self._git_ops.push_current_branch(
                        project_id=self.project_id,
                        inspection_token=parsed_p.inspection_token,
                        remote=parsed_p.remote,
                    )
                    self._push_push_line(push_res.to_dict())
                    return push_res.to_dict()
                if tool_name == "git_sync_remote":
                    parsed_s = GitSyncRemoteArgs.model_validate(arguments)
                    sync_res = self._git_ops.sync_with_remote(
                        project_id=self.project_id,
                        remote=parsed_s.remote,
                    )
                    self._push_sync_line(sync_res.to_dict())
                    return sync_res.to_dict()
                if tool_name == "start_work_branch":
                    parsed_sb = StartWorkBranchArgs.model_validate(arguments)
                    branch_res = self._git_ops.start_work_branch(
                        project_id=self.project_id,
                        kind=parsed_sb.kind,
                        slug=parsed_sb.slug,
                        base_branch=parsed_sb.base_branch,
                        remote=parsed_sb.remote,
                        allow_discard_dirty=parsed_sb.allow_discard_dirty,
                    )
                    self._push_start_branch_line(branch_res.to_dict())
                    return branch_res.to_dict()

            if (
                tool_name == "create_pull_request"
                and self._pull_request is not None
            ):
                parsed_pr = CreatePullRequestArgs.model_validate(arguments)
                pr_res = self._pull_request.create_pull_request(
                    project_id=self.project_id,
                    title=parsed_pr.title,
                    body=parsed_pr.body,
                    base=parsed_pr.base,
                )
                self._push_pr_line(pr_res.to_dict())
                return pr_res.to_dict()

            if (
                tool_name == "watch_pr_checks"
                and getattr(self, "_ci_watch", None) is not None
            ):
                parsed_w = WatchPrChecksArgs.model_validate(arguments)
                watch_res = self._ci_watch.watch(  # type: ignore[union-attr]
                    project_id=self.project_id,
                    pr_number=parsed_w.pr_number,
                    timeout_seconds=parsed_w.timeout_seconds,
                    poll_interval=parsed_w.poll_interval,
                )
                payload = watch_res.to_dict()
                self._push_ci_watch_line(payload)
                return payload
        except CodeWriteError as exc:
            return {"error": exc.code, "message": exc.message}
        except InspectionError as exc:
            return {"error": exc.code, "message": exc.message}
        except GitOpsError as exc:
            return {"error": exc.code, "message": exc.message}
        except PullRequestError as exc:
            return {"error": exc.code, "message": exc.message}
        except CIWatchError as exc:
            # The service degrades to ``status="unavailable"`` for the
            # common "gh missing / unauthenticated" case so the LLM can
            # branch on it. A raised CIWatchError is reserved for genuine
            # config / programming errors and is surfaced as a tool error
            # so the LLM doesn't mistake it for "CI failed".
            return {"error": exc.code, "message": exc.message}
        return None

    def _push_write_line(self, result: dict[str, Any], *, multi: bool) -> None:
        try:
            if multi:
                files = result.get("files") or []
                total_bytes = sum(int(f.get("bytes_written") or 0) for f in files)
                paths = ", ".join(f.get("path", "?") for f in files[:3])
                suffix = "" if len(files) <= 3 else f" +{len(files) - 3} more"
                self._emit_code_write_update(
                    f"✏️ 代码批量写入 {len(files)} 文件 / {total_bytes}B: {paths}{suffix}"
                )
            else:
                path = result.get("path", "?")
                bw = int(result.get("bytes_written") or 0)
                icon = "➕" if result.get("is_new_file") else "✏️"
                delta = int(result.get("bytes_delta") or 0)
                delta_str = f" (Δ{delta:+d}B)" if not result.get("is_new_file") else ""
                self._emit_code_write_update(
                    f"{icon} 代码写入 {path} / {bw}B{delta_str}"
                )
        except Exception:  # pragma: no cover — thread push is best-effort
            pass

    def _push_inspection_line(self, report: Any) -> None:
        try:
            ok = getattr(report, "ok", False)
            blockers = len(getattr(report, "blockers", []))
            warnings = len(getattr(report, "warnings", []))
            branch = getattr(report, "branch", "?")
            files = len(getattr(report, "files_changed", []))
            untracked = len(getattr(report, "untracked_files", []))
            status = "✅" if ok else "⛔"
            line = (
                f"{status} pre-push 检查 branch={branch} "
                f"files={files} untracked={untracked} "
                f"blockers={blockers} warnings={warnings}"
            )
            self._emit_code_write_update(line)
        except Exception:  # pragma: no cover
            pass

    def _push_commit_line(self, result: dict[str, Any]) -> None:
        try:
            branch = result.get("branch", "?")
            sha = (result.get("commit_sha") or "")[:8]
            count = result.get("files_count", "?")
            msg = result.get("message") or ""
            msg_preview = msg.splitlines()[0][:80] if msg else ""
            self._emit_code_write_update(
                f"📝 git commit {branch}@{sha} ({count} files): {msg_preview}"
            )
        except Exception:  # pragma: no cover
            pass

    def _push_push_line(self, result: dict[str, Any]) -> None:
        try:
            branch = result.get("branch", "?")
            remote = result.get("remote", "?")
            sha = (result.get("pushed_head_sha") or "")[:8]
            self._emit_code_write_update(
                f"🚀 git push {remote}/{branch}@{sha}"
            )
        except Exception:  # pragma: no cover
            pass

    def _push_sync_line(self, result: dict[str, Any]) -> None:
        try:
            status = result.get("status", "?")
            branch = result.get("branch", "?")
            remote = result.get("remote", "?")
            ahead = result.get("ahead_count", 0)
            behind = result.get("behind_count", 0)
            icon = {
                "up_to_date": "✅",
                "fast_forwarded": "⬇️",
                "ahead_no_action": "↗️",
            }.get(status, "ℹ️")
            suffix = ""
            if status == "fast_forwarded":
                pulled = len(result.get("pulled_commits") or [])
                suffix = f" ({pulled} commit(s) pulled)"
            elif status in ("ahead_no_action",):
                suffix = f" (ahead {ahead})"
            self._emit_code_write_update(
                f"{icon} git sync {remote}/{branch}: {status}"
                f" [ahead={ahead}, behind={behind}]{suffix}"
            )
        except Exception:  # pragma: no cover
            pass

    def _push_start_branch_line(self, result: dict[str, Any]) -> None:
        try:
            branch = result.get("branch", "?")
            base = result.get("base", "?")
            remote = result.get("remote", "origin")
            base_sha = (result.get("base_upstream_sha") or "")[:8]
            prev = result.get("previous_branch") or "—"
            discarded = len(result.get("discarded_dirty_paths") or [])
            suffix = f" (弃掉 {discarded} 个本地改动)" if discarded else ""
            self._emit_code_write_update(
                f"🌿 新建分支 {branch} ← {remote}/{base}@{base_sha} "
                f"（之前在 {prev}){suffix}"
            )
        except Exception:  # pragma: no cover
            pass

    def _push_pr_line(self, result: dict[str, Any]) -> None:
        try:
            url = result.get("url", "?")
            number = result.get("number")
            branch = result.get("branch", "?")
            base = result.get("base", "?")
            warnings = result.get("warnings") or []
            warning_str = f" ⚠️ {len(warnings)}" if warnings else ""
            self._emit_code_write_update(
                f"🔗 PR #{number} opened: {branch} → {base}{warning_str}\n{url}"
            )
        except Exception:  # pragma: no cover
            pass

    def _push_ci_watch_line(self, result: dict[str, Any]) -> None:
        try:
            number = result.get("pr_number")
            status = result.get("status", "?")
            watched = result.get("watched_seconds") or 0
            failing = result.get("failing_jobs") or []
            icons = {
                "success": "✅",
                "failure": "❌",
                "timeout": "⏳",
                "unavailable": "⚠️",
            }
            icon = icons.get(status, "•")
            tail = ""
            if failing:
                names = ", ".join(j.get("name", "?") for j in failing[:3])
                if len(failing) > 3:
                    names += f" (+{len(failing) - 3} more)"
                tail = f"\n  failing: {names}"
            self._emit_code_write_update(
                f"{icon} CI watch PR #{number} → {status} ({watched:.0f}s){tail}"
            )
        except Exception:  # pragma: no cover
            pass
