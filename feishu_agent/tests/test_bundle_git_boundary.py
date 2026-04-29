"""Story 004.5 AC-4 — CI guard for the git_local / git_remote split.

B-3's entire parallelism win rests on one invariant: the `git_local`
bundle (commits, diffs, local refs) never takes `repo_filelock`,
because two worktrees can legitimately run those operations in
parallel. Conversely, the `git_remote` bundle (fetch/push/pull) MUST
take `repo_filelock` because it races against the same remote + the
main working copy's HEAD.

A regression here is hard to spot in review: someone re-introducing
`repo_filelock` into `git_local.py` would serialise every developer
dispatch through a single lock again, reverting the whole B-3 win,
and the unit tests (which run in-process with mock git) would still
pass. This module is the CI guard that catches that at source-read
time.

These tests are intentionally text-only — no imports, no runtime
behaviour. They are cheap enough to run in the default pytest
collection.
"""

from __future__ import annotations

from pathlib import Path

GIT_LOCAL = (
    Path(__file__).parent.parent / "tools" / "bundles" / "git_local.py"
)
GIT_REMOTE = (
    Path(__file__).parent.parent / "tools" / "bundles" / "git_remote.py"
)


def _strip_comments_and_docstrings(src: str) -> str:
    '''Return the src with triple-quoted docstrings and
    ``# ...`` line comments stripped.

    Rationale: the module docstring of `git_local.py` legitimately
    talks *about* `repo_filelock` (as in "we deliberately do NOT take
    it"). A naive substring check would false-positive on that
    narrative. The boundary we actually care about is the *executable*
    surface — imports, function bodies, call sites.

    This is not a full Python tokeniser; it's a deliberately simple
    filter that catches the two shapes of doc commentary we actually
    use. If someone writes a nested f-string with embedded triple
    quotes they'll fool it — and also fool every human reviewer, so
    the guard's failure mode matches the review failure mode.
    '''
    out: list[str] = []
    in_doc = False
    for line in src.splitlines():
        stripped = line.strip()
        # Toggle on an opening triple-quote; if the same line also
        # contains the closing triple quote (single-line docstring)
        # drop it without toggling state.
        if not in_doc:
            if stripped.startswith(('"""', "'''")):
                quote = stripped[:3]
                # Single-line docstring on this line only.
                if stripped.count(quote) >= 2 and len(stripped) > 3:
                    continue
                in_doc = True
                continue
            # Strip line comments (keep shebangs etc. which don't
            # appear in these modules anyway).
            if "#" in line:
                line = line.split("#", 1)[0]
            if line.strip():
                out.append(line)
        else:
            if '"""' in stripped or "'''" in stripped:
                in_doc = False
    return "\n".join(out)


def test_git_local_bundle_does_not_use_repo_filelock() -> None:
    raw = GIT_LOCAL.read_text()
    src = _strip_comments_and_docstrings(raw)
    assert "repo_filelock" not in src, (
        "Story 004.5 AC-4 violated: feishu_agent/tools/bundles/git_local.py "
        "references `repo_filelock` in executable code. That lock is "
        "reserved for git_remote (fetch/push). Re-adding it here serialises "
        "every developer dispatch through a single mutex and reverts the "
        "entire B-3 parallelism win."
    )


def test_git_local_bundle_does_not_import_fcntl() -> None:
    """Hardening check: a contributor adding fcntl imports to
    git_local almost certainly means they're hand-rolling a new file
    lock, which is the back-door equivalent of re-adding
    ``repo_filelock``. Catch at source-read time rather than wait
    for a slow-test regression.
    """
    src = _strip_comments_and_docstrings(GIT_LOCAL.read_text())
    assert "import fcntl" not in src and "from fcntl" not in src, (
        "git_local.py must not import fcntl; if you need a filelock "
        "you are almost certainly working in git_remote territory."
    )


def test_git_remote_bundle_flows_through_locked_services() -> None:
    """The dual of AC-4: ``git_remote`` MUST route every mutating
    call through ``GitOpsService`` / ``PullRequestService``. Those
    services take ``repo_filelock`` internally (see
    ``git_ops_service.py`` ``with repo_filelock(root):``), which is
    the actual serialisation point that protects the remote refs.

    Asserting "``repo_filelock`` appears textually in
    ``git_remote.py``" would be a lie — the bundle layer deliberately
    stays service-oriented and never handles the lock itself. What we
    REALLY care about is that no future contributor open-codes a
    git-push without going through the service, which is exactly what
    this check pins.
    """
    src = _strip_comments_and_docstrings(GIT_REMOTE.read_text())
    assert "git_ops_service" in src, (
        "git_remote.py must route git-remote operations through "
        "ctx.git_ops_service (the service takes repo_filelock internally)."
    )
    # No raw subprocess-to-git here — that would bypass the service
    # layer entirely.
    assert "subprocess" not in src, (
        "git_remote.py must not call subprocess directly; that would "
        "bypass GitOpsService and its repo_filelock contract."
    )
