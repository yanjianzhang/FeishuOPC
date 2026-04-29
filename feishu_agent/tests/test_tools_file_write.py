"""Tests for the decorator-registered ``write_file`` tool."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

from feishu_agent.core.request_context import RequestContext
from feishu_agent.tools.tool_registry import GLOBAL_TOOL_REGISTRY


def _build_executor(allowed_write_root: Path | None):
    # Ensure the tool is registered — idempotent.
    importlib.import_module("feishu_agent.tools.legacy_tools.file_write")
    ctx = RequestContext(allowed_write_root=allowed_write_root).as_dict()
    return GLOBAL_TOOL_REGISTRY.build_executor(
        tool_names=["write_file"], context=ctx
    )


def test_write_file_writes_within_allowed_root(tmp_path: Path):
    executor = _build_executor(tmp_path)
    result = asyncio.run(
        executor.execute_tool(
            "write_file",
            {"path": "sub/specs.md", "content": "hello world"},
        )
    )
    assert result["ok"] is True
    assert result["path"] == "sub/specs.md"
    assert result["created_parents"] is True
    target = tmp_path / "sub" / "specs.md"
    assert target.read_text(encoding="utf-8") == "hello world"


def test_write_file_refuses_path_escape(tmp_path: Path):
    executor = _build_executor(tmp_path)
    result = asyncio.run(
        executor.execute_tool(
            "write_file",
            {"path": "../outside.md", "content": "nope"},
        )
    )
    assert isinstance(result, dict) and result.get("ok") is False
    assert result["error"] == "PATH_ESCAPES_ROOT"
    assert not (tmp_path.parent / "outside.md").exists()


def test_write_file_refuses_without_allowed_root(tmp_path: Path):
    executor = _build_executor(None)
    result = asyncio.run(
        executor.execute_tool(
            "write_file", {"path": "a.md", "content": "x"}
        )
    )
    assert isinstance(result, dict) and result.get("ok") is False
    assert result["error"] == "NO_ALLOWED_WRITE_ROOT"


def test_write_file_schema_hides_context_keys():
    # The write_file spec exposes only LLM-visible inputs, never
    # the injected allowed_write_root.
    importlib.import_module("feishu_agent.tools.legacy_tools.file_write")
    entry = GLOBAL_TOOL_REGISTRY.get_tool("write_file")
    assert entry is not None
    props = set(entry.spec.input_schema["properties"].keys())
    assert props == {"path", "content"}
    assert "allowed_write_root" not in props
    assert entry.spec.needs == ("allowed_write_root",)
    assert entry.spec.effect == "world"
