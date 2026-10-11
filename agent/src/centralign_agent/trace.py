from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class TraceRecorder:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events: list[dict[str, Any]] = []
        self.trace_path = self.run_dir / "trace.jsonl"

    def record(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        with self.trace_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    def snapshot(self) -> list[dict[str, Any]]:
        return list(self.events)
