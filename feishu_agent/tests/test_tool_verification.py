"""Unit tests for tool_verification.

Covers:
- Unknown tool → pass.
- Validator raising → pass + diagnostic (bug in verifier must never
  destroy a legitimate tool call).
- write_project_code: missing file → fail; present + size match → pass;
  size mismatch → fail; path escape → fail.
- write_project_code_batch: verifies every file entry.
- git_commit: malformed SHA → fail; non-existent SHA → fail;
  real commit → pass.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from feishu_agent.tools.tool_verification import (
    ToolVerifier,
    build_default_validators,
)


@pytest.mark.asyncio
async def test_unknown_tool_passes():
    v = ToolVerifier()
    out = await v.verify("unknown_tool", {}, {"any": "thing"})
    assert out.ok is True


@pytest.mark.asyncio
async def test_validator_raising_is_swallowed():
    async def bad_validator(name, args, result):
        raise RuntimeError("boom")

    v = ToolVerifier({"some_tool": bad_validator})
    out = await v.verify("some_tool", {}, {})
    assert out.ok is True
    assert out.diagnostics == {"verifier_error": "validator raised"}


@pytest.mark.asyncio
async def test_write_validator_file_present_and_size_matches(tmp_path: Path):
    file = tmp_path / "src" / "a.py"
    file.parent.mkdir(parents=True)
    file.write_text("abc", encoding="utf-8")

    verifier = ToolVerifier(
        build_default_validators(project_root_resolver=lambda a: tmp_path)
    )
    result = {"path": "src/a.py", "bytes_written": 3}
    out = await verifier.verify("write_project_code", {"project_id": "p"}, result)
    assert out.ok is True


@pytest.mark.asyncio
async def test_write_validator_missing_file_fails(tmp_path: Path):
    verifier = ToolVerifier(
        build_default_validators(project_root_resolver=lambda a: tmp_path)
    )
    result = {"path": "never/written.py", "bytes_written": 42}
    out = await verifier.verify("write_project_code", {}, result)
    assert out.ok is False
    assert "not found" in (out.error or "")


@pytest.mark.asyncio
async def test_write_validator_size_mismatch_fails(tmp_path: Path):
    f = tmp_path / "a.py"
    f.write_text("abcdefg", encoding="utf-8")
    verifier = ToolVerifier(
        build_default_validators(project_root_resolver=lambda a: tmp_path)
    )
    result = {"path": "a.py", "bytes_written": 999}
    out = await verifier.verify("write_project_code", {}, result)
    assert out.ok is False
    assert "mismatch" in (out.error or "")


@pytest.mark.asyncio
async def test_write_validator_ignores_tool_errors(tmp_path: Path):
    # When the tool itself returned error, validator should pass so the
    # LLM sees the original error, not a second layer of error noise.
    verifier = ToolVerifier(
        build_default_validators(project_root_resolver=lambda a: tmp_path)
    )
    out = await verifier.verify(
        "write_project_code",
        {},
        {"error": "policy denied", "path": "x.py"},
    )
    assert out.ok is True


@pytest.mark.asyncio
async def test_write_validator_path_escape_fails(tmp_path: Path):
    # Craft a path that resolves outside project root
    verifier = ToolVerifier(
        build_default_validators(project_root_resolver=lambda a: tmp_path)
    )
    result = {"path": "../outside.py"}
    out = await verifier.verify("write_project_code", {}, result)
    assert out.ok is False
    assert "escape" in (out.error or "")


@pytest.mark.asyncio
async def test_batch_validator_fails_on_any_missing(tmp_path: Path):
    good = tmp_path / "good.py"
    good.write_text("ok", encoding="utf-8")
    verifier = ToolVerifier(
        build_default_validators(project_root_resolver=lambda a: tmp_path)
    )
    result = {
        "count": 2,
        "files": [
            {"path": "good.py", "bytes_written": 2},
            {"path": "bad.py", "bytes_written": 1},
        ],
    }
    out = await verifier.verify("write_project_code_batch", {}, result)
    assert out.ok is False
    assert "bad.py" in (out.error or "")


@pytest.mark.asyncio
async def test_commit_validator_rejects_malformed_sha():
    verifier = ToolVerifier(
        build_default_validators(project_root_resolver=lambda a: Path("/tmp"))
    )
    out = await verifier.verify(
        "git_commit", {}, {"commit_sha": "not-a-sha"}
    )
    assert out.ok is False


@pytest.mark.asyncio
async def test_commit_validator_rejects_nonexistent_sha(tmp_path: Path):
    # Init a real empty git repo in tmp_path
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=tmp_path, check=True
    )

    verifier = ToolVerifier(
        build_default_validators(project_root_resolver=lambda a: tmp_path)
    )
    # 40 hex chars but not a real commit
    fake_sha = "0" * 40
    out = await verifier.verify(
        "git_commit", {}, {"commit_sha": fake_sha}
    )
    assert out.ok is False


@pytest.mark.asyncio
async def test_commit_validator_accepts_real_commit(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True
    )
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip()

    verifier = ToolVerifier(
        build_default_validators(project_root_resolver=lambda a: tmp_path)
    )
    out = await verifier.verify("git_commit", {}, {"commit_sha": sha})
    assert out.ok is True


@pytest.mark.asyncio
async def test_commit_validator_no_sha_is_pass():
    # Tool didn't return a commit_sha — nothing to verify, pass.
    verifier = ToolVerifier(
        build_default_validators(project_root_resolver=lambda a: Path("/tmp"))
    )
    out = await verifier.verify("git_commit", {}, {"committed": False})
    assert out.ok is True
