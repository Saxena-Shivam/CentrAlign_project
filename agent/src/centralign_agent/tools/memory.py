from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class RememberArgs(BaseModel):
    key: str = Field(..., min_length=1)
    value: Any


class RecallArgs(BaseModel):
    key: str = Field(..., min_length=1)


class MemoryToolSet:
    @staticmethod
    def remember(arguments: dict[str, Any], state_path: Path | None = None) -> dict[str, Any]:
        args = RememberArgs.model_validate(arguments).model_dump()
        state_path = state_path or Path("runs") / "latest" / "state.json"
        state_exists = state_path.exists()
        if state_exists:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        else:
            payload = {"memory": {}}
        payload.setdefault("memory", {})
        payload["memory"][args["key"]] = args["value"]
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return {"status": "ok", "stored": args["key"], "value": args["value"]}

    @staticmethod
    def recall(arguments: dict[str, Any], state_path: Path | None = None) -> dict[str, Any]:
        args = RecallArgs.model_validate(arguments).model_dump()
        state_path = state_path or Path("runs") / "latest" / "state.json"
        if not state_path.exists():
            return {"status": "ok", "key": args["key"], "value": None}
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        return {"status": "ok", "key": args["key"], "value": payload.get("memory", {}).get(args["key"])}
