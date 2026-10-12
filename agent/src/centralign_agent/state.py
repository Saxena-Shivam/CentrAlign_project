from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ToolResult:
    tool_name: str
    ok: bool
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class RunState:
    run_id: str
    goal: str
    plan: dict[str, Any] | None = None
    memory: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    write_history: list[dict[str, Any]] = field(default_factory=list)
    status: str = "running"
    final_message: str | None = None
    evidence: list[str] = field(default_factory=list)
    trace_path: str | None = None
    run_dir: str | None = None

    def persist(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(
                {
                    "run_id": self.run_id,
                    "goal": self.goal,
                    "plan": self.plan,
                    "memory": self.memory,
                    "steps": self.steps,
                    "approvals": self.approvals,
                    "write_history": self.write_history,
                    "status": self.status,
                    "final_message": self.final_message,
                    "evidence": self.evidence,
                    "trace_path": self.trace_path,
                    "run_dir": self.run_dir,
                },
                fh,
                indent=2,
            )
