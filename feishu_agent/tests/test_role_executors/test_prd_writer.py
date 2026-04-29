from __future__ import annotations

from pathlib import Path

import pytest

from feishu_agent.roles.role_executors.prd_writer import (
    PRD_WRITER_TOOL_SPECS,
    PrdWriterExecutor,
)
from feishu_agent.roles.role_registry_service import RoleRegistryService

ROLES_DIR = Path(__file__).resolve().parents[3] / "skills" / "roles"


def _make_executor(tmp_path: Path) -> PrdWriterExecutor:
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    return PrdWriterExecutor(allowed_write_root=specs_dir)


# ======================================================================
# tool_specs()
# ======================================================================


def test_tool_specs_returns_exactly_1_tool(tmp_path: Path):
    executor = _make_executor(tmp_path)
    specs = executor.tool_specs()
    assert len(specs) == 1


def test_tool_specs_names(tmp_path: Path):
    executor = _make_executor(tmp_path)
    names = {s.name for s in executor.tool_specs()}
    assert names == {"write_file"}


def test_tool_specs_returns_fresh_list(tmp_path: Path):
    executor = _make_executor(tmp_path)
    a = executor.tool_specs()
    b = executor.tool_specs()
    assert a is not b
    assert a == b


def test_module_level_specs_match_instance(tmp_path: Path):
    executor = _make_executor(tmp_path)
    assert [s.name for s in executor.tool_specs()] == [s.name for s in PRD_WRITER_TOOL_SPECS]


# ======================================================================
# execute_tool handlers
# ======================================================================


@pytest.mark.asyncio
async def test_write_file_happy_path(tmp_path: Path):
    executor = _make_executor(tmp_path)
    result = await executor.execute_tool("write_file", {
        "path": "my-feature/prd.md",
        "content": "# PRD: My Feature\n\nThis is a test.",
    })
    assert result["path"] == "my-feature/prd.md"
    assert result["bytes_written"] > 0

    written = (tmp_path / "specs" / "my-feature" / "prd.md").read_text(encoding="utf-8")
    assert written == "# PRD: My Feature\n\nThis is a test."


@pytest.mark.asyncio
async def test_write_file_creates_parent_directories(tmp_path: Path):
    executor = _make_executor(tmp_path)
    nested_path = "deep/nested/dir/document.md"
    await executor.execute_tool("write_file", {
        "path": nested_path,
        "content": "nested content",
    })
    assert (tmp_path / "specs" / "deep" / "nested" / "dir" / "document.md").exists()


@pytest.mark.asyncio
async def test_write_file_path_traversal_rejected(tmp_path: Path):
    executor = _make_executor(tmp_path)
    with pytest.raises(RuntimeError, match="resolves outside the allowed write root"):
        await executor.execute_tool("write_file", {
            "path": "../../etc/passwd",
            "content": "malicious",
        })


@pytest.mark.asyncio
async def test_write_file_absolute_path_rejected(tmp_path: Path):
    executor = _make_executor(tmp_path)
    with pytest.raises(RuntimeError, match="resolves outside the allowed write root"):
        await executor.execute_tool("write_file", {
            "path": "/tmp/evil.txt",
            "content": "malicious",
        })


@pytest.mark.asyncio
async def test_write_file_dot_dot_in_middle_rejected(tmp_path: Path):
    executor = _make_executor(tmp_path)
    with pytest.raises(RuntimeError, match="resolves outside the allowed write root"):
        await executor.execute_tool("write_file", {
            "path": "legit/../../../escape.txt",
            "content": "malicious",
        })


@pytest.mark.asyncio
async def test_write_file_unicode_content(tmp_path: Path):
    executor = _make_executor(tmp_path)
    result = await executor.execute_tool("write_file", {
        "path": "chinese-prd.md",
        "content": "# 需求文档\n\n这是一个测试。",
    })
    assert result["bytes_written"] > 0
    written = (tmp_path / "specs" / "chinese-prd.md").read_text(encoding="utf-8")
    assert "需求文档" in written


@pytest.mark.asyncio
async def test_unsupported_tool_raises(tmp_path: Path):
    executor = _make_executor(tmp_path)
    with pytest.raises(RuntimeError, match="Unsupported tool"):
        await executor.execute_tool("nonexistent", {})


# ======================================================================
# Frontmatter parity
# ======================================================================


def test_tool_allow_list_matches_role_file():
    registry = RoleRegistryService(ROLES_DIR)
    role = registry.get_role("prd_writer")
    executor = PrdWriterExecutor(allowed_write_root=Path("/tmp"))
    assert set(role.tool_allow_list) == {s.name for s in executor.tool_specs()}
