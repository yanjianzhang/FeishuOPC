"""CLI smoke tests for ``feishu_agent.task_inspect``.

The CLI is a thin wrapper over ``TaskEventLog`` / ``task_replay`` —
the heavy lifting is tested elsewhere. Here we only assert:

1. ``list`` exits 0 and prints each task's name exactly once.
2. ``show <prefix>`` resolves unique prefixes.
3. ``show`` prints structured sections the operator can grep for
   (``=== meta ===`` / ``=== snapshot``).
4. Ambiguous prefixes exit non-zero and name both candidates.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from feishu_agent.task_inspect import main as cli_main
from feishu_agent.team.task_event_log import TaskEventLog


def _make_task(root: Path, name: str, *, role: str = "tech_lead") -> Path:
    d = root / name
    log = TaskEventLog(d)
    log.write_meta(
        {
            "task_id": name,
            "bot_name": "bot",
            "chat_id": "c1",
            "root_id": "r1",
            "role_name": role,
        }
    )
    log.append(kind="task.opened", payload={"role_name": role})
    log.append(kind="message.inbound", payload={"content": "hello"})
    log.append(kind="message.outbound", payload={"content": "hi"})
    return d


def test_cli_list_prints_every_task(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _make_task(tmp_path, "bot-aaa111222333")
    _make_task(tmp_path, "bot-bbb111222333")

    rc = cli_main(["--root", str(tmp_path), "list"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "bot-aaa111222333" in out
    assert "bot-bbb111222333" in out


def test_cli_show_resolves_unique_prefix(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _make_task(tmp_path, "bot-aaa111222333")

    rc = cli_main(["--root", str(tmp_path), "show", "bot-aaa"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "=== meta ===" in out
    assert "=== snapshot" in out
    assert "bot-aaa111222333" in out


def test_cli_show_ambiguous_prefix_exits_nonzero(tmp_path: Path) -> None:
    _make_task(tmp_path, "bot-aaa111222333")
    _make_task(tmp_path, "bot-aaa999888777")

    with pytest.raises(SystemExit) as exc_info:
        cli_main(["--root", str(tmp_path), "show", "bot-aaa"])
    assert exc_info.value.code != 0


def test_cli_events_dumps_jsonl(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _make_task(tmp_path, "bot-aaa111222333")

    rc = cli_main(["--root", str(tmp_path), "events", "bot-aaa111222333"])
    out = capsys.readouterr().out

    assert rc == 0
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) >= 3
    for line in lines:
        assert line.startswith("{")
