"""``task_inspect`` — read-only browser for the per-task event log.

Quick reference
---------------
- ``python -m feishu_agent.task_inspect list`` — enumerate tasks under
  ``--root``, sorted by latest event timestamp.
- ``python -m feishu_agent.task_inspect show <task_id>`` — summary:
  meta + snapshot (computed via :func:`task_replay.replay`) +
  last N events.
- ``python -m feishu_agent.task_inspect events <task_id>`` — raw
  ``events.jsonl`` dump.

The command is intentionally minimal — M2 will extend it with
``reminders`` / ``state`` / ``diff`` subcommands once the richer
:class:`TaskState` projector lands. Keeping the M1 shape small means
operators can rely on it for incident triage while we iterate.

Lookup rules
------------
``<task_id>`` matches either the full directory name or a unique
prefix. Ambiguous prefixes fail loudly (rather than picking one) —
typo-tolerant match is easy to get wrong during incidents.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from feishu_agent.team.task_event_log import TaskEventLog
from feishu_agent.team.task_replay import replay

DEFAULT_ROOT = Path("data/tasks")


def _candidate_dirs(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        (p for p in root.iterdir() if p.is_dir() and (p / "events.jsonl").exists()),
        key=lambda p: p.name,
    )


def _resolve_task_dir(root: Path, token: str) -> Path:
    exact = root / token
    if exact.is_dir() and (exact / "events.jsonl").exists():
        return exact
    matches = [p for p in _candidate_dirs(root) if p.name.startswith(token)]
    if not matches:
        raise SystemExit(f"no task matches {token!r} under {root}")
    if len(matches) > 1:
        names = ", ".join(p.name for p in matches)
        raise SystemExit(f"ambiguous task id {token!r}; matches: {names}")
    return matches[0]


def _cmd_list(root: Path) -> int:
    dirs = _candidate_dirs(root)
    if not dirs:
        print(f"(no tasks under {root})")
        return 0
    print(f"{'task_id':40}  {'events':>6}  {'role':15}  {'bot':15}")
    for d in dirs:
        log = TaskEventLog(d)
        meta = log.read_meta()
        events = log.read_events()
        role = str(meta.get("role_name") or "-")[:15]
        bot = str(meta.get("bot_name") or "-")[:15]
        print(f"{d.name:40}  {len(events):>6}  {role:15}  {bot:15}")
    return 0


def _cmd_show(root: Path, token: str, *, tail: int) -> int:
    task_dir = _resolve_task_dir(root, token)
    log = TaskEventLog(task_dir)
    meta = log.read_meta()
    events = log.read_events()
    snap = replay(events)

    print("=== meta ===")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    print()
    print("=== snapshot (m1_lite) ===")
    print(json.dumps(snap.to_dict(), ensure_ascii=False, indent=2))
    if tail > 0 and events:
        print()
        print(f"=== last {min(tail, len(events))} events ===")
        for event in events[-tail:]:
            print(event.to_json())
    return 0


def _cmd_events(root: Path, token: str) -> int:
    task_dir = _resolve_task_dir(root, token)
    log = TaskEventLog(task_dir)
    for event in log.iter_events():
        print(event.to_json())
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="task_inspect",
        description="Read-only browser for the per-task append-only event log.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help=f"Tasks root directory (default: {DEFAULT_ROOT})",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List tasks under --root")

    show = sub.add_parser("show", help="Show meta + snapshot + last events")
    show.add_argument("task_id", help="Full directory name or unique prefix")
    show.add_argument(
        "--tail",
        type=int,
        default=10,
        help="Number of most recent events to print (default: 10; 0 to skip)",
    )

    events = sub.add_parser("events", help="Dump raw events.jsonl to stdout")
    events.add_argument("task_id", help="Full directory name or unique prefix")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    root: Path = args.root

    if args.cmd == "list":
        return _cmd_list(root)
    if args.cmd == "show":
        return _cmd_show(root, args.task_id, tail=args.tail)
    if args.cmd == "events":
        return _cmd_events(root, args.task_id)
    parser.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
