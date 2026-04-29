from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_SAFE_TRACE_ID = re.compile(r"^[\w\-]+$")


class AuditService:
    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir

    @staticmethod
    def _validate_trace_id(trace_id: str) -> str:
        if not trace_id or not _SAFE_TRACE_ID.match(trace_id):
            raise ValueError(
                f"Invalid trace_id: must be alphanumeric/dash/underscore, got {trace_id!r}"
            )
        return trace_id

    def write(self, trace_id: str, payload: dict[str, Any]) -> str:
        self._validate_trace_id(trace_id)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        file_path = self.log_dir / f"{trace_id}.json"
        file_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return f"{trace_id}.json"

    def read(self, trace_id: str) -> dict[str, Any] | None:
        self._validate_trace_id(trace_id)
        file_path = self.log_dir / f"{trace_id}.json"
        if not file_path.exists():
            return None
        return json.loads(file_path.read_text(encoding="utf-8"))
